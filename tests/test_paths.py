"""Path safety: nothing may resolve outside DOWNLOADS_ROOT."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import paths

TRAVERSAL = [
    "../../etc",
    "..",
    "../",
    "a/../../..",
    "a/b/../../../outside",
    "./../secrets",
    "..\\..\\Windows",
    "ok/../..",
]

ABSOLUTE = [
    "/etc",
    "/etc/passwd",
    "C:/Windows",
    "C:\\Windows\\System32",
    "//server/share",
    "\\\\server\\share",
]


@pytest.mark.parametrize("bad", TRAVERSAL)
def test_traversal_is_rejected(downloads_root, bad):
    with pytest.raises(HTTPException) as exc:
        paths.safe_resolve(bad, root=downloads_root)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("bad", ABSOLUTE)
def test_absolute_paths_are_rejected(downloads_root, bad):
    with pytest.raises(HTTPException) as exc:
        paths.safe_resolve(bad, root=downloads_root)
    assert exc.value.status_code == 400


def test_null_byte_rejected(downloads_root):
    with pytest.raises(HTTPException):
        paths.safe_resolve("ok\x00/evil", root=downloads_root)


@pytest.mark.parametrize("empty", ["", "   ", ".", "/", None])
def test_empty_resolves_to_root(downloads_root, empty):
    assert paths.safe_resolve(empty, root=downloads_root) == downloads_root.resolve()


def test_valid_subfolders(downloads_root):
    assert paths.safe_resolve("music", root=downloads_root) == (
        downloads_root / "music"
    ).resolve()
    assert paths.safe_resolve("a/b/c", root=downloads_root) == (
        downloads_root / "a" / "b" / "c"
    ).resolve()
    # Backslashes are normalised to separators, not silently dropped.
    assert paths.safe_resolve("a\\b", root=downloads_root) == (
        downloads_root / "a" / "b"
    ).resolve()
    # A '..' that stays inside the root is fine.
    assert paths.safe_resolve("a/../b", root=downloads_root) == (
        downloads_root / "b"
    ).resolve()


def test_relative_to_root(downloads_root):
    target = paths.safe_resolve("a/b", root=downloads_root)
    assert paths.relative_to_root(target, root=downloads_root) == "a/b"
    assert paths.relative_to_root(downloads_root, root=downloads_root) == ""


def test_create_folder_and_list(downloads_root):
    created = paths.create_folder("shows/season 1", root=downloads_root)
    assert created == "shows/season 1"
    assert (downloads_root / "shows" / "season 1").is_dir()

    listing = paths.list_folders(None, root=downloads_root)[0]
    assert listing["path"] == ""
    assert listing["parent"] is None
    assert [f["name"] for f in listing["folders"]] == ["shows"]

    sub = paths.list_folders("shows", root=downloads_root)[0]
    assert sub["parent"] == ""
    assert [f["path"] for f in sub["folders"]] == ["shows/season 1"]


def test_create_folder_rejects_traversal(downloads_root):
    with pytest.raises(HTTPException) as exc:
        paths.create_folder("../escape", root=downloads_root)
    assert exc.value.status_code == 400
    assert not (downloads_root.parent / "escape").exists()


def test_create_folder_requires_a_name(downloads_root):
    for bad in ("", "   ", "."):
        with pytest.raises(HTTPException):
            paths.create_folder(bad, root=downloads_root)


def test_list_folders_missing_dir(downloads_root):
    with pytest.raises(HTTPException) as exc:
        paths.list_folders("nope", root=downloads_root)
    assert exc.value.status_code == 404


def test_list_folders_hides_dotfolders(downloads_root):
    (downloads_root / ".hidden").mkdir()
    (downloads_root / "visible").mkdir()
    listing = paths.list_folders(None, root=downloads_root)[0]
    assert [f["name"] for f in listing["folders"]] == ["visible"]
