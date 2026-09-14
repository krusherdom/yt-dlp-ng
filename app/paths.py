"""Safe resolution of user-supplied subfolders under DOWNLOADS_ROOT."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional

from fastapi import HTTPException

from . import config


def _root(root: Optional[Path] = None) -> Path:
    return Path(root).resolve() if root is not None else config.DOWNLOADS_ROOT


def safe_resolve(subfolder: Optional[str], root: Optional[Path] = None) -> Path:
    """Resolve ``subfolder`` under the downloads root.

    Returns the absolute path. Raises ``HTTPException(400)`` if the result
    would escape the root (``..`` traversal, absolute paths, drive letters,
    UNC paths, NUL bytes).
    """
    base = _root(root)

    if subfolder is None:
        return base
    if not isinstance(subfolder, str):
        raise HTTPException(status_code=400, detail="Invalid subfolder")

    raw = subfolder.strip()
    if "\x00" in raw:
        raise HTTPException(status_code=400, detail="Invalid subfolder")

    normalised = raw.replace("\\", "/")
    if normalised.strip("/") in ("", "."):
        # "", ".", "/" and "\" all mean "the root itself".
        return base

    # Reject absolute input outright rather than silently reinterpreting it as
    # relative. Checked with both flavours so a Windows drive/UNC spec is caught
    # when running on Linux and vice versa.
    if (
        normalised.startswith("/")
        or PureWindowsPath(raw).is_absolute()
        or PureWindowsPath(raw).drive
        or PurePosixPath(normalised).is_absolute()
    ):
        raise HTTPException(status_code=400, detail="Subfolder must be relative")

    candidate = Path(normalised)

    try:
        resolved = (base / candidate).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid subfolder")

    if resolved != base and not resolved.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Subfolder escapes downloads root")

    return resolved


def relative_to_root(path: Path, root: Optional[Path] = None) -> str:
    base = _root(root)
    try:
        rel = path.relative_to(base)
    except ValueError:
        return ""
    return "" if str(rel) == "." else rel.as_posix()


def list_folders(rel: Optional[str] = None, root: Optional[Path] = None) -> List[dict]:
    """List immediate subdirectories of ``rel`` under the downloads root."""
    base = _root(root)
    target = safe_resolve(rel, root=base)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")

    entries: List[dict] = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                entries.append(
                    {"name": child.name, "path": relative_to_root(child, base)}
                )
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot list folder: {exc}")

    parent = None
    if target != base:
        parent = relative_to_root(target.parent, base)

    return [{"path": relative_to_root(target, base), "parent": parent, "folders": entries}]


def create_folder(rel: str, root: Optional[Path] = None) -> str:
    """Create a folder (including parents) under the downloads root."""
    base = _root(root)
    if not rel or not rel.strip():
        raise HTTPException(status_code=400, detail="Folder name required")
    target = safe_resolve(rel, root=base)
    if target == base:
        raise HTTPException(status_code=400, detail="Folder name required")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot create folder: {exc}")
    return relative_to_root(target, base)
