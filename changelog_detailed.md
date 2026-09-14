
## Backend

FastAPI + yt-dlp backend (Python 3.12). Owns `app/`, `requirements.txt`, `tests/`.

### Modules
- **`app/config.py`** — env config read once at import: `DOWNLOADS_ROOT` (`/downloads`), `CONFIG_DIR` (`/config`), `MAX_CONCURRENT` (2), `RESUME_ON_START` (true), `PORT` (8080). All paths `.resolve()`d immediately so relative values like `./downloads` work for local Windows testing and `is_relative_to` comparisons stay absolute-vs-absolute. Derives `jobs.db`, `logs/`, `cookies.txt`, `archive.txt`, and resolves `static/` from `__file__` rather than cwd.
- **`app/paths.py`** — `safe_resolve()` rejects `..` traversal, absolute paths, Windows drive specs and UNC paths with HTTP 400; `list_folders()`, `create_folder()`, `relative_to_root()`. Optional `root=` argument so tests do not need env vars.
- **`app/db.py`** — aiosqlite, WAL + `synchronous=NORMAL`. `jobs` table (uuid4 pk, url, title, preset, subfolder, extra_args, status, progress, speed, eta, filename, error, parent_id, type, created_at, updated_at) with indexes on status/parent/created. Async CRUD, bulk insert, child queries, and an `asyncio.Lock` around every write.
- **`app/ytdl.py`** — presets `best`, `1080p`, `720p`, `480p`, `audio-mp3`, `audio-m4a`; outtmpl `%(title)s [%(id)s].%(ext)s`; `continuedl`, `noprogress`, `ignoreerrors=False`, `download_archive=<config>/archive.txt`, `cookiefile` when `cookies.txt` exists. `parse_extra_args()` shlex-splits the advanced textbox through `yt_dlp.parse_options` and diffs against a cached baseline parse, so a user's extra args cannot silently reset `format`/`outtmpl`; postprocessors merge additively.
- **`app/importer.py`** — URL regex for text, `html.parser` href/src collection for HTML (Netscape bookmark exports included), trailing-punctuation trim, order-preserving dedupe, then validation against `gen_extractor_classes()` with `GenericIE` and non-working extractors excluded. Returns candidates `[{url, extractor}]` plus the rejected list. Extractor list cached at module import.
- **`app/worker.py`** — `ThreadPoolExecutor(MAX_CONCURRENT)` for downloads, per-job `threading.Event` cancel flag, `JobCancelled(DownloadCancelled)` raised from both the progress hook and the custom logger so cancels also unwind during extraction/post-processing. Worker threads never touch SQLite or WebSockets; they only `loop.call_soon_threadsafe` events onto an `asyncio.Queue`. A single `broadcaster` coroutine is the sole writer: it applies deltas to the db, appends `<CONFIG_DIR>/logs/<id>.log`, fills a 2000-line global deque, and fans out to all clients. Progress is computed numerically from `downloaded_bytes/total_bytes` and throttled to ~2 updates/sec.
- **`app/models.py`** — pydantic request/response models.
- **`app/main.py`** — lifespan startup (dirs, db init, worker pool, broadcaster, requeue when `RESUME_ON_START`), all `/api` routes, `WS /ws`, then `static/` mounted at `/` with `html=True` — guarded by `os.path.isdir` so a missing `static/` cannot crash startup.

### Behaviour
- **Playlists** — every submitted job first runs a flat (`extract_flat='in_playlist'`) enumeration pass. If entries come back, the job is flipped to `type=playlist` and one `type=child` job is created per entry (`entry['url']` / `webpage_url`, with a YouTube id fallback). Parent progress = done children / total; parent settles to done/failed/cancelled when every child is terminal. Retry on a parent re-queues only its failed/cancelled children; delete cascades to child rows and their log files. Startup resume deliberately skips playlist parents so children are never duplicated.
- **Cancel** — sets the event and also calls `Future.cancel()`, so a job that has not left the queue is cancelled without ever occupying a thread.
- **Statuses** — `queued`, `running`, `done`, `failed`, `cancelled`.
- **WebSocket** — sends a `snapshot` of all jobs (plus recent log lines and status) on connect, then `{"type":"job","job":{…}}` deltas, `{"type":"log","job_id":…,"line":…}` lines, and `{"type":"deleted",…}`.

### API
`GET /api/health` · `GET /api/status` · `GET /api/presets` · `GET|POST /api/jobs` · `POST /api/jobs/bulk` · `GET /api/jobs/{id}` · `POST /api/jobs/{id}/cancel|retry` · `DELETE /api/jobs/{id}` · `GET /api/jobs/{id}/log` · `GET /api/logs` · `POST /api/import` (text or file) · `GET|POST /api/folders` · `POST /api/update-ytdlp` · `WS /ws`

`POST /api/update-ytdlp` shells out to `[sys.executable, -m, pip, install, -U, yt-dlp]` and reads the new version from a fresh subprocess (in-process `importlib.metadata` still reports the loaded version), returning `old_version`/`new_version` plus a note that the running process keeps the old version until restart.

### Tests
`tests/` — 62 pytest cases covering importer (mixed valid/invalid text, bookmark HTML, page HTML, dedupe, GenericIE exclusion), paths (traversal, absolute/UNC/drive rejection, create/list) and ytdl (preset mapping, build_opts, extra-args merge isolation). All passing.

### Deviations from the plan
- Flat enumeration runs on a dedicated single-thread executor instead of the shared download pool, so adding a playlist does not wait behind `MAX_CONCURRENT` long downloads.
- `safe_resolve` rejects absolute subfolder input (`/etc`, `C:\Windows`) with a 400 rather than silently reinterpreting it as relative.
- If enumeration fails (private/offline/unsupported), the job falls back to a direct download attempt instead of failing immediately.
- The advanced `extra_args` box cannot override the destination: `paths` (`-P`) is dropped and a custom `outtmpl` (`-o`) is re-rooted under the job's resolved subfolder, with absolute and `..`-containing templates discarded. Without this, `-P /etc` would have written outside `DOWNLOADS_ROOT` and broken the plan's path invariant.
- `windowsfilenames: True` is set so titles stay portable across SMB shares on Unraid; it rewrites characters like `:` to `#` in output names.
- Retry is a no-op on a job that is already `queued` or `running`, so it cannot start a second thread against the same `.part` file.

## Frontend

Vanilla HTML/CSS/JS single-page UI served by FastAPI's `StaticFiles` mount at `/`. No build step, no framework, no CDN — three files only.

### Files
- **`static/index.html`** — page shell: header (app name, yt-dlp version pill, cookies badge, WebSocket status dot), four tabs (Queue / Import / Logs / Settings), the shared subfolder-picker `<dialog>`, a toast container, and three `<template>` elements (job row, import candidate, folder row). Templates are static author-controlled markup, so cloning them never routes untrusted data through `innerHTML`. Assets are referenced relatively with a small inline fallback that retries from `/static/` if the page is ever served from `/` with assets mounted elsewhere.
- **`static/style.css`** — dark theme built on CSS custom properties (`--bg`, `--panel`, `--accent`, per-status colours). Sticky header/tabs, card surfaces, status badges, progress bars, nested child rows, modal dialog, toasts. One `@media (max-width: 560px)` block stacks the add bar, spreads the tab strip and widens job action buttons; verified with no horizontal overflow at a 400px viewport. Honours `prefers-reduced-motion`.
- **`static/app.js`** — the whole application in one IIFE (~800 lines, commented by section).

### Architecture
- State lives in plain `Map`/`Set` structures (`jobs`, `rowEls`, `expanded`, `kidsOpen`), never in the DOM. The DOM is a projection that is **patched in place**: a keyed reconcile moves existing row elements instead of rebuilding the list, so open log drawers, scroll positions and focus survive the progress-event storm yt-dlp produces. Renders are coalesced into one `requestAnimationFrame`.
- Every server- or user-supplied string reaches the DOM through `textContent` or a text node. `innerHTML` is never used with untrusted data (verified: a log line containing `<script>` renders as literal text).
- WebSocket with exponential backoff (1s → 30s, reset on open) and a 30s keepalive ping; the connection dot shows live / retrying / offline. A `snapshot` replaces job state wholesale and also seeds status and the log tail. `job` upserts, `deleted` prunes the job and its `child_ids`, `log` fans out to both the global log and the matching job drawer.
- REST seeding on load (`/api/status`, `/api/presets`, `/api/logs`, `/api/jobs`) so the UI is populated before the socket snapshot arrives.
- Toasts for every error path; the `api()` helper unwraps FastAPI's `{"detail": …}` (and `error`/`output`) into the message.

### Tabs
- **Queue** — add bar (URL input with Enter-to-add, preset select, subfolder picker button, collapsible extra-args field, Add). A whitespace-separated multi-URL paste is routed to `/api/jobs/bulk` automatically. Job rows show title (falling back to URL), status badge, progress bar, percent/speed/ETA, preset and subfolder tags, output filename, error box, and cancel/retry/delete buttons gated by status. Playlist parents show a `▸ N items` toggle and nest children in an indented collapsible container with a `done/total` count. A per-row expand toggle opens the log drawer, which fetches `/api/jobs/{id}/log` once and then appends live WS lines (trimmed to 800 lines). Filter chips all/active/done/failed (failed also covers cancelled) and a "Clear finished" button that treats a 404 as success, since parent deletion cascades to children.
- **Import** — textarea and `.html` file input posted as `multipart/form-data` (no explicit `Content-Type`, so the browser sets the boundary). Candidates render as a checkbox list with extractor tags, all checked by default, plus a rejected count, All/None buttons, preset + subfolder picker, and an "Add N selected" button wired to `/api/jobs/bulk`.
- **Logs** — global live log, one `<div>` per line prefixed with the 8-char job id, capped at 2000 lines in the DOM, auto-scroll toggle and clear button. Seeded from `/api/logs` (or the snapshot's `logs` tail) so a reload keeps history.
- **Settings** — read-only status (version, cookies, max concurrent, downloads root) and an "Update yt-dlp" button that renders old/new version, restart requirement and the pip output.

### Subfolder picker
One shared native `<dialog>` (Escape and backdrop-click close it) opened with a callback, used by both Queue and Import. Breadcrumb navigation, drill-down, an ".. (up one level)" row, inline folder creation, and a "Use this folder" confirm. Root displays as `/` and is sent as `""`. The path the UI constructs is canonical — it is always built from folder names the server returned — and the server's echoed `path` is adopted only when it agrees, so a backend that echoed an absolute filesystem path could not break navigation.

### Verification
- `node --check static/app.js` clean.
- Driven in a real browser at 400×820 and 1100×800 against the live backend (`uvicorn app.main:app`): WS snapshot/live status, preset sync, folder create + select, job enqueue → failed → retry → delete, log drawer fetch + live append, global log, filters, "Clear finished", and an import scan returning youtube/vimeo candidates with one rejection. No horizontal overflow at 400px and no uncaught JS errors. Test jobs and the scratch folder were removed afterwards.

### Assumptions / notes
- The brief's contract differed from the shipped backend in four places; the UI accepts both shapes: `GET /api/jobs` returns `{jobs:[…]}` (not a bare array), `POST /api/jobs` returns `{job:…}` and bulk returns `{jobs,count}`; `/api/folders` returns `folders` as `{name,path}` objects (not strings); job `type` is `single|playlist|child` (not `video|playlist`), so nesting keys off `parent_id` and the item count off actual children; `/api/update-ytdlp` returns `changed` + `note` rather than `restart_required`.
- `speed` and `eta` are typed `Optional[str]` by the backend but the formatters also accept numbers (bytes/s, seconds), and `created_at` sorting accepts both ISO strings and epoch numbers.

## Integration & verification (2026-09-15)
- Fixed two design-hook findings in `static/style.css`: progress bar now animates `transform: scaleX()` instead of `width` (with matching change in `static/app.js`), and toasts use a leading colour dot instead of a side accent border.
- Built the image with `docker compose build` and ran the full verification pass against the container on Docker Desktop:
  - `/api/health` and `/api/status` healthy; static UI served.
  - Path traversal (`../../etc`) rejected with 400.
  - Import scan of mixed text returned youtube + vimeo candidates and rejected an unsupported URL.
  - Real 480p download completed into a created subfolder; files owned by PUID/PGID inside the container.
  - Cancel mid-transfer kept the `.part`; retry resumed at the exact byte offset; `docker compose restart` mid-download requeued the job and resumed again.
  - Playlist URL expanded into a parent with 13 child jobs and per-item progress; 4 children failed; 2 confirmed as private videos, 2 not inspected before cleanup.
  - Update-yt-dlp endpoint ran and reported version (already current).
- Added `launch.md` (docker compose launch, health probe).
- Test data (`downloads/`, `config/`) removed after the run.
- Fixed: `download_archive` was applied to every job, so re-adding a single URL with a different preset or folder reported "done" with no file. Archive is now used only by playlist child jobs (`use_archive=bool(parent_id)` in `app/worker.py`). Re-verified: same URL queued as 480p and audio-m4a produced both files.
- Added inline SVG favicon to `static/index.html` (was a 404 on every load).
- Known cosmetic issue: for merged presets the progress bar reaches 100% after the video stream while audio still downloads and muxes; status stays RUNNING until the merge finishes.
