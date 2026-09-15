"""Central configuration, read once from the environment.

All paths are resolved to absolute at import time so that downstream
``is_relative_to`` checks never compare a relative path against an absolute one.
Relative values (e.g. ``./downloads``) are supported for local Windows testing.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default).expanduser().resolve()


DOWNLOADS_ROOT: Path = _env_path("DOWNLOADS_ROOT", "/downloads")
CONFIG_DIR: Path = _env_path("CONFIG_DIR", "/config")

MAX_CONCURRENT: int = _env_int("MAX_CONCURRENT", 2)
RESUME_ON_START: bool = _env_bool("RESUME_ON_START", True)
PORT: int = _env_int("PORT", 8080)

# Pagination cap for the bundled imaglr extractors (profiles/pages/tags).
# The extractor reads os.environ directly (it must stay importable without the
# app package), so this is exported only for visibility/documentation.
IMAGLR_MAX_PAGES: int = _env_int("IMAGLR_MAX_PAGES", 500)

# Derived locations inside CONFIG_DIR.
DB_PATH: Path = CONFIG_DIR / "jobs.db"
LOG_DIR: Path = CONFIG_DIR / "logs"
COOKIES_FILE: Path = CONFIG_DIR / "cookies.txt"
ARCHIVE_FILE: Path = CONFIG_DIR / "archive.txt"
#: Persisted app settings (proxy pool etc.). Written atomically by app.settings.
SETTINGS_FILE: Path = CONFIG_DIR / "settings.json"

# Default URL fetched through a proxy to decide whether it is alive. A 204
# endpoint keeps the check cheap; override for air-gapped/LAN-only setups.
PROXY_TEST_URL: str = (
    os.environ.get("PROXY_TEST_URL") or "https://www.cloudflare.com/cdn-cgi/trace"
).strip()

# Repo root -> static/ lives next to app/. Resolved from __file__, never cwd.
BASE_DIR: Path = Path(__file__).resolve().parent.parent
STATIC_DIR: Path = BASE_DIR / "static"

# Global in-memory log ring buffer size (Logs tab).
LOG_RING_SIZE: int = 2000


def ensure_dirs() -> None:
    """Create the directories the app writes to. Safe to call repeatedly."""
    DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def cookies_detected() -> bool:
    return COOKIES_FILE.is_file()
