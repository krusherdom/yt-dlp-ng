"""Preset -> yt-dlp option mapping, extra-args parsing, cookie detection."""

from __future__ import annotations

import copy
import shlex
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional

from . import config

OUTTMPL = "%(title)s [%(id)s].%(ext)s"

#: preset name -> the preset-specific part of the yt-dlp option dict.
PRESETS: Dict[str, Dict[str, Any]] = {
    "best": {
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
    },
    "1080p": {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
    },
    "720p": {
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "merge_output_format": "mp4",
    },
    "480p": {
        "format": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "merge_output_format": "mp4",
    },
    "audio-mp3": {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    },
    "audio-m4a": {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "192",
            }
        ],
    },
}

DEFAULT_PRESET = "best"
PRESET_NAMES: List[str] = list(PRESETS.keys())

_baseline_opts: Optional[Dict[str, Any]] = None


class ExtraArgsError(ValueError):
    """Raised when the advanced extra-args string cannot be parsed."""


def preset_options(preset: str) -> Dict[str, Any]:
    """Return a deep copy of the option fragment for ``preset``.

    Unknown preset names fall back to ``best`` rather than failing a job.
    """
    return copy.deepcopy(PRESETS.get(preset, PRESETS[DEFAULT_PRESET]))


def _baseline() -> Dict[str, Any]:
    """yt-dlp's own defaults from an empty CLI parse, cached.

    ``parse_options`` fills in ~170 keys; merging them wholesale would clobber
    our presets, so we only take the keys a user's args actually changed.
    """
    global _baseline_opts
    if _baseline_opts is None:
        from yt_dlp import parse_options  # imported lazily: ~1s

        _baseline_opts = parse_options([]).ydl_opts
    return _baseline_opts


def _terse(exc: BaseException) -> str:
    """optparse errors carry a full usage banner; keep only the error line."""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    if not lines:
        return exc.__class__.__name__
    for line in reversed(lines):
        marker = ": error: "
        if marker in line:
            return line.split(marker, 1)[1]
    return lines[-1]


def parse_extra_args(extra_args: Optional[str]) -> Dict[str, Any]:
    """Turn a yt-dlp CLI argument string into an option dict.

    Only options that differ from yt-dlp's defaults are returned, so a merge
    into a preset dict cannot silently reset ``format``/``outtmpl``.
    """
    if not extra_args or not extra_args.strip():
        return {}

    from yt_dlp import parse_options
    from yt_dlp.utils import DownloadError

    try:
        argv = shlex.split(extra_args)
    except ValueError as exc:
        raise ExtraArgsError(f"Could not split extra args: {exc}") from exc

    if not argv:
        return {}

    baseline = _baseline()
    try:
        parsed = parse_options(argv).ydl_opts
    except SystemExit as exc:  # optparse can exit the process on bad input
        raise ExtraArgsError(f"Invalid extra args (exit {exc.code})") from exc
    except DownloadError as exc:
        raise ExtraArgsError(f"Invalid extra args: {_terse(exc)}") from exc
    except Exception as exc:  # OptParseError and friends
        raise ExtraArgsError(f"Invalid extra args: {_terse(exc)}") from exc

    return {k: v for k, v in parsed.items() if baseline.get(k) != v}


def _reroot_outtmpl(user_tmpl: Any, target_dir: Path) -> Optional[Dict[str, str]]:
    """Re-anchor a user-supplied ``-o`` template under ``target_dir``.

    A custom output template is a legitimate advanced option, but it must not
    be able to point outside the downloads root. Absolute templates and any
    containing a ``..`` segment are dropped; the rest are joined onto the job's
    resolved destination.
    """
    if not user_tmpl:
        return None
    if isinstance(user_tmpl, str):
        user_tmpl = {"default": user_tmpl}
    if not isinstance(user_tmpl, dict):
        return None

    safe: Dict[str, str] = {}
    for key, value in user_tmpl.items():
        if not isinstance(value, str) or not value.strip():
            continue
        normalised = value.replace("\\", "/")
        if normalised.startswith("/") or PureWindowsPath(value).drive:
            continue
        if any(part == ".." for part in PurePosixPath(normalised).parts):
            continue
        safe[key] = str(Path(target_dir) / normalised)
    return safe or None


def archive_path_for(target_dir: Path) -> Path:
    """Per-destination download archive.

    Playlist children use an archive so re-adding a playlist skips items that
    are already fetched. Keying it by destination folder (rather than one
    global file) means the same playlist added into a *different* folder is
    downloaded again instead of silently reporting "done" with no files.
    """
    import hashlib

    try:
        rel = Path(target_dir).resolve().relative_to(config.DOWNLOADS_ROOT.resolve())
        key = rel.as_posix() or "."
    except (ValueError, OSError):
        key = str(target_dir)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    folder = config.CONFIG_DIR / "archives"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{digest}.txt"


def build_opts(
    *,
    preset: str = DEFAULT_PRESET,
    target_dir: Path,
    extra_args: Optional[str] = None,
    flat: bool = False,
    progress_hooks: Optional[List[Any]] = None,
    postprocessor_hooks: Optional[List[Any]] = None,
    logger: Any = None,
    use_archive: bool = False,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the full yt-dlp option dict for one job."""
    opts: Dict[str, Any] = preset_options(preset)

    opts.update(
        {
            "outtmpl": {"default": str(Path(target_dir) / OUTTMPL)},
            "paths": {"home": str(target_dir)},
            "continuedl": True,
            "noprogress": True,
            "no_color": True,
            "ignoreerrors": False,
            "noplaylist": True,
            "retries": 5,
            "fragment_retries": 5,
            "restrictfilenames": False,
            "windowsfilenames": True,
            "overwrites": False,
            "nopart": False,
            "consoletitle": False,
        }
    )

    if use_archive:
        opts["download_archive"] = str(archive_path_for(target_dir))

    if config.COOKIES_FILE.is_file():
        opts["cookiefile"] = str(config.COOKIES_FILE)

    if logger is not None:
        opts["logger"] = logger
    if progress_hooks:
        opts["progress_hooks"] = list(progress_hooks)
    if postprocessor_hooks:
        opts["postprocessor_hooks"] = list(postprocessor_hooks)

    if flat:
        # Fast enumeration pass: no download, no archive filtering.
        opts.update(
            {
                "extract_flat": "in_playlist",
                "skip_download": True,
                "noplaylist": False,
                "playlist_items": None,
            }
        )
        opts.pop("download_archive", None)
        opts.pop("postprocessors", None)
        opts.pop("format", None)
        opts.pop("merge_output_format", None)

    extra = parse_extra_args(extra_args)
    if extra:
        # Destination keys are never user-overridable: "-P /etc" or
        # "-o /config/x" would otherwise write outside DOWNLOADS_ROOT and break
        # the "paths can never escape the mount" invariant.
        extra.pop("paths", None)
        user_tmpl = extra.pop("outtmpl", None)
        rerooted = _reroot_outtmpl(user_tmpl, Path(target_dir))
        if rerooted:
            opts["outtmpl"] = rerooted

        # Merge postprocessors additively; everything else is an override.
        extra_pps = extra.pop("postprocessors", None)
        opts.update(extra)
        if extra_pps:
            opts["postprocessors"] = list(opts.get("postprocessors") or []) + [
                pp for pp in extra_pps if pp not in (opts.get("postprocessors") or [])
            ]

    # Applied last, so a proxy the pool picked for a must-be-proxied domain
    # cannot be undone by a user's "--proxy" in extra args. When the pool chose
    # nothing (direct job) a user-supplied --proxy still applies.
    if proxy:
        opts["proxy"] = proxy

    return opts


def ytdlp_version() -> str:
    try:
        from importlib.metadata import version

        return version("yt-dlp")
    except Exception:
        try:
            import yt_dlp

            return getattr(yt_dlp.version, "__version__", "unknown")
        except Exception:
            return "unknown"
