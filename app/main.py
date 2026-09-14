"""FastAPI application: REST API, WebSocket fan-out, static UI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, importer, paths, worker, ytdl
from .models import BulkJobCreate, FolderCreate, JobCreate


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    await db.init_db()
    await worker.start()
    if config.RESUME_ON_START:
        try:
            count = await worker.resume_pending()
            if count:
                worker.log_ring.append(f"- | [startup] requeued {count} job(s)")
        except Exception as exc:  # pragma: no cover
            worker.log_ring.append(f"- | [startup] resume failed: {exc}")
    else:
        await db.mark_interrupted_running()
    worker.log_ring.append(
        f"- | [startup] downloads={config.DOWNLOADS_ROOT} config={config.CONFIG_DIR} "
        f"max_concurrent={config.MAX_CONCURRENT}"
    )
    try:
        yield
    finally:
        await worker.stop()
        await db.close_db()


app = FastAPI(title="yt-dlp Web", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# Health / status
# --------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True}


@app.get("/api/status")
async def status() -> Dict[str, Any]:
    return {
        "ytdlp_version": ytdl.ytdlp_version(),
        "cookies_detected": config.cookies_detected(),
        "max_concurrent": config.MAX_CONCURRENT,
        "downloads_root": str(config.DOWNLOADS_ROOT),
        "config_dir": str(config.CONFIG_DIR),
        "resume_on_start": config.RESUME_ON_START,
        "presets": ytdl.PRESET_NAMES,
    }


@app.get("/api/presets")
async def presets() -> Dict[str, Any]:
    return {"presets": ytdl.PRESET_NAMES, "default": ytdl.DEFAULT_PRESET}


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


@app.get("/api/jobs")
async def get_jobs(status: Optional[str] = Query(None)) -> Dict[str, Any]:
    return {"jobs": await db.list_jobs(status)}


def _validate_preset(preset: Optional[str]) -> str:
    name = (preset or ytdl.DEFAULT_PRESET).strip()
    if name not in ytdl.PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown preset '{name}'")
    return name


def _validate_extra_args(extra_args: Optional[str]) -> str:
    text = (extra_args or "").strip()
    if not text:
        return ""
    try:
        ytdl.parse_extra_args(text)
    except ytdl.ExtraArgsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return text


def _validate_url(url: Optional[str]) -> str:
    value = (url or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="URL required")
    if not value.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    return value


@app.post("/api/jobs/bulk", status_code=201)
async def create_jobs_bulk(payload: BulkJobCreate) -> Dict[str, Any]:
    preset = _validate_preset(payload.preset)
    extra_args = _validate_extra_args(payload.extra_args)
    target = paths.safe_resolve(payload.subfolder or "")
    subfolder = paths.relative_to_root(target)

    urls: List[str] = []
    seen = set()
    for raw in payload.urls or []:
        value = (raw or "").strip()
        if not value or value in seen:
            continue
        if not value.lower().startswith(("http://", "https://")):
            continue
        seen.add(value)
        urls.append(value)

    if not urls:
        raise HTTPException(status_code=400, detail="No valid URLs supplied")

    jobs = await db.create_jobs_bulk(
        [
            {
                "url": u,
                "preset": preset,
                "subfolder": subfolder,
                "extra_args": extra_args,
            }
            for u in urls
        ]
    )
    for job in jobs:
        await worker.push_job(job)
        await worker.submit(job)
    return {"jobs": jobs, "count": len(jobs)}


@app.post("/api/jobs", status_code=201)
async def create_job(payload: JobCreate) -> Dict[str, Any]:
    url = _validate_url(payload.url)
    preset = _validate_preset(payload.preset)
    extra_args = _validate_extra_args(payload.extra_args)
    target = paths.safe_resolve(payload.subfolder or "")
    subfolder = paths.relative_to_root(target)

    job = await db.create_job(
        url=url, preset=preset, subfolder=subfolder, extra_args=extra_args
    )
    await worker.push_job(job)
    await worker.submit(job)
    return {"job": job}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> Dict[str, Any]:
    job = await db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    children = await db.list_children(job_id) if job.get("type") == "playlist" else []
    return {"job": job, "children": children}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> Dict[str, Any]:
    job = await worker.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> Dict[str, Any]:
    job = await worker.retry(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> Dict[str, Any]:
    removed = await worker.remove(job_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
async def job_log(job_id: str) -> str:
    job = await db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    path = config.LOG_DIR / f"{job_id}.log"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read log: {exc}")


@app.get("/api/logs")
async def global_logs() -> Dict[str, Any]:
    return {"lines": list(worker.log_ring)}


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


@app.post("/api/import")
async def import_links(
    request: Request,
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
) -> Dict[str, Any]:
    payload = ""
    if file is not None:
        raw = await file.read()
        payload = raw.decode("utf-8", errors="replace")
    elif text:
        payload = text
    else:
        # The UI posts multipart, but accept a JSON body too for API clients.
        if "application/json" in (request.headers.get("content-type") or ""):
            try:
                body = await request.json()
            except Exception:
                body = None
            if isinstance(body, dict):
                payload = str(body.get("text") or "")

    if not payload.strip():
        raise HTTPException(status_code=400, detail="Provide text or an HTML file")
    return await asyncio.to_thread(importer.import_payload, payload)


# --------------------------------------------------------------------------
# Folders
# --------------------------------------------------------------------------


@app.get("/api/folders")
async def get_folders(path: Optional[str] = Query(None)) -> Dict[str, Any]:
    listing = paths.list_folders(path)[0]
    return listing


@app.post("/api/folders", status_code=201)
async def post_folder(payload: FolderCreate) -> Dict[str, Any]:
    created = paths.create_folder(payload.path)
    return {"path": created}


# --------------------------------------------------------------------------
# yt-dlp self-update
# --------------------------------------------------------------------------


def _installed_version() -> str:
    """Read the on-disk version in a fresh interpreter.

    ``importlib.metadata`` in this process caches the version that was imported
    at startup, so a subprocess is the only reliable reading after an upgrade.
    """
    try:
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib.metadata as m; print(m.version('yt-dlp'))",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _do_update() -> Dict[str, Any]:
    old = ytdl.ytdlp_version()
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    new = _installed_version()
    return {
        "ok": proc.returncode == 0,
        "old_version": old,
        "new_version": new,
        "changed": proc.returncode == 0 and new != old,
        "returncode": proc.returncode,
        "output": (proc.stdout + proc.stderr)[-4000:],
        "note": (
            "The running process keeps the previously imported yt-dlp version "
            "until the container/app is restarted."
        ),
    }


@app.post("/api/update-ytdlp")
async def update_ytdlp() -> Dict[str, Any]:
    result = await asyncio.to_thread(_do_update)
    for line in (result.get("output") or "").splitlines()[-20:]:
        worker.log_ring.append(f"- | [update] {line}")
    if not result["ok"]:
        return JSONResponse(status_code=500, content=result)
    return result


# --------------------------------------------------------------------------
# WebSocket (registered before the static mount so StaticFiles cannot claim it)
# --------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    worker.clients.add(ws)
    try:
        jobs = await db.list_jobs()
        await ws.send_text(
            json.dumps(
                {
                    "type": "snapshot",
                    "jobs": jobs,
                    "logs": list(worker.log_ring)[-200:],
                    "status": {
                        "ytdlp_version": ytdl.ytdlp_version(),
                        "cookies_detected": config.cookies_detected(),
                        "max_concurrent": config.MAX_CONCURRENT,
                        "downloads_root": str(config.DOWNLOADS_ROOT),
                    },
                },
                default=str,
            )
        )
        while True:
            # Clients do not send commands; this keeps the socket alive and
            # detects disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        worker.clients.discard(ws)
        with contextlib.suppress(Exception):
            await ws.close()


# --------------------------------------------------------------------------
# Static UI (mounted LAST so /api and /ws win; tolerates a missing static/)
# --------------------------------------------------------------------------

if os.path.isdir(config.STATIC_DIR):
    app.mount(
        "/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static"
    )
else:  # pragma: no cover - only until the frontend lands

    @app.get("/")
    async def missing_ui() -> Dict[str, Any]:
        return {
            "ok": True,
            "detail": f"static/ not found at {config.STATIC_DIR}; API is available under /api",
        }
