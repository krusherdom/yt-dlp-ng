"""Cookies upload/delete API: validation, atomic write, status wiring.

Uses FastAPI's TestClient (ASGI, via httpx) against the real app, with
``config.COOKIES_FILE`` monkeypatched per-test into a throwaway tmp_path so
tests never touch the shared session config dir set up by conftest.py, and
never collide with each other.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

NETSCAPE_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    "# This is a generated file!  Do not edit.\n"
    "\n"
    ".imaglr.com\tTRUE\t/\tFALSE\t2145916800\tsession\tabc123\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2145916800\tVISITOR_INFO1_LIVE\txyz789\n"
).encode("utf-8")

# No Netscape header, but still a real cookie row (7 tab-separated fields).
HEADERLESS_COOKIES = (
    ".example.com\tTRUE\t/\tFALSE\t2145916800\tname\tvalue\n"
).encode("utf-8")

GARBAGE = b"just some random text\nthat is not a cookies file at all\n"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def cookies_path(tmp_path, monkeypatch):
    path = tmp_path / "cookies.txt"
    monkeypatch.setattr(config, "COOKIES_FILE", path)
    return path


def test_upload_valid_netscape_cookies(client, cookies_path):
    resp = client.post(
        "/api/cookies", files={"file": ("cookies.txt", NETSCAPE_COOKIES, "text/plain")}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cookies_detected"] is True
    assert body["domains"] == ["imaglr.com", "youtube.com"]
    assert body["count"] == 2
    assert body["updated_at"]
    assert cookies_path.is_file()


def test_upload_recognizes_headerless_tab_separated_file(client, cookies_path):
    resp = client.post(
        "/api/cookies", files={"file": ("c.txt", HEADERLESS_COOKIES, "text/plain")}
    )
    assert resp.status_code == 200
    assert resp.json()["domains"] == ["example.com"]


def test_upload_garbage_rejected(client, cookies_path):
    resp = client.post("/api/cookies", files={"file": ("cookies.txt", GARBAGE, "text/plain")})
    assert resp.status_code == 400
    assert "detail" in resp.json()
    assert not cookies_path.exists()


def test_upload_rejects_oversized_file(client, cookies_path):
    huge = b"# Netscape HTTP Cookie File\n" + b"x" * (2 * 1024 * 1024 + 100)
    resp = client.post("/api/cookies", files={"file": ("big.txt", huge, "text/plain")})
    assert resp.status_code == 400
    assert not cookies_path.exists()


def test_status_reflects_uploaded_domains(client, cookies_path):
    client.post("/api/cookies", files={"file": ("cookies.txt", NETSCAPE_COOKIES, "text/plain")})
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cookies_detected"] is True
    assert body["cookies_domains"] == ["imaglr.com", "youtube.com"]
    assert body["cookies_updated_at"]


def test_delete_cookies(client, cookies_path):
    client.post("/api/cookies", files={"file": ("cookies.txt", NETSCAPE_COOKIES, "text/plain")})
    resp = client.delete("/api/cookies")
    assert resp.status_code == 200
    assert resp.json() == {"cookies_detected": False}
    assert not cookies_path.exists()

    status = client.get("/api/status").json()
    assert status["cookies_detected"] is False
    assert status["cookies_domains"] == []
    assert status["cookies_updated_at"] is None


def test_delete_when_no_cookies_file_is_a_noop(client, cookies_path):
    resp = client.delete("/api/cookies")
    assert resp.status_code == 200
    assert resp.json() == {"cookies_detected": False}


def test_get_cookies_not_allowed(client):
    resp = client.get("/api/cookies")
    assert resp.status_code in (404, 405)
