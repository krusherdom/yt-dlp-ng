# yt-dlp-ng

A self-hosted web UI for [yt-dlp](https://github.com/yt-dlp/yt-dlp), built to run as a
Docker container on Unraid (or any Docker host). Queue downloads, watch live
progress and logs in your browser, and let interrupted jobs resume automatically
after a restart.

No login/auth is implemented -- it is designed for trusted LAN use only. Do not
expose it directly to the internet.

## Features

- **Live queue** -- add URLs, watch progress/speed/ETA over a WebSocket, cancel
  or retry jobs, expand a log drawer per job.
- **Quality presets** -- `best`, `1080p`, `720p`, `480p`, `audio-mp3`, `audio-m4a`,
  plus a free-form field for advanced yt-dlp arguments.
- **Playlists & channels** -- enumerated up front; each item becomes its own
  child job with individual progress, retry, and resume.
- **Bulk import** -- paste raw text or upload an exported bookmarks `.html`
  file; links are extracted, validated against yt-dlp's extractors, and
  offered as a checklist before you enqueue them.
- **Folder picker** -- browse/create subfolders inside your downloads mount;
  the app can never write outside of it.
- **Cookies support** -- upload a cookies file from the Settings tab (or drop
  a `cookies.txt` into the config volume) for authenticated downloads.
- **Auto-resume** -- jobs still queued/running when the container stops are
  automatically requeued on the next start.
- **Self-updating** -- update yt-dlp from the Settings tab, or automatically
  on every container start.
- **Runs as non-root** -- configurable `PUID`/`PGID`/`UMASK`, matching the
  LinuxServer.io convention used throughout Unraid.

## Supported sites

Anything [yt-dlp itself supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
-- YouTube, Vimeo, SoundCloud, Twitter/X, and hundreds of others -- works
out of the box.

In addition, this app ships a first-class **imaglr.com** extractor:

- Single posts, user profiles, community pages ("`/p/<slug>`"), and tags.
- **Videos only** -- image posts are enumerated but skipped, since this app
  downloads media, not pictures.
- Public posts, profiles, and pages work anonymously (profiles and pages fall
  back to the site's public RSS feed when not logged in). **Tags always
  require a cookies file**, and logged-in profiles get richer pagination via
  imaglr's JSON API instead of RSS.

## Screenshots

_(placeholder -- add screenshots of the Queue, Import, and Settings tabs here
once the UI is built)_

## Quick start (Docker Compose)

```bash
git clone https://github.com/krusherdom/yt-dlp-ng.git
cd yt-dlp-ng
docker compose up --build -d
```

Then open `http://<host>:8080`.

`docker-compose.yml` maps `./downloads` and `./config` on the host and uses
`PUID=1000`/`PGID=1000` (typical for a Linux desktop/dev machine). Adjust
these to match the user that should own the downloaded files.

## Unraid install

The image is published to the GitHub Container Registry as
`ghcr.io/krusherdom/yt-dlp-ng:latest` and is public, so Unraid can pull it
without any registry login.

**Option A -- template URL (easiest)**

1. In the Unraid web UI open **Docker -> Add Container**.
2. At the bottom of the form, in **Template repositories** (or via the
   **Template** dropdown -> "..." on newer versions) paste this URL:
   `https://raw.githubusercontent.com/krusherdom/yt-dlp-ng/main/unraid/yt-dlp-ng.xml`
   Alternatively, download that file and copy it to
   `/boot/config/plugins/dockerMan/templates-user/` on the flash drive, then
   pick **yt-dlp-ng** from the Template dropdown.
3. Set the two host paths: `/downloads` (where videos land, e.g.
   `/mnt/user/media/youtube`) and `/config` (default
   `/mnt/user/appdata/yt-dlp-ng`).
4. Leave `PUID=99` and `PGID=100` (Unraid's `nobody:users`) unless your share
   uses a different owner, then click **Apply**.
5. Open the WebUI from the container's icon, or `http://<unraid-ip>:8080`.

**Option B -- manual Docker tab**

Create a new container manually with:

- Repository: `ghcr.io/krusherdom/yt-dlp-ng:latest`
- Network: `bridge`
- Port: `8080 -> 8080`
- Path: `/downloads -> /mnt/user/media/youtube` (or wherever you want files)
- Path: `/config -> /mnt/user/appdata/yt-dlp-ng`
- Variables: `PUID=99`, `PGID=100`, `UMASK=022`, `MAX_CONCURRENT=2`,
  `RESUME_ON_START=true`, `UPDATE_ON_START=false`

**Updating on Unraid:** every push to `main` rebuilds `:latest`. Click
**Check for Updates** on the Docker tab and then **Update** on the container.

## Environment variables

| Variable          | Default      | Description                                                                 |
|-------------------|--------------|-------------------------------------------------------------------------------|
| `DOWNLOADS_ROOT`  | `/downloads` | Container path files are saved under. Change the host-side mount, not this. |
| `CONFIG_DIR`      | `/config`    | Container path for the job database, logs, archive, and `cookies.txt`.      |
| `PORT`            | `8080`       | Port the web server listens on inside the container.                        |
| `MAX_CONCURRENT`  | `2`          | Number of downloads that may run at the same time.                          |
| `RESUME_ON_START` | `true`       | Requeue jobs left `queued`/`running` from a previous run.                   |
| `UPDATE_ON_START` | `false`      | Run `pip install -U yt-dlp` every time the container starts.                |
| `PUID`            | `99`         | User ID the app runs as (files are written with this owner).                |
| `PGID`            | `100`        | Group ID the app runs as.                                                   |
| `UMASK`           | `022`        | Umask applied to newly created files/folders.                               |
| `IMAGLR_MAX_PAGES`| `500`        | Safety cap on pages enumerated per imaglr profile/page/tag/RSS listing.     |

## Volumes

| Container path | Purpose                                                                 |
|-----------------|--------------------------------------------------------------------------|
| `/downloads`    | Where finished downloads land. Point this at your media library/share. |
| `/config`       | `jobs.db` (SQLite queue/history), `logs/<job_id>.log`, `archive.txt`, and optionally `cookies.txt`. Back this up if you care about queue history. |

## Cookies

To download age-restricted, members-only, or account-gated content (including
imaglr tags and some profile content), upload a cookies file from the app
itself:

1. While logged in to the target site in your browser, export its cookies in
   Netscape format -- e.g. with the "Get cookies.txt LOCALLY" extension, or
   `yt-dlp --cookies-from-browser <browser>` run locally to generate a file.
2. Open the **Settings** tab -> **Cookies** card, pick the exported `.txt`
   file, and click **Upload**. The app validates it looks like a real cookies
   file, stores it server-side, and immediately shows the domains it covers.
3. It's picked up on the very next job -- no restart needed. The header pill
   and the Settings tab show whether cookies are detected and which domains
   they cover.
4. To remove it, click **Delete** in the same card, then **Confirm delete**
   within 5 seconds. Once uploaded, the file is never served back out over
   the API -- deleting and re-uploading is the only way to change it.

**Manual alternative:** you can still drop the file directly onto the config
volume as `cookies.txt` (e.g. `/mnt/user/appdata/yt-dlp-ng/cookies.txt` on
Unraid, or `./config/cookies.txt` with the Compose setup) and restart the
container, or just wait -- it's picked up per-job either way.

Treat any cookies file as a credential -- don't commit it or share it.

## Updating yt-dlp

Sites change frequently and can break extraction; updating yt-dlp is usually
the fix. Two ways:

- **From the UI**: Settings tab -> "Update yt-dlp" button. Runs
  `pip install -U yt-dlp` inside the running container as the unprivileged
  app user and reports the new version.
- **Automatically**: set `UPDATE_ON_START=true` so it updates every time the
  container starts.

## Building & publishing the image

A GitHub Actions workflow (`.github/workflows/docker.yml`) builds the image
for `linux/amd64` and pushes it to GHCR on every push to `main` and on every
`v*` tag:

- `ghcr.io/krusherdom/yt-dlp-ng:latest` -- tracks `main`
- `ghcr.io/krusherdom/yt-dlp-ng:0.1.0` -- from tag `v0.1.0`
- `ghcr.io/krusherdom/yt-dlp-ng:sha-<short>` -- every commit

To release a version: `git tag v0.2.0 && git push --tags`.

For local testing without pushing anywhere, `docker compose up --build`
builds and runs directly from source.

## Troubleshooting

**Downloaded files are owned by the wrong user / permission denied writing
to the share.**
Set `PUID`/`PGID` to match the user that should own the files (on Unraid,
`99`/`100` for `nobody:users` is usually correct; for Compose on a regular
Linux host, use `id -u`/`id -g` of the intended owner). The entrypoint
recursively fixes ownership of `/config` on every start (it's small: db,
logs, archive, cookies), so a `PUID`/`PGID` change takes effect immediately.
`/downloads` is only chowned at its top level (not recursively), so
pre-existing files elsewhere in a large library are left alone -- if you
change `PUID`/`PGID` after files already exist there, fix their ownership
manually (e.g. `chown -R <uid>:<gid> /mnt/user/media/youtube`).

**Downloads suddenly fail with extractor/format errors.**
YouTube (and other sites) change frequently and break older yt-dlp releases.
Update via the Settings tab button, or enable `UPDATE_ON_START`.

**Container is "unhealthy".**
Check `docker logs yt-dlp-ng`. The health check hits `GET /api/health`
inside the container every 30s; a slow first boot (installing/updating
yt-dlp) can take a few seconds longer than the `start-period`, which is
normal.

**A playlist/channel item is stuck retrying the same file.**
Check `/config/archive.txt` and the per-job log in the Logs tab / log
drawer -- it records the exact yt-dlp error for that item.

**Path traversal / "invalid folder" errors when picking a subfolder.**
Intentional -- the app resolves subfolder paths and rejects anything that
would resolve outside `DOWNLOADS_ROOT` (e.g. `../../etc`).
