"""Test environment: point the app at throwaway dirs BEFORE importing app.*"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="ytdlpweb-tests-"))
os.environ.setdefault("DOWNLOADS_ROOT", str(_TMP / "downloads"))
os.environ.setdefault("CONFIG_DIR", str(_TMP / "config"))
os.environ.setdefault("MAX_CONCURRENT", "2")
os.environ.setdefault("RESUME_ON_START", "false")

(_TMP / "downloads").mkdir(parents=True, exist_ok=True)
(_TMP / "config").mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402


@pytest.fixture
def downloads_root(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    return root
