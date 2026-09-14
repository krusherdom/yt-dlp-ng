"""Tests for the bundled imaglr yt-dlp plugin extractors.

Everything runs against saved fixtures captured from the live site; the network
helpers (``_download_webpage`` / ``_download_json``) are always monkeypatched so
no test touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app  # noqa: F401  -- puts app/ on sys.path so yt_dlp_plugins is importable

from yt_dlp import YoutubeDL
from yt_dlp.utils import ExtractorError
from yt_dlp_plugins.extractor.imaglr import (
    ImaglrPageIE,
    ImaglrPostIE,
    ImaglrProfileIE,
    ImaglrTagIE,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def ydl():
    with YoutubeDL({"quiet": True, "no_warnings": True, "simulate": True}) as inst:
        yield inst


def _post_props(name):
    """``props.post`` out of a saved Inertia page fixture."""
    return _fixture_json(name)["props"]["post"]


# ---------------------------------------------------------------------------
# _VALID_URL matching
# ---------------------------------------------------------------------------

POST_OK = [
    "https://imaglr.com/post/90040737",
    "http://imaglr.com/post/90040737",
    "https://www.imaglr.com/post/90040737",
    "https://imaglr.com/p/art/post/84973417",
    "https://imaglr.com/post/90040737?foo=bar",
]
POST_BAD = [
    "https://imaglr.com/post/abc",
    "https://imaglr.com/profile/handyman",
    "https://imaglr.com/p/art",
    "https://imaglr.com/tag/art",
    "https://example.com/post/90040737",
    "https://notimaglr.com/post/1",
]

PROFILE_OK = [
    "https://imaglr.com/profile/handyman",
    "https://imaglr.com/profile/handyman/",
    "https://www.imaglr.com/profile/handyman?tab=posts",
    "https://imaglr.com/profile/handyman#top",
]
PROFILE_BAD = [
    "https://imaglr.com/profile/handyman/rss",
    "https://imaglr.com/profile/handyman/likes",
    "https://imaglr.com/profile/handyman/followers",
    "https://imaglr.com/profile/",
    "https://imaglr.com/post/90040737",
    "https://imaglr.com/p/art",
]

PAGE_OK = [
    "https://imaglr.com/p/art",
    "https://imaglr.com/p/art/",
    "https://www.imaglr.com/p/art?sort=new",
]
PAGE_BAD = [
    "https://imaglr.com/p/art/post/84973417",
    "https://imaglr.com/p/art/posts",
    "https://imaglr.com/p/",
    "https://imaglr.com/profile/handyman",
]

TAG_OK = [
    "https://imaglr.com/tag/art",
    "https://www.imaglr.com/tag/art",
    "https://imaglr.com/tag/car%20fun",
]
TAG_BAD = [
    "https://imaglr.com/tags/art",
    "https://imaglr.com/p/art",
    "https://imaglr.com/post/1",
]


@pytest.mark.parametrize(
    "ie,good,bad",
    [
        (ImaglrPostIE, POST_OK, POST_BAD),
        (ImaglrProfileIE, PROFILE_OK, PROFILE_BAD),
        (ImaglrPageIE, PAGE_OK, PAGE_BAD),
        (ImaglrTagIE, TAG_OK, TAG_BAD),
    ],
    ids=["post", "profile", "page", "tag"],
)
def test_valid_url_matching(ie, good, bad):
    for url in good:
        assert ie.suitable(url), f"{ie.IE_NAME} should match {url}"
    for url in bad:
        assert not ie.suitable(url), f"{ie.IE_NAME} should not match {url}"


def test_ie_names_are_namespaced():
    assert ImaglrPostIE.IE_NAME == "imaglr:post"
    assert ImaglrProfileIE.IE_NAME == "imaglr:profile"
    assert ImaglrPageIE.IE_NAME == "imaglr:page"
    assert ImaglrTagIE.IE_NAME == "imaglr:tag"


def test_post_id_extraction():
    assert ImaglrPostIE._match_id("https://imaglr.com/post/90040737") == "90040737"
    assert ImaglrPostIE._match_id("https://imaglr.com/p/art/post/84973417") == "84973417"
    assert ImaglrProfileIE._match_id("https://imaglr.com/profile/handyman") == "handyman"
    assert ImaglrPageIE._match_id("https://imaglr.com/p/art") == "art"


# ---------------------------------------------------------------------------
# _post_entries
# ---------------------------------------------------------------------------


def test_post_entries_video_post(ydl):
    """PostDetail shape: props.post = {'data': {...}} with one video medium."""
    ie = ImaglrPostIE(ydl)
    entries = ie._post_entries(_post_props("post_90040737.json"))

    assert len(entries) == 1
    entry = entries[0]
    assert entry["id"] == "90040737"
    assert entry["ext"] == "mp4"
    assert entry["url"].startswith("https://cdn03.imaglr.com/storage/uploads/")
    assert entry["url"].endswith(".mp4")
    assert entry["uploader"] == "My My My"
    assert entry["title"] == "My My My - 90040737"
    assert entry["timestamp"] == 1787958860
    assert entry["thumbnail"].endswith(".jpg")
    assert entry["webpage_url"] == "https://imaglr.com/post/90040737"
    # Requirement: every video entry carries the imaglr Referer.
    assert entry["http_headers"] == {"Referer": "https://imaglr.com/"}


def test_post_entries_image_repost_yields_nothing(ydl):
    """Pages/ShowPost shape, image repost: unwrapped, then filtered out."""
    ie = ImaglrPostIE(ydl)
    post = _post_props("post_84973417.json")

    # Sanity-check the fixture still models the case under test.
    assert post.get("media") == []
    assert post["reposted_from"]["media"][0]["type"] == "image"

    assert ie._post_entries(post) == []


def test_post_entries_unwraps_video_repost(ydl):
    """Real repost from the community page fixture: video lives in the parent."""
    ie = ImaglrPageIE(ydl)
    posts = [item["post"] for item in _fixture_json("page_art_posts.json")["data"]]
    repost = next(p for p in posts if p["id"] == 78301914)

    assert repost["media"] == []
    assert repost["reposted_from"]["media"][0]["type"] == "video"

    entries = ie._post_entries(repost)
    assert len(entries) == 1
    assert entries[0]["id"] == "78301914"
    assert entries[0]["url"] == repost["reposted_from"]["media"][0]["media_url"]
    # Outer post has no title/text, so the original post's title is used.
    assert entries[0]["title"] == "Girls get it done [Cyberpunk 2077] [Artist: Checkpik]"
    assert entries[0]["webpage_url"] == "https://imaglr.com/post/78301914"


def test_post_entries_multi_video_numbering(ydl):
    ie = ImaglrPostIE(ydl)
    post = {
        "id": 123,
        "post_type": "video",
        "title": "Two clips",
        "user": {"name": "someone"},
        "media": [
            {"type": "video", "media_url": "https://cdn03.imaglr.com/a.mp4"},
            {"type": "video", "media_url": "https://cdn03.imaglr.com/b.webm"},
        ],
    }
    entries = ie._post_entries(post)
    assert [e["id"] for e in entries] == ["123-1", "123-2"]
    assert [e["ext"] for e in entries] == ["mp4", "webm"]
    assert entries[0]["title"].endswith("(1)")


def test_post_entries_skips_images_and_gifs(ydl):
    ie = ImaglrPostIE(ydl)
    post = {
        "id": 5,
        "user": {"name": "u"},
        "media": [
            {"type": "image", "media_url": "https://cdn03.imaglr.com/a.jpg"},
            # A "gif" stored as .mp4 must still be skipped: type wins over ext.
            {"type": "gif", "media_url": "https://cdn03.imaglr.com/b.mp4"},
            {"type": "video", "media_url": "https://cdn03.imaglr.com/c.mp4"},
        ],
    }
    entries = ie._post_entries(post)
    assert len(entries) == 1
    assert entries[0]["url"].endswith("c.mp4")


def test_post_entries_typeless_media_falls_back_to_extension(ydl):
    ie = ImaglrPostIE(ydl)
    post = {
        "id": 7,
        "user": {"name": "u"},
        "media": [
            {"media_url": "https://cdn03.imaglr.com/x.jpeg"},
            {"media_url": "https://cdn03.imaglr.com/y.mp4"},
        ],
    }
    entries = ie._post_entries(post)
    assert len(entries) == 1
    assert entries[0]["url"].endswith("y.mp4")


def test_post_url_result_trusts_post_type_when_media_missing(ydl):
    """Unverified serialisers (logged-in profile / tag props) may omit media."""
    ie = ImaglrPageIE(ydl)

    claimed_video = ie._post_url_result(
        {"id": 1, "post_type": "video", "user": {"name": "u"}})
    assert claimed_video is not None
    assert claimed_video["url"] == "https://imaglr.com/post/1"
    assert claimed_video["ie_key"] == ImaglrPostIE.ie_key()

    # An image post with no media is still skipped...
    assert ie._post_url_result({"id": 2, "post_type": "image"}) is None
    # ...and so is a video-typed post whose media array is present but image-only.
    assert ie._post_url_result({
        "id": 3, "post_type": "video",
        "media": [{"type": "image", "media_url": "https://cdn03.imaglr.com/a.jpg"}],
    }) is None
    # A repost whose original claims video is kept.
    assert ie._post_url_result({
        "id": 4, "post_type": "image",
        "reposted_from": {"id": 5, "post_type": "video"},
    }) is not None


# ---------------------------------------------------------------------------
# ImaglrPostIE._real_extract
# ---------------------------------------------------------------------------


def _patch_webpage(monkeypatch, ie, body):
    monkeypatch.setattr(
        type(ie), "_download_webpage",
        lambda self, *a, **kw: body if isinstance(body, str) else body(*a, **kw),
        raising=False,
    )


def _inertia_html(fixture_name):
    """Rebuild a minimal page with an HTML-escaped data-page attribute."""
    import html as html_mod

    raw = json.dumps(_fixture_json(fixture_name))
    return f'<html><body><div id="app" data-page="{html_mod.escape(raw, quote=True)}"></div></body></html>'


def test_real_extract_single_video_post(ydl, monkeypatch):
    ie = ImaglrPostIE(ydl)
    ie._downloader = ydl
    _patch_webpage(monkeypatch, ie, _inertia_html("post_90040737.json"))

    info = ie.extract("https://imaglr.com/post/90040737")
    assert info["id"] == "90040737"
    assert info["ext"] == "mp4"
    assert info["url"].endswith(".mp4")
    assert info["http_headers"] == {"Referer": "https://imaglr.com/"}
    assert info.get("_type", "video") == "video"


def test_real_extract_image_post_raises_expected(ydl, monkeypatch):
    ie = ImaglrPostIE(ydl)
    ie._downloader = ydl
    _patch_webpage(monkeypatch, ie, _inertia_html("post_84973417.json"))

    with pytest.raises(ExtractorError) as excinfo:
        ie.extract("https://imaglr.com/post/84973417")
    assert "No video in this post" in str(excinfo.value)
    assert excinfo.value.expected is True


def test_real_extract_falls_back_to_og_video(ydl, monkeypatch):
    ie = ImaglrPostIE(ydl)
    ie._downloader = ydl
    html = (
        '<html><head>'
        '<meta property="og:video" content="https://cdn03.imaglr.com/z.mp4">'
        '<meta property="og:title" content="Fallback post">'
        '</head><body></body></html>'
    )
    _patch_webpage(monkeypatch, ie, html)

    info = ie.extract("https://imaglr.com/post/1")
    assert info["url"] == "https://cdn03.imaglr.com/z.mp4"
    assert info["http_headers"] == {"Referer": "https://imaglr.com/"}


# ---------------------------------------------------------------------------
# RSS enumeration (anonymous profile path)
# ---------------------------------------------------------------------------


def test_rss_entries_only_videos(ydl, monkeypatch):
    ie = ImaglrProfileIE(ydl)
    ie._downloader = ydl
    pages = {1: _fixture_text("rss_user_handyman.xml")}
    empty = pages[1].split("<item>")[0] + "</channel>\n</rss>"
    calls = []

    def fake(self, url, video_id, *a, **kw):
        page = int((kw.get("query") or {}).get("page", 1))
        calls.append(page)
        return pages.get(page, empty)

    monkeypatch.setattr(type(ie), "_download_webpage", fake, raising=False)

    entries = list(ie._rss_entries("/rss/user/handyman", "handyman"))
    assert len(entries) == 2
    assert [e["id"] for e in entries] == ["92286528", "90445241"]
    assert all(e["_type"] == "url" for e in entries)
    assert all(e["ie_key"] == ImaglrPostIE.ie_key() for e in entries)
    assert entries[0]["url"] == "https://imaglr.com/post/92286528"
    # Page 2 is empty -> pagination stops after exactly two requests.
    assert calls == [1, 2]


def test_rss_entries_skip_non_video_items(ydl, monkeypatch):
    ie = ImaglrProfileIE(ydl)
    ie._downloader = ydl
    feed = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item><title>img</title><link>https://imaglr.com/post/1</link><category>image</category></item>
  <item><title>vid</title><link>https://imaglr.com/post/2</link><category>video</category></item>
  <item><title>enc</title><link>https://imaglr.com/post/3</link>
    <enclosure url="https://cdn03.imaglr.com/a.mp4" type="video/mp4" length="0"/></item>
</channel></rss>"""
    empty = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
    monkeypatch.setattr(
        type(ie), "_download_webpage",
        lambda self, url, vid, *a, **kw: feed
        if int((kw.get("query") or {}).get("page", 1)) == 1 else empty,
        raising=False,
    )
    entries = list(ie._rss_entries("/rss/user/x", "x"))
    assert [e["id"] for e in entries] == ["2", "3"]


def test_rss_entries_respect_max_pages(ydl, monkeypatch):
    """A feed that keeps serving new items must still stop at the cap."""
    ie = ImaglrProfileIE(ydl)
    ie._downloader = ydl
    monkeypatch.setenv("IMAGLR_MAX_PAGES", "3")
    seen = []

    def fake(self, url, video_id, *a, **kw):
        page = int((kw.get("query") or {}).get("page", 1))
        seen.append(page)
        return (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            f'<item><link>https://imaglr.com/post/{page}</link>'
            "<category>video</category></item></channel></rss>"
        )

    monkeypatch.setattr(type(ie), "_download_webpage", fake, raising=False)
    entries = list(ie._rss_entries("/rss/user/x", "x"))
    assert len(entries) == 3
    assert seen == [1, 2, 3]


def test_profile_anonymous_uses_rss(ydl, monkeypatch):
    ie = ImaglrProfileIE(ydl)
    ie._downloader = ydl
    requested = []

    def fake(self, url, video_id, *a, **kw):
        requested.append(url)
        page = int((kw.get("query") or {}).get("page", 1))
        if page == 1:
            return _fixture_text("rss_user_handyman.xml")
        return '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'

    monkeypatch.setattr(type(ie), "_download_webpage", fake, raising=False)
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: False, raising=False)
    # A JSON call would mean the logged-in path was taken by mistake.
    monkeypatch.setattr(
        type(ie), "_download_json",
        lambda *a, **kw: pytest.fail("anonymous profile must not hit the JSON API"),
        raising=False,
    )

    result = ie._real_extract("https://imaglr.com/profile/handyman")
    entries = list(result["entries"])
    assert result["_type"] == "playlist"
    assert result["id"] == "handyman"
    assert len(entries) == 2
    assert all("rss/user/handyman" in u for u in requested)


def test_profile_logged_in_uses_json(ydl, monkeypatch):
    ie = ImaglrProfileIE(ydl)
    ie._downloader = ydl
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: True, raising=False)
    posts = [item["post"] for item in _fixture_json("page_art_posts.json")["data"]]
    video_post = next(p for p in posts if p["id"] == 78301914)
    image_post = next(p for p in posts if p["id"] != 78301914)
    queries = []

    def fake_json(self, url, video_id, *a, **kw):
        queries.append((url, dict(kw.get("query") or {})))
        assert kw["headers"]["X-Requested-With"] == "XMLHttpRequest"
        assert kw["headers"]["Accept"] == "application/json"
        return {"data": [{"post": video_post}, {"post": image_post}], "has_more": False}

    monkeypatch.setattr(type(ie), "_download_json", fake_json, raising=False)
    monkeypatch.setattr(
        type(ie), "_download_webpage",
        lambda *a, **kw: pytest.fail("logged-in profile must not fall back to RSS"),
        raising=False,
    )

    result = ie._real_extract("https://imaglr.com/profile/handyman")
    entries = list(result["entries"])
    assert len(entries) == 1  # image post filtered out
    assert entries[0]["_type"] == "url"
    assert entries[0]["url"] == "https://imaglr.com/post/78301914"
    assert queries[0][0] == "https://imaglr.com/profile/handyman/posts"
    assert queries[0][1] == {
        "skip": 0, "take": 20, "paginated": "false", "type": "video",
    }


def test_profile_json_401_falls_back_to_rss(ydl, monkeypatch):
    from yt_dlp.networking.exceptions import HTTPError

    ie = ImaglrProfileIE(ydl)
    ie._downloader = ydl
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: True, raising=False)

    class _Resp:
        status = 401
        reason = "Unauthorized"
        url = "https://imaglr.com/profile/handyman/posts"
        headers = {}

        def close(self):
            pass

    def fake_json(self, *a, **kw):
        raise ExtractorError("boom", cause=HTTPError(_Resp()))

    def fake_page(self, url, video_id, *a, **kw):
        page = int((kw.get("query") or {}).get("page", 1))
        if page == 1:
            return _fixture_text("rss_user_handyman.xml")
        return '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'

    monkeypatch.setattr(type(ie), "_download_json", fake_json, raising=False)
    monkeypatch.setattr(type(ie), "_download_webpage", fake_page, raising=False)

    result = ie._real_extract("https://imaglr.com/profile/handyman")
    assert len(list(result["entries"])) == 2


# ---------------------------------------------------------------------------
# Community page enumeration
# ---------------------------------------------------------------------------


def test_page_entries_videos_only(ydl, monkeypatch):
    ie = ImaglrPageIE(ydl)
    ie._downloader = ydl
    payload = _fixture_json("page_art_posts.json")
    assert len(payload["data"]) == 20
    # The real response paginates via has_more, not next_page_url.
    assert payload.get("next_page_url") is None
    assert "has_more" in payload

    urls = []

    def fake_json(self, url, video_id, *a, **kw):
        urls.append(url)
        if len(urls) == 1:
            return payload
        return {"data": [], "has_more": False}

    monkeypatch.setattr(type(ie), "_download_json", fake_json, raising=False)

    entries = list(ie._entries("art"))
    # Exactly one of the 20 posts on this page is a video (a repost).
    assert len(entries) == 1
    assert entries[0]["url"] == "https://imaglr.com/post/78301914"
    assert entries[0]["ie_key"] == ImaglrPostIE.ie_key()
    assert urls[0] == "https://imaglr.com/p/art/posts?page=1"


def test_page_entries_follow_has_more(ydl, monkeypatch):
    ie = ImaglrPageIE(ydl)
    ie._downloader = ydl
    video = {
        "id": 1, "post_type": "video", "user": {"name": "u"},
        "media": [{"type": "video", "media_url": "https://cdn03.imaglr.com/1.mp4"}],
    }
    video2 = dict(video, id=2,
                  media=[{"type": "video", "media_url": "https://cdn03.imaglr.com/2.mp4"}])
    urls = []

    def fake_json(self, url, video_id, *a, **kw):
        urls.append(url)
        if "page=2" in url:
            return {"data": [{"post": video2}], "has_more": False}
        return {"data": [{"post": video}], "has_more": True}

    monkeypatch.setattr(type(ie), "_download_json", fake_json, raising=False)
    entries = list(ie._entries("art"))
    assert [e["id"] for e in entries] == ["1", "2"]
    assert urls == [
        "https://imaglr.com/p/art/posts?page=1",
        "https://imaglr.com/p/art/posts?page=2",
    ]


def test_page_entries_follow_next_page_url(ydl, monkeypatch):
    ie = ImaglrPageIE(ydl)
    ie._downloader = ydl
    video = {
        "id": 9, "post_type": "video", "user": {"name": "u"},
        "media": [{"type": "video", "media_url": "https://cdn03.imaglr.com/9.mp4"}],
    }
    urls = []

    def fake_json(self, url, video_id, *a, **kw):
        urls.append(url)
        if len(urls) == 1:
            return {"data": [{"post": video}],
                    "next_page_url": "https://imaglr.com/p/art/posts?page=7"}
        return {"data": []}

    monkeypatch.setattr(type(ie), "_download_json", fake_json, raising=False)
    assert len(list(ie._entries("art"))) == 1
    assert urls[1] == "https://imaglr.com/p/art/posts?page=7"


def test_page_falls_back_to_rss_when_json_fails(ydl, monkeypatch):
    ie = ImaglrPageIE(ydl)
    ie._downloader = ydl

    def fake_json(self, *a, **kw):
        raise ExtractorError("nope", expected=True)

    def fake_page(self, url, video_id, *a, **kw):
        assert "/rss/page/art" in url
        page = int((kw.get("query") or {}).get("page", 1))
        if page == 1:
            return _fixture_text("rss_user_handyman.xml")
        return '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'

    monkeypatch.setattr(type(ie), "_download_json", fake_json, raising=False)
    monkeypatch.setattr(type(ie), "_download_webpage", fake_page, raising=False)
    assert len(list(ie._entries("art"))) == 2


# ---------------------------------------------------------------------------
# Tag extractor (cannot be exercised against the real logged-in site)
# ---------------------------------------------------------------------------


def test_tag_without_cookies_raises_expected(ydl, monkeypatch):
    ie = ImaglrTagIE(ydl)
    ie._downloader = ydl
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: False, raising=False)
    monkeypatch.setattr(
        type(ie), "_download_webpage",
        lambda *a, **kw: pytest.fail("no request should be made without cookies"),
        raising=False,
    )
    with pytest.raises(ExtractorError) as excinfo:
        ie._real_extract("https://imaglr.com/tag/art")
    assert excinfo.value.expected is True
    assert "cookies" in str(excinfo.value)


def test_tag_login_component_raises_expected(ydl, monkeypatch):
    """Stale cookies -> imaglr serves the Auth/Login Inertia component."""
    ie = ImaglrTagIE(ydl)
    ie._downloader = ydl
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: True, raising=False)
    monkeypatch.setattr(
        type(ie), "_inertia_page",
        lambda self, *a, **kw: ({"component": "Auth/Login", "props": {}}, ""),
        raising=False,
    )
    with pytest.raises(ExtractorError) as excinfo:
        ie._real_extract("https://imaglr.com/tag/art")
    assert excinfo.value.expected is True


def test_tag_finds_posts_prop_generically(ydl, monkeypatch):
    ie = ImaglrTagIE(ydl)
    ie._downloader = ydl
    video = {
        "id": 42, "post_type": "video", "user": {"name": "u"},
        "media": [{"type": "video", "media_url": "https://cdn03.imaglr.com/42.mp4"}],
    }
    image = {"id": 43, "post_type": "image", "user": {"name": "u"},
             "media": [{"type": "image", "media_url": "https://cdn03.imaglr.com/43.jpg"}]}
    props = {
        "auth": {"user": None},
        "meta": {"title": "x"},
        "tagPosts": {"data": [{"post": video}, {"post": image}], "next_page_url": None},
    }
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: True, raising=False)
    monkeypatch.setattr(
        type(ie), "_inertia_page",
        lambda self, *a, **kw: ({"component": "Tag/Show", "props": props}, ""),
        raising=False,
    )

    result = ie._real_extract("https://imaglr.com/tag/art")
    entries = list(result["entries"])
    assert len(entries) == 1
    assert entries[0]["url"] == "https://imaglr.com/post/42"


def test_tag_bare_list_prop_and_pagination(ydl, monkeypatch):
    ie = ImaglrTagIE(ydl)
    ie._downloader = ydl
    make = lambda i: {  # noqa: E731
        "id": i, "post_type": "video", "user": {"name": "u"},
        "media": [{"type": "video", "media_url": f"https://cdn03.imaglr.com/{i}.mp4"}],
    }
    props = {"posts": {"data": [make(1)], "next_page_url": "/tag/art?page=2"}}
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: True, raising=False)
    monkeypatch.setattr(
        type(ie), "_inertia_page",
        lambda self, *a, **kw: (
            {"component": "Tag/Show", "props": props, "version": "abc123"}, ""),
        raising=False,
    )
    seen = []

    def fake_page(self, url, video_id, *a, **kw):
        seen.append((url, kw.get("headers", {})))
        return json.dumps({"data": [make(2)], "next_page_url": None})

    monkeypatch.setattr(type(ie), "_download_webpage", fake_page, raising=False)

    entries = list(ie._real_extract("https://imaglr.com/tag/art")["entries"])
    assert [e["id"] for e in entries] == ["1", "2"]
    assert seen[0][0] == "https://imaglr.com/tag/art?page=2"
    # Inertia protocol headers so the page route answers with JSON.
    assert seen[0][1]["X-Inertia"] == "true"
    assert seen[0][1]["X-Inertia-Version"] == "abc123"


def test_tag_pagination_accepts_html_inertia_body(ydl, monkeypatch):
    """A page route that answers with HTML is still parsed via data-page."""
    ie = ImaglrTagIE(ydl)
    ie._downloader = ydl
    make = lambda i: {  # noqa: E731
        "id": i, "post_type": "video", "user": {"name": "u"},
        "media": [{"type": "video", "media_url": f"https://cdn03.imaglr.com/{i}.mp4"}],
    }
    props = {"posts": {"data": [make(1)], "next_page_url": "/tag/art?page=2"}}
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: True, raising=False)
    monkeypatch.setattr(
        type(ie), "_inertia_page",
        lambda self, *a, **kw: ({"component": "Tag/Show", "props": props}, ""),
        raising=False,
    )

    import html as html_mod

    page2 = json.dumps(
        {"component": "Tag/Show",
         "props": {"posts": {"data": [make(2)], "next_page_url": None}}})
    monkeypatch.setattr(
        type(ie), "_download_webpage",
        lambda self, *a, **kw: f'<div data-page="{html_mod.escape(page2, quote=True)}"></div>',
        raising=False,
    )

    entries = list(ie._real_extract("https://imaglr.com/tag/art")["entries"])
    assert [e["id"] for e in entries] == ["1", "2"]


def test_tag_pagination_failure_keeps_first_page(ydl, monkeypatch):
    ie = ImaglrTagIE(ydl)
    ie._downloader = ydl
    video = {
        "id": 1, "post_type": "video", "user": {"name": "u"},
        "media": [{"type": "video", "media_url": "https://cdn03.imaglr.com/1.mp4"}],
    }
    props = {"posts": {"data": [video], "next_page_url": "/tag/art?page=2"}}
    warnings = []
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: True, raising=False)
    monkeypatch.setattr(
        type(ie), "_inertia_page",
        lambda self, *a, **kw: ({"component": "Tag/Show", "props": props}, ""),
        raising=False,
    )
    monkeypatch.setattr(type(ie), "report_warning",
                        lambda self, msg, *a, **kw: warnings.append(msg), raising=False)

    def boom(self, *a, **kw):
        raise ExtractorError("500 server error")

    monkeypatch.setattr(type(ie), "_download_webpage", boom, raising=False)

    entries = list(ie._real_extract("https://imaglr.com/tag/art")["entries"])
    assert [e["id"] for e in entries] == ["1"]
    assert warnings and "stopping after 1 page" in warnings[0]


def test_tag_unrecognised_layout_warns_and_raises(ydl, monkeypatch):
    ie = ImaglrTagIE(ydl)
    ie._downloader = ydl
    warnings = []
    monkeypatch.setattr(type(ie), "_logged_in", lambda self: True, raising=False)
    monkeypatch.setattr(
        type(ie), "_inertia_page",
        lambda self, *a, **kw: (
            {"component": "Tag/Show", "props": {"auth": {}, "somethingElse": 3}}, ""),
        raising=False,
    )
    monkeypatch.setattr(type(ie), "report_warning",
                        lambda self, msg, *a, **kw: warnings.append(msg), raising=False)

    with pytest.raises(ExtractorError) as excinfo:
        ie._real_extract("https://imaglr.com/tag/art")
    assert excinfo.value.expected is True
    assert warnings and "Unrecognised" in warnings[0]


def test_find_posts_prop_ignores_page_furniture():
    props = {
        "auth": {"user": {"id": 1, "post_type": "nonsense"}},
        "currentUser": {"id": 2},
        "meta": {"title": "t"},
        "feed": [{"id": 5, "post_type": "video"}],
    }
    name, items, next_url = ImaglrTagIE._find_posts_prop(props)
    assert name == "feed"
    assert items[0]["id"] == 5
    assert next_url is None


# ---------------------------------------------------------------------------
# Importer integration
# ---------------------------------------------------------------------------


def test_importer_matches_imaglr_urls():
    from app import importer

    assert importer.match_extractor("https://imaglr.com/post/90040737") == "imaglr:post"
    assert importer.match_extractor("https://imaglr.com/profile/handyman") == "imaglr:profile"
    assert importer.match_extractor("https://imaglr.com/p/art") == "imaglr:page"
    assert importer.match_extractor("https://imaglr.com/tag/art") == "imaglr:tag"


def test_importer_validates_imaglr_payload():
    from app import importer

    result = importer.import_payload(
        "https://imaglr.com/post/90040737\n"
        "https://imaglr.com/p/art\n"
        "https://example.invalid/nope\n"
    )
    extractors = {c["url"]: c["extractor"] for c in result["candidates"]}
    assert extractors["https://imaglr.com/post/90040737"] == "imaglr:post"
    assert extractors["https://imaglr.com/p/art"] == "imaglr:page"
    assert result["rejected"] == ["https://example.invalid/nope"]
