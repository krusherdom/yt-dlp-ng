---
name: yt-dlp-ng
slug: yt-dlp-docker
type: web-server
mode: on-demand
auto_start: false
platforms: [windows, linux]
description: Web frontend, queue and log viewer for yt-dlp, packaged as a Docker image for Unraid.
tags: [docker, yt-dlp, media, unraid]

launch:
  windows:
    cwd: .
    command: docker compose up --build
    shell: true
    detached: true
  linux:
    cwd: .
    command: docker compose up --build
    shell: true
    detached: true

stop:
  windows:
    command: docker compose down
  linux:
    command: docker compose down

health:
  type: http
  url: http://localhost:8080/api/health

last_verified: 2026-09-15
---

## What it is
A FastAPI + vanilla JS web UI around yt-dlp. Queue downloads into a chosen subfolder of a mounted
downloads directory, watch live progress and logs over WebSocket, import lists of links, and
resume after restarts. Built to run as a container on Unraid.

## Prerequisites
- Docker Desktop (Windows) or Docker Engine with the compose plugin.
- Port 8080 free.

## How to run (current best way)
```
docker compose up --build
```
Then open http://localhost:8080. Downloads land in `./downloads`, state in `./config`.

## Alternative launch modes
Run without Docker for development (needs Python 3.12 and ffmpeg on PATH):
```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
DOWNLOADS_ROOT=./downloads CONFIG_DIR=./config .venv/Scripts/python -m uvicorn app.main:app --port 8080 --reload
```
Tests: `.venv/Scripts/python -m pytest tests`.

## Where logs / state go
- Container stdout: `docker compose logs -f`.
- Per-job logs: `config/logs/<job_id>.log`. Queue DB: `config/jobs.db`. Download archive: `config/archive.txt`.

## How to stop
```
docker compose down
```

## Notes for re-use
- On Unraid use `unraid/yt-dlp-ng.xml` or map `/downloads` and `/config` manually with PUID 99 / PGID 100.
- Drop a Netscape-format `cookies.txt` into `config/` for age-restricted or member content.
