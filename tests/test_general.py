"""General settings, generic/direct import classification and the referer fold.

Covers the v0.4.0 "catch-all" work:

* ``settings.json`` grew a ``{"proxies": ..., "general": ...}`` envelope and
  must still load files written in the old bare-``ProxySettings`` shape.
* the importer now classifies every URL as ``site``/``direct``/``generic`` and
  rejects the sites yt-dlp refuses outright (DRM / piracy) with a reason.
* a Referer travels as ``--referer`` inside ``extra_args`` -- there is no
  column for it -- and a plainly-direct media URL skips playlist enumeration.
"""

from __future__ import annotations

import asyncio
import json
import shlex

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config, importer, proxies, settings, worker
from app.main import app
from app.models import GeneralSettings, ProxyEntry, ProxySettings

# A page with player markup but no bare absolute media URL in the text, so the
# <video>/<source>/<meta> harvesting is what has to find the files.
PLAYER_HTML = """<html><head>
  <meta property="og:video" content="https://cdn.example.com/og/clip.mp4">
  <meta name="twitter:player:stream" content="https://cdn.example.com/tw/stream.m3u8">
  <link rel="stylesheet" href="https://cdn.example.com/site.css">
</head><body>
  <video src="/media/local.mp4" poster="/p.jpg"></video>
  <audio><source src="../audio/track.m4a" type="audio/mp4"></audio>
  <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>
  <a href="#top">top</a>
  <a href="javascript:void(0)">nope</a>
</body></html>
"""


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Point app.config.SETTINGS_FILE at a throwaway file and clear caches."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    settings.reset_cache()
    proxies.reset_state()
    yield path
    settings.reset_cache()
    proxies.reset_state()


@pytest.fixture
def no_worker(monkeypatch):
    """Stop POST /api/jobs from actually queueing a download."""
    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(worker, "submit", _noop)
    monkeypatch.setattr(worker, "push_job", _noop)


# --------------------------------------------------------------------------
# Settings file format / migration
# --------------------------------------------------------------------------


def test_load_migrates_bare_proxysettings_file(settings_file):
    """An old file is a ProxySettings dump: its "proxies" key is a LIST."""
    legacy = ProxySettings(
        proxies=[ProxyEntry(id="p1", url="http://10.0.0.5:8888", label="one")],
        mode="all",
        domains=["example.com"],
        timeout_s=11,
    )
    settings_file.write_text(json.dumps(legacy.model_dump()), encoding="utf-8")

    loaded = asyncio.run(settings.load())
    assert [p.url for p in loaded.proxies] == ["http://10.0.0.5:8888"]
    assert loaded.mode == "all"
    assert loaded.timeout_s == 11
    # No general block on disk -> defaults, not an error.
    assert settings.get_general() == GeneralSettings()


def test_load_reads_new_envelope(settings_file):
    settings_file.write_text(
        json.dumps(
            {
                "proxies": ProxySettings(mode="all").model_dump(),
                "general": {"allow_generic": False, "default_referer": "https://ref.test/"},
            }
        ),
        encoding="utf-8",
    )
    loaded = asyncio.run(settings.load())
    assert loaded.mode == "all"
    assert settings.get_general().allow_generic is False
    assert settings.get_general().default_referer == "https://ref.test/"


def test_save_general_writes_envelope_and_keeps_proxies(settings_file):
    asyncio.run(settings.save(ProxySettings(mode="all", timeout_s=9)))
    asyncio.run(settings.save_general(GeneralSettings(allow_generic=False)))

    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert set(data) == {"proxies", "general"}
    assert isinstance(data["proxies"], dict)
    assert data["proxies"]["mode"] == "all"
    assert data["proxies"]["timeout_s"] == 9
    assert data["general"] == {"allow_generic": False, "default_referer": ""}

    # And a proxy save afterwards must not drop the general block.
    asyncio.run(settings.save(ProxySettings(mode="domains")))
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["general"]["allow_generic"] is False


def test_load_tolerates_broken_general_block(settings_file):
    settings_file.write_text(
        json.dumps(
            {
                "proxies": ProxySettings(mode="all").model_dump(),
                "general": {"default_referer": "ftp://nope"},
            }
        ),
        encoding="utf-8",
    )
    loaded = asyncio.run(settings.load())
    assert loaded.mode == "all"  # proxies survive a bad general section
    assert settings.get_general() == GeneralSettings()


# --------------------------------------------------------------------------
# GeneralSettings validation
# --------------------------------------------------------------------------


def test_general_settings_defaults():
    g = GeneralSettings()
    assert g.allow_generic is True
    assert g.default_referer == ""


@pytest.mark.parametrize("value", ["ftp://example.com/", "example.com", "https://a b/", "//x"])
def test_general_settings_rejects_bad_referer(value):
    with pytest.raises(ValidationError):
        GeneralSettings(default_referer=value)


@pytest.mark.parametrize("value", ["", "  ", "https://ref.example.com/page", "http://x.test/"])
def test_general_settings_accepts_referer(value):
    assert GeneralSettings(default_referer=value).default_referer == value.strip()


# --------------------------------------------------------------------------
# GET / PUT /api/settings/general
# --------------------------------------------------------------------------


def test_general_settings_endpoints_roundtrip(client, settings_file):
    body = client.get("/api/settings/general").json()
    assert body == {"allow_generic": True, "default_referer": ""}

    resp = client.put(
        "/api/settings/general",
        json={"allow_generic": False, "default_referer": "https://ref.example.com/"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "allow_generic": False,
        "default_referer": "https://ref.example.com/",
    }

    assert client.get("/api/settings/general").json()["allow_generic"] is False
    assert json.loads(settings_file.read_text(encoding="utf-8"))["general"][
        "allow_generic"
    ] is False


def test_general_settings_put_rejects_bad_referer(client, settings_file):
    resp = client.put(
        "/api/settings/general", json={"allow_generic": True, "default_referer": "ftp://x/"}
    )
    assert resp.status_code == 422


def test_status_payload_exposes_allow_generic(client, settings_file):
    assert client.get("/api/status").json()["allow_generic"] is True
    client.put("/api/settings/general", json={"allow_generic": False})
    assert client.get("/api/status").json()["allow_generic"] is False


# --------------------------------------------------------------------------
# Import classification
# --------------------------------------------------------------------------


def _by_url(result):
    return {c["url"]: c for c in result["candidates"]}


def test_validate_urls_classifies_site_direct_generic():
    candidates, rejected = importer.validate_urls(
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://cdn.example.invalid/files/clip.MP4?sig=abc&t=1",
            "https://some.odd.invalid/watch/page",
        ],
        allow_generic=True,
    )
    kinds = {c["url"]: c["kind"] for c in candidates}
    assert kinds["https://www.youtube.com/watch?v=dQw4w9WgXcQ"] == "site"
    assert kinds["https://cdn.example.invalid/files/clip.MP4?sig=abc&t=1"] == "direct"
    assert kinds["https://some.odd.invalid/watch/page"] == "generic"
    extractors = {c["url"]: c["extractor"] for c in candidates}
    assert extractors["https://cdn.example.invalid/files/clip.MP4?sig=abc&t=1"] == "direct"
    assert extractors["https://some.odd.invalid/watch/page"] == "generic"
    assert rejected == []


@pytest.mark.parametrize(
    "ext", sorted(importer.MEDIA_EXTENSIONS)
)
def test_is_direct_media_covers_every_extension(ext):
    assert importer.is_direct_media(f"https://h.invalid/a/b.{ext}")
    assert importer.is_direct_media(f"https://h.invalid/a/b.{ext.upper()}?x=1#f")


def test_is_direct_media_ignores_query_string_extensions():
    assert not importer.is_direct_media("https://h.invalid/watch?file=clip.mp4")
    assert not importer.is_direct_media("https://h.invalid/page.html")
    assert not importer.is_direct_media("https://h.invalid/")


def test_allow_generic_off_rejects_generic_urls():
    candidates, rejected = importer.validate_urls(
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://cdn.example.invalid/files/clip.mp4",
            "https://some.odd.invalid/watch/page",
        ],
        allow_generic=False,
    )
    # Known sites and plain media files still work with generic extraction off.
    assert [c["kind"] for c in candidates] == ["site", "direct"]
    assert rejected == [
        {"url": "https://some.odd.invalid/watch/page", "reason": "generic extraction disabled"}
    ]


def test_validate_urls_rejects_non_urls():
    _candidates, rejected = importer.validate_urls(["not a url", "ftp://x/y.mp4"])
    assert [r["reason"] for r in rejected] == ["not a URL", "not a URL"]


def test_piracy_and_drm_sites_are_rejected_with_reasons():
    """KnownPiracyIE / KnownDRMIE only ever raise, so never offer them."""
    candidates, rejected = importer.validate_urls(
        [
            "https://dood.to/e/5s1wmbdacezb",  # KnownPiracyIE
            "https://www.crunchyroll.com/watch/GY2P1Q98Y/to-the-future",  # KnownDRMIE
        ],
        allow_generic=True,
    )
    assert candidates == []
    assert rejected == [
        {"url": "https://dood.to/e/5s1wmbdacezb", "reason": "unsupported by yt-dlp (piracy)"},
        {
            "url": "https://www.crunchyroll.com/watch/GY2P1Q98Y/to-the-future",
            "reason": "unsupported by yt-dlp (DRM)",
        },
    ]


def test_piracy_rejection_wins_over_direct_media_extension():
    _candidates, rejected = importer.validate_urls(
        ["https://gofile.io/d/something.mp4"], allow_generic=True
    )
    assert rejected == [
        {"url": "https://gofile.io/d/something.mp4", "reason": "unsupported by yt-dlp (piracy)"}
    ]


def test_import_payload_shape():
    result = importer.import_payload(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", allow_generic=True
    )
    assert set(result) == {"candidates", "rejected", "total_found", "allow_generic"}
    assert result["allow_generic"] is True
    assert result["total_found"] == 1
    assert set(result["candidates"][0]) == {"url", "extractor", "kind"}


# --------------------------------------------------------------------------
# HTML media-link harvesting
# --------------------------------------------------------------------------


def test_extract_urls_harvests_player_markup_with_base_url():
    urls = importer.extract_urls(PLAYER_HTML, base_url="https://site.invalid/a/b/page.html")
    assert "https://cdn.example.com/og/clip.mp4" in urls
    assert "https://cdn.example.com/tw/stream.m3u8" in urls
    # <video src> is root-relative -> resolved against the base host.
    assert "https://site.invalid/media/local.mp4" in urls
    # <source src> is path-relative -> resolved against the base directory.
    assert "https://site.invalid/a/audio/track.m4a" in urls
    assert "https://www.youtube.com/embed/dQw4w9WgXcQ" in urls
    # Page furniture never becomes a candidate.
    assert "https://cdn.example.com/site.css" not in urls
    assert not any("javascript:" in u for u in urls)
    assert "https://site.invalid/a/b/page.html" not in urls  # the "#top" anchor


def test_extract_urls_drops_relative_media_without_base_url():
    urls = importer.extract_urls(PLAYER_HTML)
    assert "https://cdn.example.com/og/clip.mp4" in urls
    assert not any(u.endswith("/media/local.mp4") for u in urls)
    assert not any(u.endswith("/audio/track.m4a") for u in urls)


def test_extract_urls_dedupes_and_preserves_order_across_sources():
    html = (
        '<video src="https://h.invalid/1.mp4"></video>'
        '<a href="https://h.invalid/2.mp4">two</a>'
        " see also https://h.invalid/1.mp4"
    )
    assert importer.extract_urls(html) == [
        "https://h.invalid/1.mp4",
        "https://h.invalid/2.mp4",
    ]


def test_import_payload_html_classifies_direct_media():
    result = importer.import_payload(
        PLAYER_HTML, base_url="https://site.invalid/a/b/page.html", allow_generic=True
    )
    found = _by_url(result)
    assert found["https://cdn.example.com/og/clip.mp4"]["kind"] == "direct"
    assert found["https://site.invalid/media/local.mp4"]["kind"] == "direct"
    assert found["https://www.youtube.com/embed/dQw4w9WgXcQ"]["kind"] == "site"


def test_import_endpoint_accepts_base_url(client, settings_file):
    resp = client.post(
        "/api/import",
        data={"text": PLAYER_HTML, "base_url": "https://site.invalid/a/b/page.html"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["allow_generic"] is True
    urls = [c["url"] for c in body["candidates"]]
    assert "https://site.invalid/media/local.mp4" in urls


def test_import_endpoint_rejects_bad_base_url(client, settings_file):
    resp = client.post("/api/import", data={"text": PLAYER_HTML, "base_url": "site.invalid"})
    assert resp.status_code == 400


def test_import_endpoint_honours_allow_generic_setting(client, settings_file):
    client.put("/api/settings/general", json={"allow_generic": False})
    body = client.post("/api/import", data={"text": "https://some.odd.invalid/watch"}).json()
    assert body["allow_generic"] is False
    assert body["candidates"] == []
    assert body["rejected"] == [
        {"url": "https://some.odd.invalid/watch", "reason": "generic extraction disabled"}
    ]


# --------------------------------------------------------------------------
# Referer -> extra_args
# --------------------------------------------------------------------------


def test_referer_folded_into_extra_args(client, settings_file, no_worker):
    resp = client.post(
        "/api/jobs",
        json={
            "url": "https://some.odd.invalid/watch",
            "extra_args": "--no-part",
            "referer": "https://some.odd.invalid/page?a=1&b=2",
        },
    )
    assert resp.status_code == 201
    extra = resp.json()["job"]["extra_args"]
    assert shlex.split(extra) == [
        "--no-part",
        "--referer",
        "https://some.odd.invalid/page?a=1&b=2",
    ]


def test_referer_is_quoted_for_shlex(client, settings_file, no_worker):
    tricky = "https://h.invalid/a;b&c|d(e)"
    resp = client.post(
        "/api/jobs", json={"url": "https://h.invalid/x", "referer": tricky}
    )
    assert resp.status_code == 201
    extra = resp.json()["job"]["extra_args"]
    assert shlex.split(extra) == ["--referer", tricky]
    # And it still parses as yt-dlp options.
    from app import ytdl

    assert ytdl.parse_extra_args(extra)["http_headers"]["Referer"] == tricky


def test_default_referer_applies_when_job_omits_one(client, settings_file, no_worker):
    client.put(
        "/api/settings/general",
        json={"allow_generic": True, "default_referer": "https://default.invalid/"},
    )
    extra = client.post("/api/jobs", json={"url": "https://h.invalid/x"}).json()["job"][
        "extra_args"
    ]
    assert shlex.split(extra) == ["--referer", "https://default.invalid/"]


def test_explicit_referer_beats_the_default(client, settings_file, no_worker):
    client.put(
        "/api/settings/general",
        json={"allow_generic": True, "default_referer": "https://default.invalid/"},
    )
    extra = client.post(
        "/api/jobs",
        json={"url": "https://h.invalid/x", "referer": "https://explicit.invalid/"},
    ).json()["job"]["extra_args"]
    assert shlex.split(extra) == ["--referer", "https://explicit.invalid/"]


def test_user_referer_in_extra_args_is_not_duplicated(client, settings_file, no_worker):
    client.put(
        "/api/settings/general",
        json={"allow_generic": True, "default_referer": "https://default.invalid/"},
    )
    extra = client.post(
        "/api/jobs",
        json={
            "url": "https://h.invalid/x",
            "extra_args": "--referer https://mine.invalid/",
        },
    ).json()["job"]["extra_args"]
    assert extra.count("--referer") == 1
    assert shlex.split(extra) == ["--referer", "https://mine.invalid/"]


def test_referer_must_be_http(client, settings_file, no_worker):
    resp = client.post(
        "/api/jobs", json={"url": "https://h.invalid/x", "referer": "ftp://nope/"}
    )
    assert resp.status_code == 400


def test_bulk_jobs_fold_referer(client, settings_file, no_worker):
    resp = client.post(
        "/api/jobs/bulk",
        json={
            "urls": ["https://h.invalid/a", "https://h.invalid/b"],
            "referer": "https://ref.invalid/p",
        },
    )
    assert resp.status_code == 201
    for job in resp.json()["jobs"]:
        assert shlex.split(job["extra_args"]) == ["--referer", "https://ref.invalid/p"]


# --------------------------------------------------------------------------
# Direct media jobs skip the flat enumeration pass
# --------------------------------------------------------------------------


def _run_submit(job):
    async def main():
        await worker.submit(job)
        await asyncio.sleep(0)  # let any dispatch task start

    asyncio.run(main())


@pytest.fixture
def submit_spy(monkeypatch):
    calls = {"download": [], "dispatch": []}
    monkeypatch.setattr(
        worker, "_submit_download", lambda job: calls["download"].append(job["url"])
    )

    async def _fake_dispatch(job):
        calls["dispatch"].append(job["url"])

    monkeypatch.setattr(worker, "_dispatch_single", _fake_dispatch)
    return calls


def test_direct_media_job_skips_enumeration(submit_spy):
    url = "https://cdn.invalid/videos/clip.mp4?token=xyz"
    _run_submit({"id": "j1", "url": url, "type": "single"})
    assert submit_spy["download"] == [url]
    assert submit_spy["dispatch"] == []


def test_page_url_still_enumerates(submit_spy):
    url = "https://www.youtube.com/playlist?list=PL123"
    _run_submit({"id": "j2", "url": url, "type": "single"})
    assert submit_spy["dispatch"] == [url]
    assert submit_spy["download"] == []
