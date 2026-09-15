"""Proxy pool: health checks, round-robin selection and the periodic checker.

Threading contract
------------------
``acquire()`` and ``check_sync()`` are synchronous and are called from the
download worker threads; everything they touch is guarded by ``_lock``
(a plain :class:`threading.Lock`, never held across a network call). The async
helpers (``check_all``/``check_one``/``start_background``) live on the event
loop and push the blocking checks into the default executor.

Health checks reuse yt-dlp's own networking stack, so SOCKS proxies work with
no extra dependency and behave exactly like they will during a download.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
from urllib.parse import urlsplit

from . import settings as settings_store
from .models import ProxyEntry, ProxySettings, ProxyStatus, normalise_domain

#: Informational only: the exit IP as seen by a third party. Best effort.
EXIT_IP_URL = "https://api.ipify.org?format=json"
EXIT_IP_TIMEOUT = 5.0

#: Callbacks invoked (awaited) with the full status list after every check
#: round. ``app.main`` appends a WebSocket broadcaster here.
on_change: List[Callable[[List[ProxyStatus]], Awaitable[None]]] = []

_lock = threading.Lock()
_statuses: Dict[str, ProxyStatus] = {}
_checked_at: Dict[str, float] = {}
_rr_index: int = 0

_bg_task: Optional["asyncio.Task"] = None


class NoLiveProxy(Exception):
    """No usable proxy for a host that must be proxied."""

    def __init__(self, host: str) -> None:
        super().__init__(f"No live proxy for {host}")
        self.host = host


def _log(line: str) -> None:
    """Best-effort line into the app's global log ring (Logs tab)."""
    try:
        from . import worker  # imported lazily: worker imports this module

        worker.log_ring.append(f"- | {line}")
    except Exception:  # pragma: no cover - logging must never break a check
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Routing decisions
# --------------------------------------------------------------------------


def host_matches(host: str, domains: List[str]) -> bool:
    """True when ``host`` is one of ``domains`` or a subdomain of one.

    Matching is on dot boundaries, so ``notxvideos.com`` never matches
    ``xvideos.com``. A leading ``www.`` on either side is ignored.
    """
    needle = normalise_domain(host)
    if not needle:
        return False
    for raw in domains or []:
        domain = normalise_domain(raw)
        if not domain:
            continue
        if needle == domain or needle.endswith("." + domain):
            return True
    return False


def _host_of(url: str) -> str:
    try:
        return (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def needs_proxy(url: str) -> bool:
    """Whether ``url`` must be routed through the pool, per the settings."""
    st = settings_store.get()
    if not st.proxies:
        return False
    if st.mode == "all":
        return True
    host = _host_of(url)
    if not host:
        return False
    return host_matches(host, st.domains)


# --------------------------------------------------------------------------
# Status table
# --------------------------------------------------------------------------


def reset_statuses_for(settings: ProxySettings) -> None:
    """Keep the status table in step with the configured proxies."""
    ids = [p.id for p in settings.proxies]
    keep = set(ids)
    with _lock:
        for gone in [pid for pid in _statuses if pid not in keep]:
            _statuses.pop(gone, None)
            _checked_at.pop(gone, None)
        for pid in ids:
            _statuses.setdefault(pid, ProxyStatus(id=pid))


def statuses() -> List[ProxyStatus]:
    """One status per configured proxy, in settings order."""
    entries = settings_store.get().proxies
    with _lock:
        return [
            (_statuses.get(p.id) or ProxyStatus(id=p.id)).model_copy(deep=True)
            for p in entries
        ]


def summary() -> dict:
    entries = settings_store.get().proxies
    with _lock:
        live = sum(
            1
            for p in entries
            if p.enabled and (_statuses.get(p.id) or ProxyStatus(id=p.id)).live
        )
    return {
        "configured": len(entries),
        "enabled": sum(1 for p in entries if p.enabled),
        "live": live,
    }


def _store(status: ProxyStatus) -> None:
    if not status.id:
        return
    with _lock:
        _statuses[status.id] = status
        _checked_at[status.id] = time.monotonic()


def _is_stale(proxy_id: str, max_age_s: int) -> bool:
    with _lock:
        status = _statuses.get(proxy_id)
        checked = _checked_at.get(proxy_id)
    if status is None or status.live is None or checked is None:
        return True
    return (time.monotonic() - checked) >= max_age_s


# --------------------------------------------------------------------------
# Health checks
# --------------------------------------------------------------------------


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    message = text[0] if text else ""
    return (message or exc.__class__.__name__)[:200]


def _exit_ip(url: str) -> Optional[str]:
    """Look up the apparent exit IP through ``url``. Never raises."""
    try:
        import yt_dlp
        from yt_dlp.networking import Request

        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
        try:
            resp = ydl.urlopen(
                Request(
                    EXIT_IP_URL,
                    proxies={"all": url},
                    extensions={"timeout": EXIT_IP_TIMEOUT},
                )
            )
            try:
                import json

                data = json.loads(resp.read().decode("utf-8", "replace"))
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            ip = data.get("ip") if isinstance(data, dict) else None
            return str(ip)[:64] if ip else None
        finally:
            try:
                ydl.close()
            except Exception:
                pass
    except Exception:
        return None


def check_sync(
    entry_or_url: Any,
    test_url: Optional[str] = None,
    timeout_s: Optional[int] = None,
) -> ProxyStatus:
    """Fetch ``test_url`` through one proxy. Blocking; safe in any thread.

    Accepts a :class:`~app.models.ProxyEntry` (the result is stored in the
    status table) or a bare URL string (``ProxyStatus.id`` comes back empty,
    which is what the "test before adding" endpoint wants).
    """
    st = settings_store.get()
    if isinstance(entry_or_url, ProxyEntry):
        proxy_id = entry_or_url.id
        url = entry_or_url.url
    else:
        proxy_id = ""
        url = str(entry_or_url or "").strip()

    target = (test_url or st.test_url or "").strip()
    timeout = float(timeout_s or st.timeout_s)

    status = ProxyStatus(id=proxy_id, last_checked=_now_iso())
    if not url:
        status.live = False
        status.last_error = "Empty proxy URL"
        _store(status)
        return status

    import yt_dlp
    from yt_dlp.networking import Request

    ydl = None
    resp = None
    started = time.perf_counter()
    try:
        ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
        # Timed from here: building YoutubeDL costs ~seconds on the first call
        # and would otherwise be reported to the UI as proxy latency.
        started = time.perf_counter()
        resp = ydl.urlopen(
            Request(target, proxies={"all": url}, extensions={"timeout": timeout})
        )
        code = int(getattr(resp, "status", 0) or 0)
        status.latency_ms = int((time.perf_counter() - started) * 1000)
        status.live = code < 400
        if not status.live:
            status.last_error = f"HTTP {code} from test URL (the proxy works but the test site rejected it; try another Test URL under Advanced)"
    except Exception as exc:
        status.latency_ms = int((time.perf_counter() - started) * 1000)
        status.live = False
        status.last_error = _short(exc)
    finally:
        for closeable in (resp, ydl):
            try:
                if closeable is not None:
                    closeable.close()
            except Exception:
                pass

    status.last_checked = _now_iso()
    if status.live:
        status.exit_ip = _exit_ip(url)

    _store(status)
    return status


async def _notify() -> None:
    if not on_change:
        return
    current = statuses()
    for callback in list(on_change):
        try:
            await callback(current)
        except Exception as exc:  # a dead listener must not kill the checker
            _log(f"[proxy] on_change callback failed: {exc}")


async def check_all() -> List[ProxyStatus]:
    """Check every enabled proxy concurrently, then notify listeners."""
    st = settings_store.get()
    reset_statuses_for(st)
    enabled = [p for p in st.proxies if p.enabled]
    if enabled:
        loop = asyncio.get_running_loop()
        await asyncio.gather(
            *(
                loop.run_in_executor(None, check_sync, entry, st.test_url, st.timeout_s)
                for entry in enabled
            ),
            return_exceptions=True,
        )
    result = statuses()
    await _notify()
    return result


async def check_one(proxy_id: str) -> ProxyStatus:
    """Re-check a single configured proxy. Raises ``KeyError`` if unknown."""
    st = settings_store.get()
    entry = next((p for p in st.proxies if p.id == proxy_id), None)
    if entry is None:
        raise KeyError(proxy_id)
    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(
        None, check_sync, entry, st.test_url, st.timeout_s
    )
    await _notify()
    return status


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def acquire(url: str, exclude: Optional[Set[str]] = None) -> Optional[ProxyEntry]:
    """Pick the proxy for ``url``, or ``None`` when it should go direct.

    Blocking: unchecked or stale candidates are health-checked inline, which is
    fine because this only ever runs on a worker thread.

    Raises :class:`NoLiveProxy` when the URL must be proxied but no enabled
    proxy is alive -- failing the job beats silently leaking the real IP to a
    site the user explicitly routed through the pool.
    """
    if not needs_proxy(url):
        return None

    st = settings_store.get()
    skip = set(exclude or ())
    host = _host_of(url) or url
    candidates = [p for p in st.proxies if p.enabled and p.id not in skip]

    for entry in candidates:
        if _is_stale(entry.id, st.check_interval_s):
            check_sync(entry, st.test_url, st.timeout_s)

    global _rr_index
    with _lock:
        live = [p for p in candidates if (_statuses.get(p.id) or ProxyStatus(id=p.id)).live]
        if not live:
            raise NoLiveProxy(host)
        chosen = live[_rr_index % len(live)]
        _rr_index = (_rr_index + 1) % 1_000_000
    return chosen


def describe(entry: Optional[ProxyEntry]) -> Optional[str]:
    """Human label for a chosen proxy: its name, else its masked URL."""
    if entry is None:
        return None
    return entry.label or settings_store.mask_url(entry.url)


# --------------------------------------------------------------------------
# Background checker
# --------------------------------------------------------------------------


async def _periodic() -> None:
    while True:
        try:
            await check_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            _log(f"[proxy] periodic check failed: {exc}")
        interval = max(30, int(settings_store.get().check_interval_s or 300))
        await asyncio.sleep(interval)


async def start_background(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Start the periodic checker. Returns immediately (never blocks startup)."""
    global _bg_task
    await stop_background()
    runner = loop or asyncio.get_running_loop()
    _bg_task = runner.create_task(_periodic())


async def stop_background() -> None:
    global _bg_task
    task = _bg_task
    _bg_task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def reset_state() -> None:
    """Drop all cached health (tests)."""
    global _rr_index
    with _lock:
        _statuses.clear()
        _checked_at.clear()
        _rr_index = 0
