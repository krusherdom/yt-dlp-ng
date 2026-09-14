"""Job execution: thread pool, cancel flags, progress/log event fan-out.

Threading contract
------------------
Worker threads NEVER touch the database or the WebSocket clients. They only
push tuples onto an ``asyncio.Queue`` via ``loop.call_soon_threadsafe``. The
single ``broadcaster`` coroutine is the only writer: it applies state deltas to
SQLite, appends to the per-job log file and the global ring buffer, and fans
events out to every connected client.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from . import config, db, paths, ytdl

# --------------------------------------------------------------------------
# Module state
# --------------------------------------------------------------------------

_loop: Optional[asyncio.AbstractEventLoop] = None
_events: Optional[asyncio.Queue] = None
_pool: Optional[ThreadPoolExecutor] = None
#: Separate single-thread pool so a playlist enumeration never waits behind
#: two long downloads occupying the whole MAX_CONCURRENT pool.
_extract_pool: Optional[ThreadPoolExecutor] = None
_broadcaster_task: Optional[asyncio.Task] = None

_cancel_flags: Dict[str, threading.Event] = {}
_futures: Dict[str, Future] = {}
_dispatch_tasks: Set[asyncio.Task] = set()

clients: Set[Any] = set()
log_ring: Deque[str] = deque(maxlen=config.LOG_RING_SIZE)
#: Recently deleted job ids, so late events from a still-unwinding thread do
#: not resurrect the row's log file. Bounded to stay small.
_deleted_ids: Deque[str] = deque(maxlen=512)

TERMINAL = ("done", "failed", "cancelled")


class JobCancelled(Exception):
    """Raised inside a worker thread to unwind ``YoutubeDL.download()``.

    Subclasses yt-dlp's ``DownloadCancelled`` when available so that yt-dlp
    re-raises it unwrapped instead of swallowing it as a download error.
    """


try:  # pragma: no cover - depends on installed yt-dlp
    from yt_dlp.utils import DownloadCancelled as _DownloadCancelled

    class JobCancelled(_DownloadCancelled):  # type: ignore[no-redef] # noqa: F811
        """Raised inside a worker thread to unwind ``YoutubeDL.download()``."""

except Exception:  # pragma: no cover
    pass


# --------------------------------------------------------------------------
# Event plumbing (thread -> loop)
# --------------------------------------------------------------------------


def _emit(kind: str, job_id: Optional[str], payload: Any) -> None:
    """Push an event onto the asyncio queue from any thread."""
    if _loop is None or _events is None:
        return
    try:
        _loop.call_soon_threadsafe(_events.put_nowait, (kind, job_id, payload))
    except RuntimeError:
        pass  # loop already closed during shutdown


def emit_job(job_id: str, **fields: Any) -> None:
    _emit("job", job_id, fields)


def emit_log(job_id: Optional[str], line: str) -> None:
    if not line:
        return
    for part in str(line).splitlines() or [str(line)]:
        _emit("log", job_id, part)


# --------------------------------------------------------------------------
# yt-dlp glue
# --------------------------------------------------------------------------


class JobLogger:
    """yt-dlp logger that forwards lines as events and honours the cancel flag."""

    def __init__(self, job_id: str, cancel: threading.Event) -> None:
        self.job_id = job_id
        self.cancel = cancel

    def _check(self) -> None:
        # Progress hooks only fire while downloading; checking here also cancels
        # during extraction and post-processing.
        if self.cancel.is_set():
            raise JobCancelled("cancelled by user")

    def debug(self, msg: str) -> None:
        self._check()
        text = str(msg)
        if text.startswith("[debug] "):
            return
        emit_log(self.job_id, text)

    def info(self, msg: str) -> None:
        self._check()
        emit_log(self.job_id, str(msg))

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        emit_log(self.job_id, f"WARNING: {msg}")

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        emit_log(self.job_id, f"ERROR: {msg}")


def _fmt_bytes(n: Optional[float]) -> str:
    if not n:
        return ""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TiB"


def _fmt_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 0:
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:d}:{sec:02d}"


def _make_progress_hook(job_id: str, cancel: threading.Event):
    state = {"last": 0.0, "pct": -1.0}

    def hook(d: Dict[str, Any]) -> None:
        if cancel.is_set():
            raise JobCancelled("cancelled by user")

        status = d.get("status")
        if status == "finished":
            emit_job(
                job_id,
                progress=100.0,
                speed=None,
                eta=None,
                filename=Path(d.get("filename") or "").name or None,
            )
            emit_log(job_id, f"[download] finished {Path(d.get('filename') or '').name}")
            return
        if status == "error":
            return
        if status != "downloading":
            return

        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        pct = (done / total * 100.0) if total else 0.0
        pct = max(0.0, min(100.0, pct))

        now = time.monotonic()
        # Throttle: at most ~2 updates/sec, and only on a meaningful change.
        if now - state["last"] < 0.5 and abs(pct - state["pct"]) < 1.0:
            return
        state["last"] = now
        state["pct"] = pct

        speed = d.get("speed")
        emit_job(
            job_id,
            progress=round(pct, 2),
            speed=(f"{_fmt_bytes(speed)}/s" if speed else None),
            eta=_fmt_eta(d.get("eta")) or None,
            filename=Path(d.get("filename") or "").name or None,
        )

    return hook


# --------------------------------------------------------------------------
# Thread-side run functions
# --------------------------------------------------------------------------


def _run_download(job: Dict[str, Any]) -> None:
    """Executed in a worker thread. Emits events only."""
    import yt_dlp

    job_id = job["id"]
    cancel = _cancel_flags.setdefault(job_id, threading.Event())

    if cancel.is_set():
        emit_job(job_id, status="cancelled")
        return

    try:
        target = paths.safe_resolve(job.get("subfolder") or "")
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        emit_job(job_id, status="failed", error=f"Bad destination: {exc}")
        return

    logger = JobLogger(job_id, cancel)
    try:
        opts = ytdl.build_opts(
            preset=job.get("preset") or ytdl.DEFAULT_PRESET,
            target_dir=target,
            extra_args=job.get("extra_args") or "",
            progress_hooks=[_make_progress_hook(job_id, cancel)],
            logger=logger,
            # Only playlist children consult the shared archive, so re-adding a
            # playlist skips items already fetched. Single-URL jobs always
            # download, otherwise a second request with a different preset or
            # folder would silently report "done" with no file.
            use_archive=bool(job.get("parent_id")),
        )
    except ytdl.ExtraArgsError as exc:
        emit_job(job_id, status="failed", error=str(exc))
        return

    emit_job(job_id, status="running", error=None, progress=job.get("progress") or 0.0)
    emit_log(job_id, f"[job] starting {job.get('url')} (preset={job.get('preset')})")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=True)
        title = None
        filename = None
        if isinstance(info, dict):
            title = info.get("title")
            req = info.get("requested_downloads") or []
            if req:
                filename = Path(req[0].get("filepath") or req[0].get("filename") or "").name
            if not filename and info.get("_filename"):
                filename = Path(info["_filename"]).name
        emit_job(
            job_id,
            status="done",
            progress=100.0,
            speed=None,
            eta=None,
            error=None,
            **({"title": title} if title else {}),
            **({"filename": filename} if filename else {}),
        )
        emit_log(job_id, "[job] done")
    except JobCancelled:
        emit_job(job_id, status="cancelled", speed=None, eta=None)
        emit_log(job_id, "[job] cancelled")
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        emit_job(job_id, status="failed", error=message[:2000], speed=None, eta=None)
        emit_log(job_id, f"[job] failed: {message}")
    finally:
        _cancel_flags.pop(job_id, None)


def _run_flat_extract(job: Dict[str, Any]) -> Dict[str, Any]:
    """Flat enumeration pass. Returns {'entries': [...]} or {'info': {...}}."""
    import yt_dlp

    job_id = job["id"]
    cancel = _cancel_flags.setdefault(job_id, threading.Event())
    logger = JobLogger(job_id, cancel)

    target = config.DOWNLOADS_ROOT
    opts = ytdl.build_opts(
        preset=job.get("preset") or ytdl.DEFAULT_PRESET,
        target_dir=target,
        extra_args=None,
        flat=True,
        logger=logger,
    )

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(job["url"], download=False)

    if not isinstance(info, dict):
        return {"info": {}}

    raw_entries = info.get("entries")
    if raw_entries is None:
        return {"info": info}

    entries: List[Dict[str, Any]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url") or entry.get("webpage_url") or entry.get("original_url")
        if not url:
            vid = entry.get("id")
            if vid and entry.get("ie_key") == "Youtube":
                url = f"https://www.youtube.com/watch?v={vid}"
        if not url:
            continue
        entries.append({"url": url, "title": entry.get("title")})

    return {"info": info, "entries": entries}


# --------------------------------------------------------------------------
# Async dispatch
# --------------------------------------------------------------------------


def _track(task: asyncio.Task) -> None:
    _dispatch_tasks.add(task)
    task.add_done_callback(_dispatch_tasks.discard)


async def submit(job: Dict[str, Any]) -> None:
    """Queue a job. Playlist enumeration happens off the request path."""
    if job.get("type") == "child" or job.get("type") == "playlist":
        _submit_download(job)
        return
    _track(asyncio.create_task(_dispatch_single(job)))


def _submit_download(job: Dict[str, Any]) -> None:
    job_id = job["id"]
    if _pool is None:
        return
    if job.get("type") == "playlist":
        return  # parents never download; their children do
    _cancel_flags[job_id] = threading.Event()
    future = _pool.submit(_run_download, dict(job))
    _futures[job_id] = future
    future.add_done_callback(lambda _f, jid=job_id: _futures.pop(jid, None))


async def _dispatch_single(job: Dict[str, Any]) -> None:
    """Flat-extract first; split into parent + children if it is a playlist."""
    job_id = job["id"]
    assert _loop is not None
    _cancel_flags.setdefault(job_id, threading.Event())
    try:
        result = await _loop.run_in_executor(_extract_pool, _run_flat_extract, dict(job))
    except Exception as exc:
        # Enumeration failed (private/unsupported/offline). Try downloading it
        # directly rather than failing outright.
        emit_log(job_id, f"[job] enumeration failed, downloading directly: {exc}")
        _submit_download(job)
        return

    if _cancel_flags.get(job_id, threading.Event()).is_set():
        emit_job(job_id, status="cancelled")
        return

    entries = result.get("entries")
    info = result.get("info") or {}

    if not entries:
        title = info.get("title") if isinstance(info, dict) else None
        if title:
            emit_job(job_id, title=title)
        _submit_download(job)
        return

    playlist_title = info.get("title") or f"Playlist ({len(entries)} items)"
    await db.update_job(
        job_id, type="playlist", status="running", title=playlist_title, progress=0.0
    )
    parent = await db.get_job(job_id)
    if parent:
        await push_job(parent)
    emit_log(job_id, f"[job] playlist '{playlist_title}' with {len(entries)} entries")

    children = await db.create_jobs_bulk(
        [
            {
                "url": e["url"],
                "title": e.get("title"),
                "preset": job.get("preset") or ytdl.DEFAULT_PRESET,
                "subfolder": job.get("subfolder") or "",
                "extra_args": job.get("extra_args") or "",
                "parent_id": job_id,
                "job_type": "child",
                "status": "queued",
            }
            for e in entries
        ]
    )
    for child in children:
        await push_job(child)
        _submit_download(child)


async def cancel(job_id: str) -> Optional[Dict[str, Any]]:
    """Cancel a job (and its children if it is a playlist parent)."""
    job = await db.get_job(job_id)
    if job is None:
        return None

    if job.get("type") == "playlist":
        for child in await db.list_children(job_id):
            if child["status"] not in TERMINAL:
                await cancel(child["id"])
        updated = await db.update_job(job_id, status="cancelled", speed=None, eta=None)
        if updated:
            await push_job(updated)
        return updated

    flag = _cancel_flags.get(job_id)
    if flag is not None:
        flag.set()

    future = _futures.get(job_id)
    started = True
    if future is not None and future.cancel():
        started = False  # never left the queue
        _futures.pop(job_id, None)
        _cancel_flags.pop(job_id, None)

    if not started or job["status"] == "queued":
        updated = await db.update_job(job_id, status="cancelled", speed=None, eta=None)
        if updated:
            await push_job(updated)
            await _refresh_parent(updated.get("parent_id"))
        return updated

    return job  # the running thread will report 'cancelled' itself


async def retry(job_id: str) -> Optional[Dict[str, Any]]:
    """Reset a job to queued and resubmit it. Parents retry their children."""
    job = await db.get_job(job_id)
    if job is None:
        return None

    if job.get("type") == "playlist":
        children = await db.list_children(job_id)
        retried = 0
        for child in children:
            if child["status"] in ("failed", "cancelled"):
                await retry(child["id"])
                retried += 1
        updated = await db.update_job(
            job_id, status="running" if retried else job["status"], error=None
        )
        if updated:
            await push_job(updated)
        await _refresh_parent(job_id)
        return updated

    if job["status"] not in TERMINAL:
        # Already queued or running: resubmitting would start a second thread
        # against the same .part file and orphan the first one's cancel flag.
        return job

    _cancel_flags.pop(job_id, None)
    updated = await db.update_job(
        job_id, status="queued", error=None, progress=0.0, speed=None, eta=None
    )
    if updated:
        await push_job(updated)
        _submit_download(updated)
        await _refresh_parent(updated.get("parent_id"))
    return updated


async def remove(job_id: str) -> bool:
    """Cancel if needed, then delete the row(s) and log file(s)."""
    job = await db.get_job(job_id)
    if job is None:
        return False

    child_ids = [c["id"] for c in await db.list_children(job_id)]
    if job["status"] not in TERMINAL:
        await cancel(job_id)
    for cid in child_ids:
        _cancel_flags.pop(cid, None)
    _cancel_flags.pop(job_id, None)

    await db.delete_job(job_id)
    for jid in [job_id] + child_ids:
        _deleted_ids.append(jid)
        log_path = config.LOG_DIR / f"{jid}.log"
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass
    await _broadcast({"type": "deleted", "job_id": job_id, "child_ids": child_ids})
    return True


# --------------------------------------------------------------------------
# Broadcaster (single writer)
# --------------------------------------------------------------------------


def _append_log_file(job_id: Optional[str], line: str) -> None:
    stamped = line
    log_ring.append(f"{job_id or '-'} | {stamped}" if job_id else stamped)
    if not job_id or job_id in _deleted_ids:
        # A cancelled job's thread emits a trailing line after the row and log
        # file are gone; writing it would recreate an orphan <id>.log.
        return
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LOG_DIR / f"{job_id}.log", "a", encoding="utf-8", errors="replace") as fh:
            fh.write(stamped + "\n")
    except OSError:
        pass


async def _broadcast(message: Dict[str, Any]) -> None:
    if not clients:
        return
    payload = json.dumps(message, default=str)
    dead = []
    for ws in list(clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def push_job(job: Dict[str, Any]) -> None:
    await _broadcast({"type": "job", "job": job})


async def _refresh_parent(parent_id: Optional[str]) -> None:
    """Recompute a playlist parent's progress from its children."""
    if not parent_id:
        return
    counts = await db.count_children_by_status(parent_id)
    total = sum(counts.values())
    if not total:
        return
    done = counts.get("done", 0)
    finished = done + counts.get("failed", 0) + counts.get("cancelled", 0)
    progress = round(done / total * 100.0, 2)

    if finished >= total:
        status = "done" if done == total else ("cancelled" if done == 0 and counts.get("cancelled") else "failed")
    else:
        status = "running"

    fields: Dict[str, Any] = {"progress": progress, "status": status}
    if finished >= total and done != total:
        fields["error"] = f"{done}/{total} items downloaded"
    parent = await db.update_job(parent_id, **fields)
    if parent:
        await push_job(parent)


async def broadcaster() -> None:
    """The only task that writes to SQLite, log files and WebSockets."""
    assert _events is not None
    while True:
        try:
            kind, job_id, payload = await _events.get()
        except asyncio.CancelledError:
            raise

        try:
            if kind == "log":
                _append_log_file(job_id, payload)
                await _broadcast({"type": "log", "job_id": job_id, "line": payload})
            elif kind == "job" and job_id:
                job = await db.update_job(job_id, **payload)
                if job:
                    await push_job(job)
                    if payload.get("status") in TERMINAL and job.get("parent_id"):
                        await _refresh_parent(job["parent_id"])
        except Exception as exc:  # never let the broadcaster die
            log_ring.append(f"- | [broadcaster] error: {exc}")
        finally:
            _events.task_done()


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


async def start() -> None:
    global _loop, _events, _pool, _extract_pool, _broadcaster_task
    _loop = asyncio.get_running_loop()
    _events = asyncio.Queue()
    _pool = ThreadPoolExecutor(
        max_workers=config.MAX_CONCURRENT, thread_name_prefix="ytdl"
    )
    _extract_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ytdl-extract")
    _broadcaster_task = asyncio.create_task(broadcaster())


async def stop() -> None:
    global _pool, _extract_pool, _broadcaster_task
    for flag in list(_cancel_flags.values()):
        flag.set()
    for task in list(_dispatch_tasks):
        task.cancel()
    if _broadcaster_task is not None:
        _broadcaster_task.cancel()
        try:
            await _broadcaster_task
        except (asyncio.CancelledError, Exception):
            pass
        _broadcaster_task = None
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
    if _extract_pool is not None:
        _extract_pool.shutdown(wait=False, cancel_futures=True)
        _extract_pool = None
    clients.clear()


async def resume_pending() -> int:
    """Requeue jobs left queued/running by a previous run."""
    await db.mark_interrupted_running()
    pending = await db.list_resumable()
    parents = []
    for job in pending:
        if job.get("type") == "child":
            _submit_download(job)
            pid = job.get("parent_id")
            if pid and pid not in parents:
                parents.append(pid)
        else:
            await submit(job)

    # mark_interrupted_running() also reset playlist parents to 'queued'; settle
    # them from their children instead of leaving a stale status behind.
    for pid in parents:
        await _refresh_parent(pid)
    for parent in await db.list_jobs():
        if parent.get("type") == "playlist" and parent["id"] not in parents:
            await _refresh_parent(parent["id"])

    return len(pending)
