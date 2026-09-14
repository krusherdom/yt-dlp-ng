"""yt-dlp Web backend package."""

import sys as _sys
from pathlib import Path as _Path

# yt-dlp discovers plugins by scanning ``sys.path`` for a ``yt_dlp_plugins``
# package. In the container ``/app`` is the cwd (and PYTHONPATH), but for local
# dev/test runs the repo root is on the path instead, so ``app/`` has to be
# appended explicitly. Appended (not prepended) so that app-internal modules
# such as ``config``/``db`` can never shadow a third-party top-level module.
_APP_DIR = str(_Path(__file__).resolve().parent)
if _APP_DIR not in _sys.path:
    _sys.path.append(_APP_DIR)

__version__ = "0.1.0"
