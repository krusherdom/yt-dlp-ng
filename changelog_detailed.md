
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

## Cookies upload & docs (2026-09-15)
- `app/main.py`: added `status_payload()` as the single source for `GET /api/status` and the WS `snapshot.status` payload (previously two hand-written dicts had drifted -- the WS one was missing `config_dir`/`resume_on_start`/`presets`). Extended it with `cookies_domains` (list, parsed from the cookie file, leading `.` stripped, sorted, unique) and `cookies_updated_at` (file mtime as UTC ISO 8601, or `null`).
- Added `POST /api/cookies` (multipart `file`): decodes as UTF-8 (`errors="replace"`), enforces a 2 MB cap on the raw bytes, validates either a `# Netscape HTTP Cookie File` / `# HTTP Cookie File` header on the first non-empty line or at least one 7-tab-field cookie row (tolerating the `#HttpOnly_` prefix some exporters use for HttpOnly cookies), then writes atomically via `tempfile.mkstemp` in the same directory as `config.COOKIES_FILE` + `os.replace`, `chmod 0600` best-effort (wrapped in `try`/`except OSError` for Windows). Returns `{cookies_detected, domains, count, updated_at}`.
- Added `DELETE /api/cookies`: removes the file (`Path.unlink(missing_ok=True)`), returns `{"cookies_detected": false}`.
- Both routes call a new `_broadcast_status()` helper that mirrors `worker._broadcast`'s fan-out (`{"type": "status", **status_payload()}` to every socket in `worker.clients`) without importing anything private from `worker.py`, so `app/worker.py` was left untouched -- it isn't owned by this stage and the other in-flight agent may touch it.
- Confirmed (and documented in a comment) that the `StaticFiles` mount only ever serves `config.STATIC_DIR`; there is no other catch-all route, so `/config` and the cookies file contents are never reachable over HTTP. `GET /api/cookies` correctly 405s (no route defined for that method+path).
- `static/index.html`: new "Cookies" card in the Settings tab -- status/domains/uploaded-date `<dl>`, a `.txt` file input, Upload button, and a Delete button that requires a second click ("Confirm delete") within 5s before it actually deletes.
- `static/app.js`: `uploadCookies()` (FormData POST) and `onDeleteCookiesClick()` (two-click confirm, 5s window via `setTimeout`) wired to the new buttons; both call `loadStatus()` afterwards and toast the result. `applyStatus()` now also paints the Cookies card and puts the domain list in the `#cookies-pill` title attribute. `handleWSMessage()` gained a `status` branch so every open tab repaints from a broadcasted status without polling. All new text hits the DOM via `textContent` (`setText`), consistent with the existing discipline.
- `static/style.css`: one new rule, `.btn.danger.confirm`, for the armed delete-confirmation state (no side accent borders, no width/height animation, consistent with the rest of the sheet).
- `tests/test_cookies.py` (new): FastAPI `TestClient` against the real `app.main` app (first test file in the repo to exercise the ASGI app end-to-end rather than calling module functions directly), with `config.COOKIES_FILE` monkeypatched per-test into `tmp_path` so tests never touch the shared session config dir from `tests/conftest.py` or collide with each other. Covers: valid Netscape upload -> 200 + correct domains, a headerless-but-tab-separated file -> 200, garbage -> 400 (and no file written), an oversized (>2MB) file -> 400, `GET /api/status` reflecting the uploaded domains, delete -> `cookies_detected: false` and file gone, delete-when-absent is a no-op 200, and `GET /api/cookies` -> 405. `python -m pytest tests/test_cookies.py -q` passes (8/8).
- Docs: README gained a "Supported sites" section (yt-dlp's full site list plus imaglr posts/profiles/pages/tags, videos-only, tags need cookies) and the cookies section was rewritten to lead with the Settings-tab upload flow, keeping the manual `/config/cookies.txt` drop as a documented alternative; added `IMAGLR_MAX_PAGES` (default 500) to the env var table and the Unraid template (`Display="advanced"`). `changelog.md` bumped to v0.2.0.

## imaglr extractor (2026-09-15)
- New yt-dlp plugin package `app/yt_dlp_plugins/` (`__init__.py`, `extractor/__init__.py`, `extractor/imaglr.py`). yt-dlp discovers any `yt_dlp_plugins` package on `sys.path`; the loader synthesises the namespace packages itself and never executes those `__init__.py` files, so both are intentionally empty.
- `app/yt_dlp_plugins/extractor/imaglr.py`: four extractors over imaglr.com's Laravel + Inertia site, **videos only** (image and GIF media are filtered out everywhere).
  - `ImaglrPostIE` (`imaglr:post`, `/(p/<slug>/)?post/<id>`): parses the HTML-escaped `data-page` Inertia JSON. Handles both post prop shapes found live -- `PostDetail` gives `props.post = {"data": {...}}`, `Pages/ShowPost` gives a flat `props.post`. Reposts carry an empty outer `media`, so the payload is unwrapped from `original_post`/`reposted_from`. One video -> single info dict; several -> `playlist_result(..., multi_video=True)`. No video at all -> `ExtractorError("No video in this post", expected=True)`. Falls back to `og:video` only when the props can't be parsed at all.
  - `ImaglrProfileIE` (`imaglr:profile`, `/profile/<name>` with a trailing `(?:[?#]|$)` anchor so `/profile/<name>/rss` and `/likes` are *not* matched): when the cookie jar holds any imaglr.com cookie it uses `GET /profile/<name>/posts?skip=K&take=20&paginated=false&type=video` with `Accept: application/json` + `X-Requested-With: XMLHttpRequest`; a 401/403 emits a warning and falls back to the public RSS feed `/rss/user/<name>?page=N` rather than failing (a Laravel guest session cookie is indistinguishable from a real one before the request is made).
  - `ImaglrPageIE` (`imaglr:page`, `/p/<slug>`): public `GET /p/<slug>/posts?page=1`. The live response is `{data, page, has_more}` with **no** `next_page_url`, so the paginator follows `next_page_url` when present and otherwise increments `?page=N` while `has_more` is true. Falls back to `/rss/page/<slug>` if the JSON API fails before anything was yielded.
  - `ImaglrTagIE` (`imaglr:tag`, `/tag/<tag>`): needs a login. With no imaglr cookie it raises an expected error without making a request; with cookies it fetches `/tag/<tag>` and, because an anonymous/stale session is served the Inertia component `Auth/Login`, that component name is treated as "cookies missing or expired". The posts prop is then located *generically* -- the first non-furniture prop that is a list (or a `{data: [...]}` paginator) whose first item looks like a post (`post_type` present, or a nested `post` object) -- and paginated via `next_page_url`. An unrecognised layout logs a warning naming the component and the prop keys, then raises an expected error.
  - Playlists always yield `url_result(post_url, ie=ImaglrPostIE.ie_key(), ...)`, never full info dicts, including on the logged-in profile path: `worker._run_flat_extract` turns `entries[].url` into child jobs, so full dicts would have produced child jobs pointing at bare cdn03 mp4 URLs (generic extractor, hash filename, no Referer). Videos-only filtering happens *before* yielding, so the flat entry list already excludes image posts. Entry generators are lazy so `--playlist-end` doesn't walk every page.
  - Every returned video entry sets `http_headers: {"Referer": "https://imaglr.com/"}`.
  - Defensive hardening for the two paths that could not be captured from the live site (the logged-in `/profile/<name>/posts` serialiser and the tag props): if a post filters to zero videos but claims `post_type == "video"` *and* carries no `media` array at all, the url_result is still yielded -- `ImaglrPostIE` then either finds the video on the post page or raises the expected error, instead of the playlist silently coming back empty. A post whose `media` array is present but image-only is still skipped.
  - Tag pagination sends the Inertia protocol headers (`X-Inertia`, `X-Inertia-Version` from the first page's `version`) so a page-route pagination URL answers with JSON, and still digs `data-page` back out if it answers with HTML. Both the tag and logged-in-profile pagination loops now catch `ExtractorError` mid-playlist, warn, and stop with the already-yielded entries intact rather than failing the whole playlist (which the worker would turn into a second failure by retrying the playlist URL as a direct download).
  - `IMAGLR_MAX_PAGES` (default 500) is read from `os.environ` on every call (not at import) and caps RSS, page, profile and tag pagination; hitting the cap logs a warning.
  - `__all__` lists only the four public extractors -- yt-dlp's plugin loader registers every class whose name ends in `IE`, and an exposed `ImaglrBaseIE` without a `_VALID_URL` would break `suitable()` for every URL. `IE_NAME` is set explicitly on each (the default derivation would give `ImaglrPost`, not `imaglr:post`).
- `app/importer.py:_extractors()`: now calls `yt_dlp.plugins.load_all_plugins()` (wrapped in try/except) before `gen_extractor_classes()` -- without it plugin extractors are invisible to the importer, since it never constructs a `YoutubeDL` (which would load them itself). Generic is still excluded.
- `app/__init__.py`: appends the `app/` directory to `sys.path` so `yt_dlp_plugins` is discoverable in local dev/test runs where only the repo root is on the path. Appended rather than prepended so `app/config.py`, `db.py` etc. can never shadow a third-party top-level module.
- `app/config.py`: added `IMAGLR_MAX_PAGES` (`_env_int`, default 500). Exported for visibility only -- the extractor reads `os.environ` directly so it stays importable without the app package.
- `Dockerfile`: added `ENV PYTHONPATH=/app`. Note this does **not** expose the plugin by itself -- in the image the plugin is at `/app/app/yt_dlp_plugins/`, so `/app` on the path would look for `/app/yt_dlp_plugins`, which does not exist (the plan's assumption here was wrong). What actually finds it in the container is the `app/__init__.py` append, which runs because the entrypoint execs `uvicorn app.main:app` and therefore imports the `app` package first; this was verified by driving `ytdl.build_opts(flat=True)` + `YoutubeDL` through the `app` package with no `PYTHONPATH` set, which enumerated the profile correctly. The env var is kept because it guarantees `import app` works under the `gosu` exec regardless of cwd. It is deliberately *not* `/app/app`: PYTHONPATH entries precede site-packages, which would let `app/config.py`, `db.py` etc. shadow third-party top-level modules.
- `app/worker.py`, `app/ytdl.py`: unchanged. Verified `build_opts(flat=True)` + `YoutubeDL` returns url_result entries with a usable `url` for the profile extractor, which is exactly what `_run_flat_extract` consumes; the Referer is set per-entry by the extractor so `build_opts` needs no URL-aware branch.
- `tests/test_imaglr.py` (new, 36 tests) + `tests/fixtures/` (new): `post_90040737.json` and `post_84973417.json` (real `data-page` props of a video post and of an image-only repost), `rss_user_handyman.xml` (real 2-item feed), `page_art_posts.json` (real `/p/art/posts?page=1`, 20 posts of which exactly 1 is a video -- and that one is a repost whose video lives in `reposted_from`, so it covers videos-only filtering and repost unwrapping with real data). Covers `_VALID_URL` positives/negatives for all four extractors (including `/profile/x/rss`, `/profile/x/likes`, `/p/art/post/123`), `_post_entries` on both post shapes, multi-video numbering, GIF-stored-as-mp4 rejection, extension fallback for type-less media, `og:video` fallback, the expected "No video in this post" error, RSS pagination/stop conditions/`IMAGLR_MAX_PAGES`, the anonymous-RSS vs logged-in-JSON profile branch and the 401 fallback, page `has_more` and `next_page_url` pagination plus RSS fallback, the tag extractor's four failure/success paths, and importer validation of imaglr URLs. No network: `_download_webpage`/`_download_json` are monkeypatched throughout.
- Verification: `.venv/Scripts/python -m pytest tests -q` -> 113 passed. Live: `PYTHONPATH=app python -m yt_dlp --print ...` on `/post/90040737` -> `imaglr:post|90040737|My My My - 90040737|https://cdn03.imaglr.com/.../JAFv6mhfgzbXcf0SXsxFBKx2woFbCbK09xIVQuNR.mp4`; `/post/84973417` -> `ERROR: [imaglr:post] 84973417: No video in this post`; `--flat-playlist` on `/profile/handyman` -> 2 `imaglr:post` entries; on `/p/art` -> 7 video posts (images skipped); `/tag/art` anonymously and with a dummy cookie -> the expected login error. The single post downloaded to a scratch dir (9.20 MiB mp4) and was deleted.
- Known limitation: a post with *several* videos returns a multi_video playlist of full info dicts, so under flat extraction its children would be bare mp4 URLs. No such post was found on the site while building this, so it is untested. The tag extractor could not be exercised against a real logged-in session either -- its prop discovery is deliberately shape-agnostic and warns loudly instead of failing silently.

## v0.2.0 integration & verification (2026-09-15)
- Verified in Docker: import classifies imaglr post/profile/page/tag URLs (and rejects the /rss variant); single post downloads via `imaglr:post`; profile playlist yields 2 children, both download; community page `/p/art` paginates 4 JSON pages and yields only the 7 video posts (images skipped); image-only post fails with "No video in this post".
- Cookies: upload returns domains, garbage rejected 400, GET /api/cookies 404, file stored 0600 in /config, delete removes it and status updates.
- Renamed page title and header brand to yt-dlp-ng.
- Not verified: tag extractor and logged-in profile JSON path (need the user's real imaglr cookies).

## Proxy API, UI & docs (2026-09-15)
- `app/main.py`: wired the proxy pool (`app/settings.py` + `app/proxies.py`, built concurrently against the agreed contract) into the API.
  - Lifespan: `await settings.load()` at startup, `_broadcast_proxies` registered in `proxies.on_change` (guarded against double-registration across repeated `TestClient(app)` lifespans in the same process, since `on_change` is a module-level list), `await proxies.start_background(loop)`; shutdown adds `await proxies.stop_background()`.
  - `GET /api/proxies` -> `{"settings": public_settings(), "statuses": [...], "summary": {...}}` (`_proxies_payload()` is the single source, reused by PUT's response).
  - `PUT /api/proxies` (body: `ProxySettings`) -> `await settings.save(payload)`, catching `ValidationError`/`ValueError` into 422/400; on success schedules `proxies.check_all()` as a tracked background task (`_fire_and_forget`, keeps a `Set[asyncio.Task]` reference so it can't be GC'd mid-flight, logs any failure into the log ring instead of raising) and returns the same shape as GET. The masked-URL round-trip (`user:***@` for an existing id keeps the stored URL) turned out to already be handled inside `settings.save()` itself (`_unmask()`), so no duplicate logic was added here.
  - `POST /api/proxies/test` (body `{"url"}` or `{"id"}`, local `ProxyTestRequest` model -- not added to `app/models.py` since that file belongs to the other in-flight agent): `id` path 404s if unknown, else `await proxies.check_one(id)`; `url` path runs `proxies.check_sync` via `asyncio.to_thread`. Both are wrapped in `asyncio.wait_for(..., timeout=settings.timeout_s + 5)` as a safety net on top of the check's own internal timeout, returning 504 rather than hanging the request. Response is the `ProxyStatus` dict plus a masked `url`.
  - `POST /api/proxies/check` -> `await proxies.check_all()` -> `{"statuses": [...], "summary": {...}}`.
  - `status_payload()` gained `"proxies": proxies.summary()`; the WS `snapshot` message gained a top-level `"proxies": {"statuses": [...], "summary": {...}}` (richer than `status.proxies`, which is just the summary counts used by the header pill).
  - All `ProxyStatus`/`ProxyEntry` dicts use `.model_dump()` (pydantic 2.13 installed; `.dict()` is deprecated).
- `static/index.html` / `static/app.js` / `static/style.css`: new Settings -> Proxies card -- table of configured proxies (label, masked URL, status dot + live/down/latency/exit-IP meta line with the last error in a `title` tooltip, enabled checkbox, Test, Remove), an add-row (URL + optional label + Test + Add), mode radios (domains / all) with a domains textarea (one per line, lower-cased client-side, hidden outside domains mode), advanced `<details>` (test URL, check interval, timeout), and Re-check all / Save buttons. Save shows "Save changes" and an amber (`--warn`) highlight (`.btn-primary.unsaved`) whenever any field has been touched since the last successful load/save (`markProxyDirty()`); a successful GET/PUT response clears it. Test on a row not yet persisted (added locally, never saved) sends `{url}`; Test on a row that came from the server sends `{id}` (tracked with a client-only `_isNew` flag that a save clears by fully re-deriving state from the PUT response). New proxy rows get a client-generated id (`crypto.randomUUID()`, with a fallback for older browsers) since the id is the stable key both the add/remove UI and later `{id}`-based tests need. Header `#proxy-pill` ("proxies 2/3 live") stays hidden while `configured === 0`, otherwise green/red on `live > 0`; it's kept in sync from three sources: the initial `GET /api/proxies`, live WS `proxies` messages, and (for the count only) every `status` broadcast and the WS `snapshot`, since a cookies-only change still carries the proxy summary. Queue rows gained a `via <value>` tag (`.job-proxy`, reusing `.tag`) driven by `job.proxy`, which the other agent's `worker.py` already populates with a human label or masked URL. All new/changed text is written via `textContent`/`setText`; no inline styles; state transitions use `background`/`border-color`/`transform` only (no side-accent borders, no width/height animation), consistent with the existing design-hook constraints. `node --check static/app.js` is clean.
- `tests/test_proxy_api.py` (new, 12 tests): `TestClient` against the real app (module-scoped, lifespan runs once), with a per-test fixture monkeypatching `app.config.SETTINGS_FILE` into `tmp_path` and resetting `app.settings`'s and `app.proxies`'s module caches before and after. A `fast_check` fixture stubs `app.proxies.check_sync` (still writing into the real in-memory status table via `proxies._store`) for the settings-CRUD tests, since a PUT schedules a real `check_all()` in the background that would otherwise hit the network for up to `timeout_s` seconds per proxy. Covers: PUT valid settings -> 200 + GET returns masked credentials; PUT with a bad scheme / missing host / missing port -> 4xx (via pydantic's own `field_validator` on `ProxyEntry.url`, already a 422 before the handler runs); PUT round-tripping a masked URL for an existing id keeps the real stored URL while still letting the label change, verified against `app.settings.get()` directly; the test endpoint against `http://127.0.0.1:9` (nothing listening) comes back `live: false` using the *real*, unstubbed health check within the timeout bound; test-endpoint validation (`400` with neither `url` nor `id`, `404` for an unknown `id`); GET with nothing saved returns defaults; `/api/proxies/check` and `/api/status` and the WS `snapshot` all carry the proxy summary/statuses. `python -m pytest tests -q` -> 125 passed (includes the other agent's `tests/test_proxies.py`, not authored here).
- Docs: README gained a "Proxies" section (why XVideos' Australian age-gate produces "No video formats found" with a 200 response and no sources, the gluetun HTTP/SOCKS5 example, per-site vs. all-downloads mode, health checks + round-robin, retry-once-on-failure, credential masking) and a matching Troubleshooting entry; `PROXY_TEST_URL` added to the environment variable table. Unraid template (`unraid/yt-dlp-ng.xml`) gained `PROXY_TEST_URL` as an advanced `Variable` (default `https://www.google.com/generate_204`). `changelog.md` bumped to v0.3.0.
- Did not touch: `app/models.py`, `app/settings.py`, `app/proxies.py`, `app/ytdl.py`, `app/worker.py`, `app/db.py`, `app/config.py`, `tests/test_proxies.py` -- all written by the concurrently running agent per the agreed contract; verified by reading them (not by inference) before wiring `main.py` against them, and again before writing this test file.

## Proxy pool backend

Backend half of the proxy-pool feature (plan sections 1-3): settings store,
health-checked pool, and the wiring into yt-dlp options, the worker and the DB.
The REST/WS endpoints and the UI are covered separately.

### New: `app/settings.py`
- Persisted app settings in `config.SETTINGS_FILE` (`/config/settings.json`),
  written atomically (`tempfile.mkstemp` + `fsync` + `os.replace`) under an
  asyncio lock and cached in module state.
- `load()` (awaited once at startup; missing/corrupt/invalid file logs a warning
  and falls back to defaults), `get()` (sync, never does I/O, returns defaults
  when `load()` has not run), `save(settings)` (atomic, updates the cache and
  calls `proxies.reset_statuses_for`).
- `mask_url()` turns `scheme://user:pw@host:port` into `scheme://user:***@host:port`,
  rebuilding the netloc from `netloc.rsplit("@", 1)[-1]` so bracketed IPv6
  literals survive. `public_settings()` is the masked dump the API returns.
- `_unmask()` on save: an entry whose URL is exactly the mask of the stored URL
  for the same id keeps the stored URL, so a UI that PUTs back what GET handed
  it cannot persist `***` and silently break the proxy.

### New: `app/proxies.py`
- In-memory status table (`ProxyStatus` per proxy id) plus a monotonic
  `_checked_at` map, both guarded by a single `threading.Lock` that is never
  held across a network call.
- `host_matches()` / `needs_proxy()`: dot-boundary suffix matching with `www.`
  stripped on both sides, so `notxvideos.com` never matches `xvideos.com`.
- `check_sync()` reuses yt-dlp's own networking
  (`YoutubeDL.urlopen(Request(test_url, proxies={"all": url}, extensions={"timeout": t}))`),
  so SOCKS works with no extra dependency and the check exercises exactly the
  stack a download will use. Live iff status < 400; latency from
  `time.perf_counter()`; a best-effort exit IP via `api.ipify.org` afterwards.
- `acquire(url, exclude)`: returns `None` for direct jobs, otherwise checks
  unchecked/stale candidates inline (it only ever runs on a worker thread) and
  round-robins over the live ones. Raises `NoLiveProxy` when a URL that must be
  proxied has nothing alive, rather than leaking the real IP to the site the
  user explicitly routed through the pool.
- `check_all()` / `check_one()` fan the blocking checks into the default
  executor and then await the `on_change` callbacks (each wrapped in
  try/except so a dead listener cannot kill the periodic loop).
- `start_background()` / `stop_background()`: periodic checker that re-reads
  `check_interval_s` every cycle and returns immediately at startup.

### Wiring
- `app/models.py`: `ProxyEntry` (uuid id, scheme/host/explicit-port/whitespace
  validation), `ProxySettings` (mode, normalised domain list, bounded interval
  and timeout, duplicate-URL rejection), `ProxyStatus`, `normalise_domain()`,
  and `Job.proxy`.
- `app/config.py`: `SETTINGS_FILE`, `PROXY_TEST_URL` (env override, default
  `https://www.google.com/generate_204`).
- `app/ytdl.py`: `build_opts(..., proxy=...)` sets `opts["proxy"]` last, after
  the extra-args merge, so a pool decision for a must-be-proxied domain cannot
  be undone by a user's `--proxy`; on direct jobs a user `--proxy` still works.
- `app/worker.py`: `_run_download` acquires a proxy, reports it with
  `emit_job(proxy=...)`, logs `[job] via proxy <label>` / `[job] direct`, and
  retries once on the next live proxy (bounded to two attempts, cancel-aware,
  with an inline re-check of the failed proxy first). `_run_flat_extract` uses
  the same selection so geo-gated playlist enumeration is proxied too;
  `NoLiveProxy` propagates and `_dispatch_single` turns it into a failed job
  with "No live proxy for <host>; check Settings -> Proxies" instead of the
  usual "downloading directly" fallback.
- `app/db.py`: `proxy TEXT` in the schema and `_UPDATABLE`, `_row_to_dict`
  defaults it to `None`, and `_migrate()` adds the column to databases created
  by an older version (`PRAGMA table_info` guarded, idempotent, runs on every
  startup).

### Tests
`tests/test_proxies.py` (62 cases, no network): an autouse fixture isolates the
module state and a fake `check_sync` reports health from a dict while recording
every call, which doubles as the assertion that stale entries are re-checked
inline. Covers host matching, mode-based routing, round-robin and exclusions,
`NoLiveProxy`, status/summary bookkeeping, model validation, masking and the
masked-round-trip guard, settings save/load (including corrupt files),
`build_opts` proxy handling, and the DB migration against a pre-existing
database without the column.

### Review fixes (same day)
- `static/style.css`/`app.js`: header pill actually turns red (new `.pill.bad`, err-colored) when `live == 0` and `configured > 0` -- it previously fell back to the neutral `.pill.off` (grey), contradicting the README.
- `app/main.py`: `POST /api/proxies/test`'s `asyncio.wait_for` bound was `timeout_s + 5`, which had zero slack for a *live* proxy -- `check_sync` also runs the best-effort exit-IP lookup (`proxies.EXIT_IP_TIMEOUT`, 5s) after a successful check, so a slow-but-live proxy could hit exactly the old bound and 504 despite being live. Now `timeout_s + EXIT_IP_TIMEOUT + 5`. Bumped `FastAPI(version=...)` to `0.3.0` (was still `0.1.0`).
- `static/app.js`: `applyProxyStatuses()` (the WS live-update path) no longer wipes the test result of a locally-added-but-not-yet-saved proxy row on the next periodic broadcast, since the server has no record of it to report back. `showTab('settings')` now also calls `loadProxies()` (guarded by `!proxyDirty`, so it can't clobber in-progress edits) so a save made in another tab/window shows up without a full reload.
- Verified (grep, not just review) that no raw proxy URL ever reaches the UI: every `emit_job(..., proxy=...)` in `app/worker.py` passes `proxies.describe(entry)` (label or masked URL); only `ytdl.build_opts(proxy=...)` gets the real `entry.url`/`selected.url`, which never leaves the worker thread.
- Noted, not fixed (owned by the other agent): `app/models.py`'s `ProxySettings._no_duplicates` validator runs before `settings.py`'s `_unmask()`, so a PUT containing both a round-tripped masked URL and a freshly typed real URL that happen to unmask to the same value would pass duplicate-checking and then collide -- an obscure edge case, reported rather than touched since `models.py`/`settings.py` are not owned by this stage.
- Re-ran `pytest tests -q` (187 passed) and `node --check static/app.js` (clean) after these fixes.

## v0.3.0 integration & verification (2026-09-15)
- Verified in Docker with two Squid containers plus a dead entry: test endpoint reports down/live with latency and exit IP; credentials masked in every response and in settings.json round-trips; domains mode routes xvideos.com jobs round-robin over live proxies (squid access log confirms) while YouTube goes direct; retry on the next live proxy after a failure; "all" mode proxies YouTube; killing the proxy mid-download triggers retry on the other proxy and the download completes; with no live proxy the job fails fast with "No live proxy for www.xvideos.com".
- XVideos from an Australian exit still returns no formats (site age gate); only a non-AU proxy exit resolves it, which the user's LAN proxies provide.
