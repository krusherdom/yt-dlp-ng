"""Persisted app settings: the proxy pool plus the general options.

One small JSON document in ``config.SETTINGS_FILE``, written atomically
(tmp file + :func:`os.replace`) under an asyncio lock and cached in module
state so that the synchronous worker threads can read it without touching the
event loop.

On-disk shape (since v0.4.0)::

    {"proxies": {...ProxySettings...}, "general": {...GeneralSettings...}}

Older files are a bare ``ProxySettings`` dump (whose own ``proxies`` key is a
*list*) or a ``{"proxy": {...}}`` envelope; both are migrated on load and
rewritten in the new shape by the next save.

``load()`` is awaited once at startup; :func:`get` / :func:`get_general` are
the hot paths and never do I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from . import config
from .models import GeneralSettings, ProxySettings

log = logging.getLogger("ytdlpweb.settings")

_cached: Optional[ProxySettings] = None
_cached_general: Optional[GeneralSettings] = None
_lock = asyncio.Lock()


# --------------------------------------------------------------------------
# Masking
# --------------------------------------------------------------------------


def mask_url(url: str) -> str:
    """``socks5://bob:hunter2@h:1080`` -> ``socks5://bob:***@h:1080``.

    URLs without embedded credentials are returned untouched. The host part is
    taken verbatim from ``netloc`` so bracketed IPv6 literals survive.
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    netloc = parts.netloc
    if "@" not in netloc:
        return raw
    userinfo, host = netloc.rsplit("@", 1)
    if ":" not in userinfo:
        # A username with no password: nothing secret to hide.
        return raw
    user = userinfo.split(":", 1)[0]
    masked = f"{user}:***@{host}"
    return parts._replace(netloc=masked).geturl()


def is_masked(url: str) -> bool:
    """True when ``url`` looks like the output of :func:`mask_url`."""
    raw = (url or "").strip()
    if "@" not in raw:
        return False
    try:
        netloc = urlsplit(raw).netloc
    except ValueError:
        return False
    userinfo = netloc.rsplit("@", 1)[0]
    return userinfo.split(":", 1)[-1] == "***"


def _masked_dump(settings: ProxySettings) -> Dict[str, Any]:
    data = settings.model_dump()
    for entry in data.get("proxies") or []:
        entry["url"] = mask_url(entry.get("url") or "")
    return data


def public_settings() -> dict:
    """The cached settings as the API returns them: every URL masked."""
    return _masked_dump(get())


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------


def _split_sections(data: Dict[str, Any]) -> Tuple[Any, Any]:
    """Pick the proxy and general payloads out of any known file shape.

    A bare ``ProxySettings`` dump also has a ``proxies`` key, but it holds a
    *list*; the v0.4.0 envelope holds a *dict*. That difference is what tells
    the two apart.
    """
    if isinstance(data.get("proxies"), dict):  # v0.4.0 envelope
        return data.get("proxies"), data.get("general")
    if isinstance(data.get("proxy"), dict):  # legacy {"proxy": {...}} envelope
        return data.get("proxy"), data.get("general")
    return data, data.get("general")  # legacy bare ProxySettings dump


def _read_file() -> Tuple[ProxySettings, GeneralSettings]:
    path = config.SETTINGS_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProxySettings(), GeneralSettings()
    except OSError as exc:
        log.warning("settings: could not read %s (%s); using defaults", path, exc)
        return ProxySettings(), GeneralSettings()

    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("settings: %s is not valid JSON (%s); using defaults", path, exc)
        return ProxySettings(), GeneralSettings()

    if not isinstance(data, dict):
        log.warning("settings: %s is not a JSON object; using defaults", path)
        return ProxySettings(), GeneralSettings()

    proxy_payload, general_payload = _split_sections(data)

    # The two sections are validated independently: a malformed "general"
    # block must never cost the user their configured proxies.
    try:
        proxy_settings = ProxySettings(**(proxy_payload or {}))
    except Exception as exc:
        log.warning("settings: %s proxies failed validation (%s); using defaults", path, exc)
        proxy_settings = ProxySettings()

    try:
        general = GeneralSettings(**(general_payload or {}))
    except Exception as exc:
        log.warning("settings: %s general failed validation (%s); using defaults", path, exc)
        general = GeneralSettings()

    return proxy_settings, general


async def load() -> ProxySettings:
    """Read the settings file into the cache. Never raises."""
    global _cached, _cached_general
    async with _lock:
        settings, general = _read_file()
        _cached = settings
        _cached_general = general
    _reset_statuses(settings)
    return settings


def get() -> ProxySettings:
    """The cached settings.

    Falls back to defaults when :func:`load` has not run yet (tests, or a call
    that races startup) so no caller ever has to handle ``None``.
    """
    return _cached if _cached is not None else ProxySettings()


def get_general() -> GeneralSettings:
    """The cached general settings; defaults before :func:`load` has run."""
    return _cached_general if _cached_general is not None else GeneralSettings()


def public_general() -> dict:
    """The general settings as the API returns them (nothing is secret)."""
    return get_general().model_dump()


def _unmask(settings: ProxySettings) -> ProxySettings:
    """Restore credentials the API masked on the way out.

    ``GET /api/proxies`` returns ``user:***@host``; a UI that PUTs the form
    straight back would otherwise persist the mask and break the proxy. Any
    entry whose URL is exactly the mask of the stored URL for the same id keeps
    the stored URL.
    """
    current = {p.id: p.url for p in get().proxies}
    changed = False
    entries = []
    for entry in settings.proxies:
        stored = current.get(entry.id)
        if stored and entry.url != stored and is_masked(entry.url):
            if mask_url(stored) == entry.url:
                entry = entry.model_copy(update={"url": stored})
                changed = True
        entries.append(entry)
    if not changed:
        return settings
    return settings.model_copy(update={"proxies": entries})


def _atomic_write(path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _reset_statuses(settings: ProxySettings) -> None:
    # Imported lazily: app.proxies imports this module.
    from . import proxies

    proxies.reset_statuses_for(settings)


def _envelope(proxy_settings: ProxySettings, general: GeneralSettings) -> str:
    return json.dumps(
        {"proxies": proxy_settings.model_dump(), "general": general.model_dump()},
        indent=2,
        sort_keys=True,
    )


async def save(settings: ProxySettings) -> ProxySettings:
    """Persist the proxy section atomically and update the cache."""
    global _cached
    settings = _unmask(settings)
    async with _lock:
        payload = _envelope(settings, get_general())
        _atomic_write(config.SETTINGS_FILE, payload)
        _cached = settings
    _reset_statuses(settings)
    return settings


async def save_general(general: GeneralSettings) -> GeneralSettings:
    """Persist the general section atomically and update the cache.

    Deliberately does *not* touch the proxy health table: nothing about these
    options invalidates a live/dead verdict.
    """
    global _cached_general
    async with _lock:
        payload = _envelope(get(), general)
        _atomic_write(config.SETTINGS_FILE, payload)
        _cached_general = general
    return general


def reset_cache() -> None:
    """Drop the cache (tests)."""
    global _cached, _cached_general
    _cached = None
    _cached_general = None
