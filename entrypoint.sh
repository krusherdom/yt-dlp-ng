#!/bin/sh
# Entrypoint: runs as root, fixes ownership/permissions for the configured
# PUID/PGID, optionally updates yt-dlp, then drops privileges via gosu to
# actually run the app. Must keep LF line endings (see .gitattributes).
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
DOWNLOADS_ROOT="${DOWNLOADS_ROOT:-/downloads}"
PORT="${PORT:-8080}"
VENV="${VENV_PATH:-/opt/venv}"

# --- create/adjust the "app" group and user to match PUID/PGID ------------
if getent group app >/dev/null 2>&1; then
    groupmod -o -g "$PGID" app
else
    groupadd -o -g "$PGID" app
fi

if getent passwd app >/dev/null 2>&1; then
    usermod -o -u "$PUID" -g "$PGID" -d "$CONFIG_DIR" app
else
    useradd -o -u "$PUID" -g "$PGID" -d "$CONFIG_DIR" -M -s /usr/sbin/nologin app
fi

umask "$UMASK"

mkdir -p "$CONFIG_DIR/logs" "$DOWNLOADS_ROOT"

# Config dir (db, logs, cookies.txt, yt-dlp/pip caches now that $HOME points
# here) is small, so it's safe -- and necessary -- to chown it recursively on
# every start; that way a PUID/PGID change takes effect immediately instead
# of leaving pre-existing files owned by the old uid. Downloads root is only
# chowned at the top level -- NOT recursive -- so an existing large media
# library isn't walked (and rewritten) on every start.
chown -R "$PUID:$PGID" "$CONFIG_DIR" 2>/dev/null || true
chown "$PUID:$PGID" "$DOWNLOADS_ROOT" 2>/dev/null || true

# --- give the runtime user write access to the venv's site-packages -------
# Needed so "Update yt-dlp" (Settings tab button or UPDATE_ON_START) can pip
# install as the unprivileged user. Only chown when ownership actually
# differs from the target uid, so restarts stay fast.
SITE_PACKAGES=$(find "$VENV/lib" -maxdepth 1 -name 'python3.*' -exec printf '%s/site-packages' {} \; 2>/dev/null | head -n1)
if [ -n "$SITE_PACKAGES" ] && [ -d "$SITE_PACKAGES" ]; then
    CURRENT_OWNER=$(stat -c '%u' "$SITE_PACKAGES" 2>/dev/null || echo "")
    if [ "$CURRENT_OWNER" != "$PUID" ]; then
        chown -R "$PUID:$PGID" "$VENV"
    fi
fi

if [ "$UPDATE_ON_START" = "true" ]; then
    echo "[entrypoint] UPDATE_ON_START=true, updating yt-dlp..."
    gosu "$PUID:$PGID" "$VENV/bin/pip" install --no-cache-dir -U "yt-dlp[curl-cffi]" || \
        echo "[entrypoint] WARNING: yt-dlp update failed, continuing with existing version"
fi

echo "[entrypoint] starting yt-dlp-web as uid=$PUID gid=$PGID on port $PORT"
exec gosu "$PUID:$PGID" "$VENV/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
