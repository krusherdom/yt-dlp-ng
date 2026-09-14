# Changelog

## v0.1.0 - 2026-09-15
Initial release.

### Added
- FastAPI backend with a persistent SQLite-backed download queue, WebSocket live progress/log streaming, and a worker pool with configurable concurrency (`MAX_CONCURRENT`).
- Format/quality presets (`best`, `1080p`, `720p`, `480p`, `audio-mp3`, `audio-m4a`) plus a custom advanced-args field.
- Playlist/channel support: fast enumeration, per-item child jobs with individual progress, and `download_archive` de-duplication for playlist items (single URLs always download).
- Bulk import of links from pasted text or an uploaded HTML (bookmarks) file, with per-URL extractor validation before enqueueing.
- `cookies.txt` support for authenticated downloads, auto-detected from `/config`.
- Auto-resume of interrupted/queued jobs on container restart (`RESUME_ON_START`).
- In-app yt-dlp updater (Settings tab button and `UPDATE_ON_START` env var).
- Vanilla JS dark-themed UI: Queue, Import, Logs, and Settings tabs, responsive down to phone width.
- Docker image (`python:3.12-slim` + `ffmpeg` + `gosu`) that runs as a configurable non-root user (`PUID`/`PGID`/`UMASK`), with a `HEALTHCHECK` against `/api/health`.
- `docker-compose.yml` for local testing and an Unraid Community Applications template (`unraid/yt-dlp-ng.xml`).
