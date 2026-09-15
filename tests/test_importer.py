"""Importer: URL extraction from text and HTML, dedupe, extractor validation."""

from __future__ import annotations

import pytest

from app import importer

VALID_TEXT = """
Check these out:
https://www.youtube.com/watch?v=dQw4w9WgXcQ
https://vimeo.com/123456789
not a url at all
https://example.invalid/some/random/page.html
ftp://files.example.com/thing.mp4
https://soundcloud.com/someartist/some-track
https://www.youtube.com/watch?v=dQw4w9WgXcQ
"""

BOOKMARKS_HTML = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3 ADD_DATE="1700000000">Videos</H3>
    <DL><p>
        <DT><A HREF="https://www.youtube.com/watch?v=abcdefghijk" ADD_DATE="1700000001">A video</A>
        <DT><A HREF="https://vimeo.com/987654321" ADD_DATE="1700000002">Another</A>
        <DT><A HREF="https://news.ycombinator.com/" ADD_DATE="1700000003">HN</A>
        <DT><A HREF="https://www.youtube.com/watch?v=abcdefghijk">Dupe</A>
    </DL><p>
</DL><p>
"""

PAGE_HTML = """<html><head><link rel="stylesheet" href="https://cdn.example.com/a.css"></head>
<body>
  <a href="https://www.youtube.com/playlist?list=PL1234567890abcdefg">Playlist</a>
  <a href="/relative/path">Relative</a>
  <a href="https://twitter.com/someone/status/123456789">Tweet</a>
  Bare mention: https://www.dailymotion.com/video/x7tgad0 and text after.
</body></html>
"""


def test_extract_urls_from_text_dedupes_and_preserves_order():
    urls = importer.extract_urls(VALID_TEXT)
    assert urls[0] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert urls.count("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == 1
    assert "https://vimeo.com/123456789" in urls
    # Non-http schemes are ignored entirely.
    assert not any(u.startswith("ftp://") for u in urls)


def test_extract_urls_strips_trailing_punctuation():
    urls = importer.extract_urls("see https://vimeo.com/123456789, and done.")
    assert urls == ["https://vimeo.com/123456789"]


def test_extract_urls_from_netscape_bookmarks():
    urls = importer.extract_urls(BOOKMARKS_HTML)
    assert "https://www.youtube.com/watch?v=abcdefghijk" in urls
    assert "https://vimeo.com/987654321" in urls
    assert urls.count("https://www.youtube.com/watch?v=abcdefghijk") == 1


def test_extract_urls_from_html_page_ignores_relative_hrefs():
    urls = importer.extract_urls(PAGE_HTML)
    assert "https://www.youtube.com/playlist?list=PL1234567890abcdefg" in urls
    assert "https://www.dailymotion.com/video/x7tgad0" in urls
    assert not any(u.startswith("/") for u in urls)


def test_extract_urls_empty_input():
    assert importer.extract_urls("") == []
    assert importer.extract_urls("nothing to see here") == []


def test_match_extractor_known_sites():
    assert importer.match_extractor("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is not None
    assert importer.match_extractor("https://vimeo.com/123456789") is not None


def test_match_extractor_rejects_generic_pages():
    # GenericIE is excluded, so an arbitrary page must not match.
    assert importer.match_extractor("https://example.invalid/some/random/page.html") is None
    assert importer.match_extractor("https://news.ycombinator.com/") is None


def test_validate_urls_splits_valid_and_rejected():
    # allow_generic=False keeps the old "known sites only" behaviour.
    candidates, rejected = importer.validate_urls(
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://example.invalid/some/random/page.html",
            "https://vimeo.com/123456789",
        ],
        allow_generic=False,
    )
    assert [c["url"] for c in candidates] == [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://vimeo.com/123456789",
    ]
    assert all(c["extractor"] for c in candidates)
    assert all(c["kind"] == "site" for c in candidates)
    assert rejected == [
        {
            "url": "https://example.invalid/some/random/page.html",
            "reason": importer.REASON_NO_GENERIC,
        }
    ]


def test_import_payload_mixed_text():
    result = importer.import_payload(VALID_TEXT, allow_generic=False)
    assert result["total_found"] >= 4
    assert result["allow_generic"] is False
    urls = [c["url"] for c in result["candidates"]]
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in urls
    assert "https://example.invalid/some/random/page.html" in [
        r["url"] for r in result["rejected"]
    ]


def test_import_payload_html_file():
    result = importer.import_payload(BOOKMARKS_HTML, allow_generic=False)
    urls = [c["url"] for c in result["candidates"]]
    assert "https://www.youtube.com/watch?v=abcdefghijk" in urls
    assert "https://news.ycombinator.com/" in [r["url"] for r in result["rejected"]]


def test_looks_like_html_detection():
    assert importer.looks_like_html(BOOKMARKS_HTML)
    assert importer.looks_like_html(PAGE_HTML)
    assert not importer.looks_like_html(VALID_TEXT)


def test_extractor_cache_is_reused():
    first = importer._extractors()
    second = importer._extractors()
    assert first is second
    assert len(first) > 100
    assert not any(ie.__name__ == "GenericIE" for ie in first)
