"""Persisted app settings (currently: the proxy pool).

One small JSON document in ``config.SETTINGS_FILE``, written atomically
(tmp file + :func:`os.replace`) under an asyncio lock and cached in module
state so that the synchronous worker threads can read it without touching the
event loop.

``load()`` is awaited once at startup; :func:`get` is the hot path and never
does I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from . import config
from .models import ProxySettings

log = logging.getLogger("ytdlpweb.settings")

_cached: Optional[ProxySettings] = None
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


def _read_file() -> ProxySettings:
    path = config.SETTINGS_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProxySettings()
    except OSError as exc:
        log.warning("settings: could not read %s (%s); using defaults", path, exc)
        return ProxySettings()

    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.warning("settings: %s is not valid JSON (%s); using defaults", path, exc)
        return ProxySettings()

    if not isinstance(data, dict):
        log.warning("settings: %s is not a JSON object; using defaults", path)
        return ProxySettings()

    # Tolerate both {"proxy": {...}} envelopes and a bare ProxySettings dump.
    payload = data.get("proxy") if isinstance(data.get("proxy"), dict) else data
    try:
        return ProxySettings(**payload)
    except Exception as exc:
        log.warning("settings: %s failed validation (%s); using defaults", path, exc)
        return ProxySettings()


async def load() -> ProxySettings:
    """Read the settings file into the cache. Never raises."""
    global _cached
    async with _lock:
        settings = _read_file()
        _cached = settings
    _reset_statuses(settings)
    return settings


def get() -> ProxySettings:
    """The cached settings.

    Falls back to defaults when :func:`load` has not run yet (tests, or a call
    that races startup) so no caller ever has to handle ``None``.
    """
    return _cached if _cached is not None else ProxySettings()


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


async def save(settings: ProxySettings) -> ProxySettings:
    """Persist ``settings`` atomically and update the cache."""
    global _cached
    settings = _unmask(settings)
    payload = json.dumps(settings.model_dump(), indent=2, sort_keys=True)
    async with _lock:
        _atomic_write(config.SETTINGS_FILE, payload)
        _cached = settings
    _reset_statuses(settings)
    return settings


def reset_cache() -> None:
    """Drop the cache (tests)."""
    global _cached
    _cached = None
