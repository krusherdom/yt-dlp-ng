"""Proxy pool API: settings CRUD, credential masking, and the test endpoint.

Uses FastAPI's TestClient (ASGI, via httpx) against the real app, mirroring
tests/test_cookies.py's pattern: a module-scoped client (lifespan runs once)
plus a per-test fixture that points ``app.config.SETTINGS_FILE`` at a
throwaway ``tmp_path`` and resets the settings/proxies module caches, so
tests never touch the shared session config dir from tests/conftest.py and
never collide with each other.

``app.proxies.check_sync`` is monkeypatched to a fast stub for the settings
CRUD tests (a PUT schedules a real ``check_all()`` as a background task,
which would otherwise hit the network for up to ``timeout_s`` seconds per
proxy). The dead-proxy test below deliberately does NOT stub it -- it needs
the real health check to prove a proxy nobody is listening on comes back
``live: false`` within the bound timeout.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, proxies, settings
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def proxy_settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    settings.reset_cache()
    proxies.reset_state()
    yield path
    settings.reset_cache()
    proxies.reset_state()


@pytest.fixture
def fast_check(monkeypatch):
    """Stand in for proxies.check_sync: instant, deterministic, always down.

    Still writes into the real in-memory status table via the module's own
    ``_store`` so statuses()/summary() behave exactly as they would with a
    real (slow) check that happened to fail.
    """
    from app.models import ProxyEntry, ProxyStatus

    def _stub(entry_or_url, test_url=None, timeout_s=None):
        proxy_id = entry_or_url.id if isinstance(entry_or_url, ProxyEntry) else ""
        status = ProxyStatus(
            id=proxy_id,
            live=False,
            latency_ms=1,
            last_checked="2026-01-01T00:00:00+00:00",
            last_error="stubbed: not actually checked",
        )
        proxies._store(status)
        return status

    monkeypatch.setattr(proxies, "check_sync", _stub)
    return _stub


def test_put_valid_settings_then_get_masks_urls(client, proxy_settings_file, fast_check):
    body = {
        "proxies": [
            {"url": "http://user:secret@10.0.0.5:8888", "label": "squid", "enabled": True}
        ],
        "mode": "domains",
        "domains": ["xvideos.com"],
        "test_url": "https://www.google.com/generate_204",
        "check_interval_s": 300,
        "timeout_s": 8,
    }
    resp = client.put("/api/proxies", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["settings"]["proxies"][0]["url"] == "http://user:***@10.0.0.5:8888"
    assert data["settings"]["proxies"][0]["label"] == "squid"
    assert data["settings"]["domains"] == ["xvideos.com"]
    assert "statuses" in data and "summary" in data
    assert data["summary"]["configured"] == 1

    resp2 = client.get("/api/proxies")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["settings"]["proxies"][0]["url"] == "http://user:***@10.0.0.5:8888"
    assert data2["settings"]["mode"] == "domains"
    assert data2["settings"]["domains"] == ["xvideos.com"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://10.0.0.5:21",
        "10.0.0.5:8888",         # no scheme
        "http://10.0.0.5",       # no port
        "http://",                # no host
    ],
)
def test_put_bad_proxy_url_rejected(client, proxy_settings_file, fast_check, url):
    resp = client.put("/api/proxies", json={"proxies": [{"url": url}]})
    assert 400 <= resp.status_code < 500
    assert "detail" in resp.json()


def test_put_with_masked_url_for_existing_id_keeps_stored_url(client, proxy_settings_file, fast_check):
    create = client.put(
        "/api/proxies",
        json={"proxies": [{"url": "socks5://bob:hunter2@10.0.0.9:1080", "label": "socks"}]},
    )
    assert create.status_code == 200
    entry = create.json()["settings"]["proxies"][0]
    proxy_id = entry["id"]
    assert entry["url"] == "socks5://bob:***@10.0.0.9:1080"

    # The UI round-trips the masked value it was given for this id, but also
    # changes the label -- only the URL should be protected from clobbering.
    roundtrip = client.put(
        "/api/proxies",
        json={
            "proxies": [
                {
                    "id": proxy_id,
                    "url": "socks5://bob:***@10.0.0.9:1080",
                    "label": "socks renamed",
                    "enabled": True,
                }
            ]
        },
    )
    assert roundtrip.status_code == 200
    out = roundtrip.json()["settings"]["proxies"][0]
    assert out["label"] == "socks renamed"
    assert out["url"] == "socks5://bob:***@10.0.0.9:1080"   # still masked in the response

    # The real secret must have survived underneath, verified server-side.
    stored = settings.get()
    assert len(stored.proxies) == 1
    assert stored.proxies[0].url == "socks5://bob:hunter2@10.0.0.9:1080"
    assert stored.proxies[0].label == "socks renamed"


def test_test_endpoint_with_unreachable_proxy_is_not_live(client, proxy_settings_file):
    # Deliberately no fast_check stub: this exercises the real health check
    # against a proxy port nobody is listening on, and must come back False
    # well inside the (default) timeout bound rather than hanging or erroring.
    resp = client.post("/api/proxies/test", json={"url": "http://127.0.0.1:9"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["live"] is False
    assert data["url"] == "http://127.0.0.1:9"   # no credentials to mask


def test_test_endpoint_requires_url_or_id(client, proxy_settings_file):
    resp = client.post("/api/proxies/test", json={})
    assert resp.status_code == 400


def test_test_endpoint_unknown_id_is_404(client, proxy_settings_file):
    resp = client.post("/api/proxies/test", json={"id": "does-not-exist"})
    assert resp.status_code == 404


def test_get_proxies_defaults_when_nothing_saved(client, proxy_settings_file):
    resp = client.get("/api/proxies")
    assert resp.status_code == 200
    data = resp.json()
    assert data["settings"]["proxies"] == []
    assert data["settings"]["mode"] == "domains"
    assert data["summary"] == {"configured": 0, "enabled": 0, "live": 0}


def test_check_endpoint_returns_statuses_and_summary(client, proxy_settings_file, fast_check):
    client.put("/api/proxies", json={"proxies": [{"url": "http://10.0.0.7:8080"}]})
    resp = client.post("/api/proxies/check")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["statuses"]) == 1
    assert data["statuses"][0]["live"] is False   # stubbed
    assert data["summary"]["configured"] == 1


def test_status_and_ws_snapshot_include_proxies(client, proxy_settings_file, fast_check):
    client.put("/api/proxies", json={"proxies": [{"url": "http://10.0.0.7:8080"}]})
    status = client.get("/api/status").json()
    assert "proxies" in status
    assert status["proxies"]["configured"] == 1

    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert "proxies" in msg
        assert "statuses" in msg["proxies"]
        assert "summary" in msg["proxies"]
        assert msg["proxies"]["summary"]["configured"] == 1
