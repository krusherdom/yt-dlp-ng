"""Proxy pool: routing rules, round-robin, settings persistence, migration.

Nothing here touches the network: ``proxies.check_sync`` is replaced by a fake
that reports health from a dict and records every call, which doubles as the
assertion for "stale entries are re-checked inline".
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from pydantic import ValidationError

from app import config, db, proxies, settings, ytdl
from app.models import ProxyEntry, ProxySettings, ProxyStatus


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_pool(monkeypatch):
    """Isolate module state: no cached settings, no health, no listeners."""
    proxies.reset_state()
    monkeypatch.setattr(settings, "_cached", None)
    monkeypatch.setattr(proxies, "on_change", [])
    yield
    proxies.reset_state()


def install(monkeypatch, st: ProxySettings, health: dict) -> list:
    """Make ``st`` the live settings and stub the health check.

    ``health`` maps proxy id (or bare URL) -> live bool. Returns the list that
    records each check_sync call.
    """
    monkeypatch.setattr(settings, "_cached", st)
    proxies.reset_statuses_for(st)
    calls: list = []

    def fake_check(entry_or_url, test_url=None, timeout_s=None):
        pid = getattr(entry_or_url, "id", "")
        url = getattr(entry_or_url, "url", entry_or_url)
        calls.append(pid or url)
        live = bool(health.get(pid, health.get(url, False)))
        status = ProxyStatus(
            id=pid, live=live, latency_ms=11, last_checked="2026-01-01T00:00:00+00:00"
        )
        proxies._store(status)
        return status

    monkeypatch.setattr(proxies, "check_sync", fake_check)
    return calls


def entry(pid: str, url: str, **kw) -> ProxyEntry:
    return ProxyEntry(id=pid, url=url, **kw)


# --------------------------------------------------------------------------
# host_matches
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,domains,expected",
    [
        ("xvideos.com", ["xvideos.com"], True),
        ("www.xvideos.com", ["xvideos.com"], True),
        ("cdn.eu.xvideos.com", ["xvideos.com"], True),
        ("XVideos.COM", ["xvideos.com"], True),
        # the domain list may be written with a www. prefix; it is normalised
        ("xvideos.com", ["www.xvideos.com"], True),
        # dot-boundary only
        ("notxvideos.com", ["xvideos.com"], False),
        ("xvideos.com.evil.net", ["xvideos.com"], False),
        ("youtube.com", ["xvideos.com"], False),
        ("", ["xvideos.com"], False),
        ("xvideos.com", [], False),
        ("xvideos.com", [""], False),
        # multiple domains
        ("clips.example.org", ["xvideos.com", "example.org"], True),
    ],
)
def test_host_matches(host, domains, expected):
    assert proxies.host_matches(host, domains) is expected


# --------------------------------------------------------------------------
# needs_proxy / acquire
# --------------------------------------------------------------------------


def test_needs_proxy_domains_mode(monkeypatch):
    st = ProxySettings(
        proxies=[entry("a", "http://10.0.0.5:8888")],
        mode="domains",
        domains=["xvideos.com"],
    )
    install(monkeypatch, st, {"a": True})
    assert proxies.needs_proxy("https://www.xvideos.com/video123/x") is True
    assert proxies.needs_proxy("https://www.youtube.com/watch?v=abc") is False


def test_needs_proxy_all_mode(monkeypatch):
    st = ProxySettings(proxies=[entry("a", "http://10.0.0.5:8888")], mode="all")
    install(monkeypatch, st, {"a": True})
    assert proxies.needs_proxy("https://www.youtube.com/watch?v=abc") is True
    assert proxies.needs_proxy("https://www.xvideos.com/x") is True


def test_needs_proxy_false_without_configured_proxies(monkeypatch):
    install(monkeypatch, ProxySettings(mode="all"), {})
    assert proxies.needs_proxy("https://www.youtube.com/watch?v=abc") is False


def test_acquire_returns_none_when_not_needed(monkeypatch):
    st = ProxySettings(
        proxies=[entry("a", "http://10.0.0.5:8888")],
        mode="domains",
        domains=["xvideos.com"],
    )
    calls = install(monkeypatch, st, {"a": True})
    assert proxies.acquire("https://www.youtube.com/watch?v=abc") is None
    assert calls == []  # a direct job never pays for a health check


def test_acquire_round_robin_and_exclude(monkeypatch):
    st = ProxySettings(
        proxies=[entry("a", "http://a:8888"), entry("b", "socks5://b:1080")],
        mode="all",
    )
    install(monkeypatch, st, {"a": True, "b": True})

    url = "https://www.youtube.com/watch?v=abc"
    first = proxies.acquire(url)
    second = proxies.acquire(url)
    third = proxies.acquire(url)
    assert [first.id, second.id, third.id] == ["a", "b", "a"]

    # Excluding the one just used hands back the other.
    assert proxies.acquire(url, exclude={"a"}).id == "b"
    assert proxies.acquire(url, exclude={"b"}).id == "a"


def test_acquire_skips_disabled_and_dead(monkeypatch):
    st = ProxySettings(
        proxies=[
            entry("dead", "http://dead:9"),
            entry("off", "http://off:8888", enabled=False),
            entry("good", "http://good:8888"),
        ],
        mode="all",
    )
    calls = install(monkeypatch, st, {"good": True, "dead": False, "off": True})

    for _ in range(3):
        assert proxies.acquire("https://example.com/x").id == "good"
    assert "off" not in calls  # disabled proxies are never checked by acquire


def test_acquire_checks_unchecked_then_reuses_fresh_status(monkeypatch):
    st = ProxySettings(proxies=[entry("a", "http://a:8888")], mode="all")
    calls = install(monkeypatch, st, {"a": True})

    proxies.acquire("https://example.com/x")
    assert calls == ["a"]  # unchecked -> checked inline

    proxies.acquire("https://example.com/x")
    assert calls == ["a"]  # still fresh -> no second check


def test_acquire_rechecks_stale_status(monkeypatch):
    st = ProxySettings(
        proxies=[entry("a", "http://a:8888")], mode="all", check_interval_s=30
    )
    calls = install(monkeypatch, st, {"a": True})

    proxies.acquire("https://example.com/x")
    assert calls == ["a"]

    # Pretend the last check happened well over check_interval_s ago.
    proxies._checked_at["a"] -= 10_000
    proxies.acquire("https://example.com/x")
    assert calls == ["a", "a"]


def test_acquire_raises_when_nothing_live(monkeypatch):
    st = ProxySettings(
        proxies=[entry("a", "http://a:8888"), entry("b", "http://b:8888")],
        mode="domains",
        domains=["xvideos.com"],
    )
    install(monkeypatch, st, {"a": False, "b": False})

    with pytest.raises(proxies.NoLiveProxy) as excinfo:
        proxies.acquire("https://www.xvideos.com/video1")
    assert excinfo.value.host == "www.xvideos.com"


def test_acquire_raises_when_every_live_proxy_excluded(monkeypatch):
    st = ProxySettings(proxies=[entry("a", "http://a:8888")], mode="all")
    install(monkeypatch, st, {"a": True})
    with pytest.raises(proxies.NoLiveProxy):
        proxies.acquire("https://example.com/x", exclude={"a"})


def test_statuses_and_summary(monkeypatch):
    st = ProxySettings(
        proxies=[
            entry("a", "http://a:8888"),
            entry("b", "http://b:8888"),
            entry("c", "http://c:8888", enabled=False),
        ],
        mode="all",
    )
    install(monkeypatch, st, {"a": True, "b": False})

    rows = proxies.statuses()
    assert [r.id for r in rows] == ["a", "b", "c"]
    assert all(r.live is None for r in rows)  # nothing checked yet
    assert proxies.summary() == {"configured": 3, "enabled": 2, "live": 0}

    proxies.acquire("https://example.com/x")
    assert proxies.summary() == {"configured": 3, "enabled": 2, "live": 1}
    by_id = {r.id: r for r in proxies.statuses()}
    assert by_id["a"].live is True and by_id["b"].live is False
    assert by_id["c"].live is None


def test_reset_statuses_for_drops_removed_and_adds_new(monkeypatch):
    st = ProxySettings(proxies=[entry("a", "http://a:8888")], mode="all")
    install(monkeypatch, st, {"a": True})
    proxies.acquire("https://example.com/x")
    assert proxies._statuses["a"].live is True

    new = ProxySettings(proxies=[entry("b", "http://b:8888")], mode="all")
    monkeypatch.setattr(settings, "_cached", new)
    proxies.reset_statuses_for(new)
    assert "a" not in proxies._statuses
    assert proxies._statuses["b"].live is None


def test_describe_prefers_label_then_masks(monkeypatch):
    assert proxies.describe(None) is None
    assert proxies.describe(entry("a", "http://a:8888", label="gluetun")) == "gluetun"
    assert (
        proxies.describe(entry("a", "http://bob:pw@a:8888"))
        == "http://bob:***@a:8888"
    )


def test_check_all_notifies_listeners(monkeypatch):
    st = ProxySettings(proxies=[entry("a", "http://a:8888")], mode="all")
    install(monkeypatch, st, {"a": True})

    seen: list = []

    async def listener(rows):
        seen.append([r.id for r in rows])

    proxies.on_change.append(listener)
    result = asyncio.run(proxies.check_all())
    assert [r.id for r in result] == ["a"]
    assert result[0].live is True
    assert seen == [["a"]]


def test_check_one_unknown_id_raises(monkeypatch):
    install(monkeypatch, ProxySettings(), {})
    with pytest.raises(KeyError):
        asyncio.run(proxies.check_one("nope"))


# --------------------------------------------------------------------------
# Model validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host:2121",  # unsupported scheme
        "host:8080",  # no scheme
        "http://host",  # no explicit port
        "http://:8080",  # no host
        "http://ho st:8080",  # whitespace
        "",
    ],
)
def test_proxy_entry_rejects_bad_urls(url):
    with pytest.raises(ValidationError):
        ProxyEntry(url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8888",
        "https://proxy.lan:3128",
        "socks4://10.0.0.5:1080",
        "socks5://10.0.0.5:1080",
        "socks5h://user:pass@10.0.0.5:1080",
    ],
)
def test_proxy_entry_accepts_supported_urls(url):
    assert ProxyEntry(url=url).url == url


def test_proxy_entry_generates_unique_ids():
    a, b = ProxyEntry(url="http://a:1"), ProxyEntry(url="http://b:2")
    assert a.id and b.id and a.id != b.id


def test_proxy_settings_rejects_duplicate_urls():
    with pytest.raises(ValidationError):
        ProxySettings(
            proxies=[
                ProxyEntry(url="http://a:8888"),
                ProxyEntry(url="http://a:8888"),
            ]
        )


def test_proxy_settings_normalises_domains():
    st = ProxySettings(
        domains=[
            "  https://WWW.XVideos.com/some/path?q=1  ",
            "xvideos.com",
            "",
            "   ",
            "Example.ORG:8443",
            "youtube.com.",
        ]
    )
    assert st.domains == ["xvideos.com", "example.org", "youtube.com"]


def test_proxy_settings_bounds_and_defaults():
    st = ProxySettings()
    assert st.mode == "domains"
    assert st.check_interval_s == 300 and st.timeout_s == 8
    assert st.test_url == config.PROXY_TEST_URL
    with pytest.raises(ValidationError):
        ProxySettings(check_interval_s=5)
    with pytest.raises(ValidationError):
        ProxySettings(check_interval_s=999_999)
    with pytest.raises(ValidationError):
        ProxySettings(timeout_s=1)
    with pytest.raises(ValidationError):
        ProxySettings(timeout_s=600)
    with pytest.raises(ValidationError):
        ProxySettings(mode="sometimes")


# --------------------------------------------------------------------------
# Masking + persistence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,masked",
    [
        ("http://10.0.0.5:8888", "http://10.0.0.5:8888"),
        ("socks5://bob:hunter2@h.lan:1080", "socks5://bob:***@h.lan:1080"),
        ("http://user:p%40ss@h:3128", "http://user:***@h:3128"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("http://bob:pw@[::1]:8080", "http://bob:***@[::1]:8080"),
        ("", ""),
    ],
)
def test_mask_url(raw, masked):
    assert settings.mask_url(raw) == masked


def test_public_settings_masks_every_url(monkeypatch):
    st = ProxySettings(
        proxies=[
            entry("a", "http://bob:pw@a.lan:8888", label="one"),
            entry("b", "socks5://b.lan:1080"),
        ]
    )
    install(monkeypatch, st, {})
    data = settings.public_settings()
    assert [p["url"] for p in data["proxies"]] == [
        "http://bob:***@a.lan:8888",
        "socks5://b.lan:1080",
    ]
    assert data["mode"] == "domains"


def test_settings_save_load_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", path)

    st = ProxySettings(
        proxies=[entry("a", "http://bob:pw@a.lan:8888", label="gluetun")],
        mode="domains",
        domains=["XVIDEOS.com"],
        check_interval_s=60,
        timeout_s=4,
    )
    asyncio.run(settings.save(st))

    assert path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["proxies"][0]["url"] == "http://bob:pw@a.lan:8888"

    settings.reset_cache()
    loaded = asyncio.run(settings.load())
    assert loaded.proxies[0].url == "http://bob:pw@a.lan:8888"
    assert loaded.proxies[0].label == "gluetun"
    assert loaded.domains == ["xvideos.com"]
    assert loaded.check_interval_s == 60 and loaded.timeout_s == 4
    assert settings.get().mode == "domains"


def test_settings_load_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "nope.json")
    loaded = asyncio.run(settings.load())
    assert loaded.proxies == [] and loaded.mode == "domains"


@pytest.mark.parametrize(
    "content", ["{not json", "[]", json.dumps({"proxies": [{"url": "nope"}]})]
)
def test_settings_load_corrupt_file_returns_defaults(tmp_path, monkeypatch, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    loaded = asyncio.run(settings.load())
    assert loaded.proxies == [] and loaded.mode == "domains"


def test_save_keeps_credentials_when_ui_returns_the_mask(tmp_path, monkeypatch):
    """A PUT that echoes back what GET masked must not persist ``***``."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", path)

    original = "socks5://bob:hunter2@h.lan:1080"
    asyncio.run(settings.save(ProxySettings(proxies=[entry("a", original)])))

    echoed = ProxySettings(**settings.public_settings())
    assert echoed.proxies[0].url == "socks5://bob:***@h.lan:1080"

    saved = asyncio.run(settings.save(echoed))
    assert saved.proxies[0].url == original
    assert json.loads(path.read_text(encoding="utf-8"))["proxies"][0]["url"] == original


def test_save_updates_statuses_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    asyncio.run(settings.save(ProxySettings(proxies=[entry("a", "http://a:8888")])))
    assert set(proxies._statuses) == {"a"}
    asyncio.run(settings.save(ProxySettings(proxies=[entry("b", "http://b:8888")])))
    assert set(proxies._statuses) == {"b"}
    assert settings.get().proxies[0].id == "b"


# --------------------------------------------------------------------------
# ytdl wiring
# --------------------------------------------------------------------------


def test_build_opts_sets_proxy(tmp_path):
    opts = ytdl.build_opts(target_dir=tmp_path, proxy="socks5://10.0.0.5:1080")
    assert opts["proxy"] == "socks5://10.0.0.5:1080"


def test_build_opts_sets_proxy_in_flat_mode(tmp_path):
    opts = ytdl.build_opts(target_dir=tmp_path, flat=True, proxy="http://10.0.0.5:8888")
    assert opts["proxy"] == "http://10.0.0.5:8888"
    assert opts["extract_flat"] == "in_playlist"


def test_build_opts_without_proxy_leaves_key_unset(tmp_path):
    assert "proxy" not in ytdl.build_opts(target_dir=tmp_path)
    assert "proxy" not in ytdl.build_opts(target_dir=tmp_path, proxy="")


def test_pool_proxy_overrides_extra_args(tmp_path):
    opts = ytdl.build_opts(
        target_dir=tmp_path,
        extra_args="--proxy http://user-chosen:1234",
        proxy="http://pool:8888",
    )
    assert opts["proxy"] == "http://pool:8888"


# --------------------------------------------------------------------------
# db migration
# --------------------------------------------------------------------------


def _columns(path) -> set:
    con = sqlite3.connect(str(path))
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    finally:
        con.close()


def test_migration_adds_proxy_column_to_old_db(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    old_schema = db.SCHEMA.replace("    proxy       TEXT,\n", "")
    assert "proxy" not in old_schema

    con = sqlite3.connect(str(path))
    con.executescript(old_schema)
    con.execute(
        """INSERT INTO jobs (id, url, preset, subfolder, extra_args, status,
                             progress, type, created_at, updated_at)
           VALUES ('old1','https://example.com/v','best','','','done',100,'single',
                   '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"""
    )
    con.commit()
    con.close()
    assert "proxy" not in _columns(path)

    monkeypatch.setattr(config, "DB_PATH", path)
    previous = db._conn
    db._conn = None

    async def scenario():
        await db.init_db()
        try:
            job = await db.get_job("old1")
            assert job is not None and job["proxy"] is None
            updated = await db.update_job("old1", proxy="gluetun")
            assert updated is not None and updated["proxy"] == "gluetun"
            # Idempotent: a second pass must not try to add the column again.
            assert await db._migrate(db._require()) == 0
        finally:
            await db.close_db()

    try:
        asyncio.run(scenario())
    finally:
        db._conn = previous

    assert "proxy" in _columns(path)


def test_new_db_has_proxy_column(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    previous = db._conn
    db._conn = None

    async def scenario():
        await db.init_db()
        try:
            job = await db.create_job(url="https://example.com/v")
            assert job["proxy"] is None
            rows = await db.list_jobs()
            assert all("proxy" in r for r in rows)
        finally:
            await db.close_db()

    try:
        asyncio.run(scenario())
    finally:
        db._conn = previous
