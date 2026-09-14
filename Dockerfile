# syntax=docker/dockerfile:1
FROM python:3.12-slim

# --- System dependencies -------------------------------------------------
# ffmpeg: required by yt-dlp for merging/remuxing/audio extraction.
# gosu:   lets the entrypoint start as root (to fix ownership/PUID/PGID)
#         and then drop to an unprivileged user before running the app.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# --- Python dependencies ---------------------------------------------------
# Installed into a dedicated venv (rather than system site-packages) so the
# entrypoint can chown just this one directory tree, giving the runtime
# (non-root) user permission to self-update yt-dlp via pip without needing
# write access anywhere else on the filesystem.
ENV VENV_PATH=/opt/venv
RUN python -m venv "$VENV_PATH"
ENV PATH="$VENV_PATH/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# --- Application code -------------------------------------------------------
COPY app/ ./app/
COPY static/ ./static/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# --- Runtime configuration ---------------------------------------------
# PUID/PGID/UMASK follow the LinuxServer.io convention used across Unraid.
# Defaults (99/100) match Unraid's built-in "nobody"/"users" account.
ENV DOWNLOADS_ROOT=/downloads \
    CONFIG_DIR=/config \
    PORT=8080 \
    MAX_CONCURRENT=2 \
    RESUME_ON_START=true \
    UPDATE_ON_START=false \
    PUID=99 \
    PGID=100 \
    UMASK=022

EXPOSE 8080
VOLUME ["/downloads", "/config"]

# Plain python stdlib check -- avoids pulling in curl just for the healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "\
import os, sys, urllib.request; \
port = os.environ.get('PORT', '8080'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=4).status == 200 else 1)" \
    || exit 1

ENTRYPOINT ["/entrypoint.sh"]
