"""URL extraction from pasted text / uploaded HTML plus extractor validation."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

# Matches http(s) URLs in free text. Trailing punctuation is trimmed below.
URL_RE = re.compile(r"https?://[^\s<>\"'\\)\]}]+", re.IGNORECASE)

_TRAILING = ".,;:!?'\"`)]}>"

_extractors_cache: Optional[List] = None


class _HrefParser(HTMLParser):
    """Collects href/src attributes.

    Netscape bookmark exports (``<DT><A HREF="..." ADD_DATE="...">title</A>``)
    are just malformed HTML with ``A`` tags, so the same pass handles them.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() not in ("a", "area", "link", "iframe", "embed", "source"):
            return
        for name, value in attrs:
            if name and name.lower() in ("href", "src") and value:
                self.urls.append(value.strip())


def _clean(url: str) -> str:
    url = url.strip().strip("\u200b")
    while url and url[-1] in _TRAILING:
        # Keep a closing paren if the URL clearly contains a matching open one.
        if url[-1] == ")" and url.count("(") > url.count(")"):
            break
        url = url[:-1]
    return url


def looks_like_html(payload: str) -> bool:
    head = payload.lstrip()[:4096].lower()
    return (
        "<!doctype netscape-bookmark-file" in head
        or "<html" in head
        or "<a " in head
        or "<dt>" in head
        or "href=" in head
    )


def extract_urls(payload: str) -> List[str]:
    """Extract candidate URLs from text or HTML, deduped, order preserved."""
    if not payload:
        return []

    found: List[str] = []
    if looks_like_html(payload):
        parser = _HrefParser()
        try:
            parser.feed(payload)
            parser.close()
        except Exception:
            pass
        found.extend(parser.urls)
        # Bookmark exports and pasted pages can also carry bare URLs in text.
        found.extend(URL_RE.findall(payload))
    else:
        found.extend(URL_RE.findall(payload))

    seen = set()
    result: List[str] = []
    for raw in found:
        url = _clean(raw)
        if not url or not url.lower().startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _extractors() -> List:
    """All yt-dlp extractor classes except GenericIE, cached at first use."""
    global _extractors_cache
    if _extractors_cache is None:
        from yt_dlp.extractor import gen_extractor_classes

        classes = []
        for ie in gen_extractor_classes():
            name = getattr(ie, "IE_NAME", ie.__name__)
            if str(name).lower() == "generic" or ie.__name__ == "GenericIE":
                continue
            if not getattr(ie, "_WORKING", True):
                continue
            classes.append(ie)
        _extractors_cache = classes
    return _extractors_cache


def match_extractor(url: str) -> Optional[str]:
    """Return the name of the first non-generic extractor that claims ``url``."""
    for ie in _extractors():
        try:
            if ie.suitable(url):
                return str(getattr(ie, "IE_NAME", ie.__name__))
        except Exception:
            continue
    return None


def validate_urls(urls: List[str]) -> Tuple[List[Dict[str, str]], List[str]]:
    """Split ``urls`` into supported candidates and rejected URLs."""
    candidates: List[Dict[str, str]] = []
    rejected: List[str] = []
    for url in urls:
        name = match_extractor(url)
        if name:
            candidates.append({"url": url, "extractor": name})
        else:
            rejected.append(url)
    return candidates, rejected


def import_payload(payload: str) -> Dict[str, object]:
    """Full pipeline: extract -> dedupe -> validate."""
    urls = extract_urls(payload)
    candidates, rejected = validate_urls(urls)
    return {
        "candidates": candidates,
        "rejected": rejected,
        "total_found": len(urls),
    }


# Warm the extractor list at import so the first request is not slow.
try:  # pragma: no cover - best effort only
    _extractors()
except Exception:  # pragma: no cover
    pass
