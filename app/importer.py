"""URL extraction from pasted text / uploaded HTML plus extractor validation.

Three kinds of candidate come out of here:

``site``
    A dedicated yt-dlp extractor claims the URL.
``direct``
    The URL path ends in a media extension, so yt-dlp can just fetch it.
``generic``
    Anything else: handed to yt-dlp's GenericIE, which scrapes the page for a
    playable stream. Only offered when the ``allow_generic`` general setting is
    on -- that is what makes the app a catch-all for odd sites.

Sites yt-dlp deliberately refuses (DRM-only, piracy, liability) are rejected
with a reason rather than offered as candidates.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

# Matches http(s) URLs in free text. Trailing punctuation is trimmed below.
URL_RE = re.compile(r"https?://[^\s<>\"'\\)\]}]+", re.IGNORECASE)

_TRAILING = ".,;:!?'\"`)]}>"

#: A URL path ending in one of these is a media file we can download straight
#: away -- no extractor, and no playlist enumeration pass.
MEDIA_EXTENSIONS = frozenset(
    {
        "mp4", "webm", "mkv", "mov", "m4v", "avi", "flv", "ts",
        "mp3", "m4a", "aac", "ogg", "opus", "flac", "wav",
        "m3u8", "mpd",
    }
)

#: Rejection reasons returned by :func:`validate_urls`.
REASON_DRM = "unsupported by yt-dlp (DRM)"
REASON_PIRACY = "unsupported by yt-dlp (piracy)"
REASON_LIABILITY = "unsupported by yt-dlp (liability)"
#: Fallback for a future member of the unsupported family we do not know yet.
REASON_UNSUPPORTED = "unsupported by yt-dlp"
REASON_NO_GENERIC = "generic extraction disabled"
REASON_NOT_URL = "not a URL"

#: IE_NAME -> reason, for the yt_dlp.extractor.unsupported family.
_UNSUPPORTED_REASONS = {
    "drm": REASON_DRM,
    "piracy": REASON_PIRACY,
    "liability": REASON_LIABILITY,
}

#: Attributes that can carry a media/link URL.
_URL_ATTRS = ("href", "src", "data-src", "data-url")

#: Tags whose href/src we harvest.
_URL_TAGS = (
    "a", "area", "link", "iframe", "embed", "source", "video", "audio", "track"
)

#: <link rel="..."> values that are page furniture, never media.
_SKIP_LINK_RELS = frozenset(
    {
        "stylesheet", "icon", "shortcut icon", "apple-touch-icon", "manifest",
        "preconnect", "dns-prefetch", "prefetch", "preload", "modulepreload",
        "canonical", "alternate", "author", "license", "search", "help",
    }
)

#: <meta property|name="..."> keys that point at a playable stream.
_MEDIA_META_KEYS = frozenset(
    {
        "og:video",
        "og:video:url",
        "og:video:secure_url",
        "twitter:player:stream",
    }
)

_extractors_cache: Optional[List] = None


class _HrefParser(HTMLParser):
    """Collects link/media URLs from HTML.

    Netscape bookmark exports (``<DT><A HREF="..." ADD_DATE="...">title</A>``)
    are just malformed HTML with ``A`` tags, so the same pass handles them.
    Player markup (``<video src>``, ``<source src>``, ``og:video`` meta tags)
    is what makes "paste the page, get the file" work on sites yt-dlp has no
    extractor for.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: List[str] = []
        #: Text nodes (script bodies included) scanned separately for bare
        #: URLs, so a skipped attribute -- a stylesheet href, say -- does not
        #: sneak back in through a regex pass over the raw markup.
        self.text: List[str] = []

    def handle_data(self, data: str) -> None:
        if data and "http" in data:
            self.text.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        name = tag.lower()
        values = {k.lower(): (v or "") for k, v in attrs if k}

        if name == "meta":
            key = (values.get("property") or values.get("name") or "").strip().lower()
            if key in _MEDIA_META_KEYS:
                content = (values.get("content") or "").strip()
                if content:
                    self.urls.append(content)
            return

        if name not in _URL_TAGS:
            return
        if name == "link":
            rels = (values.get("rel") or "").strip().lower()
            if rels and rels in _SKIP_LINK_RELS:
                return
            if any(part in _SKIP_LINK_RELS for part in rels.split()):
                return

        for attr in _URL_ATTRS:
            value = (values.get(attr) or "").strip()
            if value:
                self.urls.append(value)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)


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
        or "src=" in head
        or "<video" in head
        or "<audio" in head
        or "<source" in head
        or "<iframe" in head
        or "<embed" in head
        or "<meta" in head
    )


def _resolve(url: str, base_url: Optional[str]) -> Optional[str]:
    """Absolutise ``url``; ``None`` when it cannot be used as a download URL."""
    if not url:
        return None
    lowered = url.lower()
    if lowered.startswith(("http://", "https://")):
        return url
    # Fragments, scripts, inline data and non-http schemes are never downloads.
    if url.startswith("#") or lowered.startswith(
        ("javascript:", "mailto:", "data:", "about:", "tel:", "blob:")
    ):
        return None
    try:
        if urlsplit(url).scheme:
            return None  # some other scheme (ftp:, magnet:, ...)
    except ValueError:
        return None
    if not base_url:
        return None
    try:
        joined = urljoin(base_url, url)
    except ValueError:
        return None
    return joined if joined.lower().startswith(("http://", "https://")) else None


def extract_urls(payload: str, base_url: Optional[str] = None) -> List[str]:
    """Extract candidate URLs from text or HTML, deduped, order preserved.

    ``base_url`` is the page the markup came from; relative ``src``/``href``
    values resolve against it. Without one they are dropped.
    """
    if not payload:
        return []

    base = (base_url or "").strip() or None

    found: List[str] = []
    if looks_like_html(payload):
        parser = _HrefParser()
        parsed = True
        try:
            parser.feed(payload)
            parser.close()
        except Exception:
            parsed = False
        found.extend(parser.urls)
        # Bookmark exports and pasted pages can also carry bare URLs in text,
        # and player configs hide them in inline <script> blobs.
        found.extend(URL_RE.findall("\n".join(parser.text) if parsed else payload))
    else:
        found.extend(URL_RE.findall(payload))

    seen = set()
    result: List[str] = []
    for raw in found:
        url = _resolve(_clean(raw), base)
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _extractors() -> List:
    """All yt-dlp extractor classes except GenericIE, cached at first use."""
    global _extractors_cache
    if _extractors_cache is None:
        import yt_dlp.plugins
        from yt_dlp.extractor import gen_extractor_classes

        # gen_extractor_classes() does not see plugin extractors (e.g. the
        # bundled imaglr one) until the plugin loader has run; YoutubeDL() does
        # this itself, but we never construct one here.
        try:
            yt_dlp.plugins.load_all_plugins()
        except Exception:  # pragma: no cover - never fail import over a plugin
            pass

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


def _unsupported_base():
    """``UnsupportedInfoExtractor`` if this yt-dlp has it, else ``None``."""
    try:
        from yt_dlp.extractor.unsupported import UnsupportedInfoExtractor

        return UnsupportedInfoExtractor
    except Exception:  # pragma: no cover - very old yt-dlp
        return None


def unsupported_reason(ie) -> Optional[str]:
    """Why yt-dlp refuses ``ie``, or ``None`` if it is a real extractor.

    yt-dlp models "we will never support this" as extractors that only raise:
    ``KnownDRMIE``, ``KnownPiracyIE``, ``KnownLiabilityIE``. Offering them as
    candidates would just queue jobs that fail, so the importer rejects them
    up front with the reason.
    """
    if ie is None:
        return None
    base = _unsupported_base()
    name = str(getattr(ie, "IE_NAME", ie.__name__)).strip().lower()
    is_unsupported = bool(base) and isinstance(ie, type) and issubclass(ie, base)
    if not is_unsupported and name not in _UNSUPPORTED_REASONS:
        return None
    return _UNSUPPORTED_REASONS.get(name, REASON_UNSUPPORTED if is_unsupported else None)


def match_extractor_class(url: str):
    """The first non-generic extractor class that claims ``url``."""
    for ie in _extractors():
        try:
            if ie.suitable(url):
                return ie
        except Exception:
            continue
    return None


def match_extractor(url: str) -> Optional[str]:
    """Return the name of the first non-generic extractor that claims ``url``."""
    ie = match_extractor_class(url)
    return None if ie is None else str(getattr(ie, "IE_NAME", ie.__name__))


def is_direct_media(url: str) -> bool:
    """True when the URL path ends in a media extension (query ignored).

    Used both to classify import candidates and to let the worker skip the
    flat enumeration pass for a URL that is plainly a file, not a playlist.
    """
    if not url:
        return False
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    if not path:
        return False
    return PurePosixPath(path).suffix.lstrip(".").lower() in MEDIA_EXTENSIONS


def _allow_generic_default() -> bool:
    from . import settings  # local import: settings must stay importable alone

    try:
        return bool(settings.get_general().allow_generic)
    except Exception:  # pragma: no cover - defensive
        return True


def validate_urls(
    urls: List[str], allow_generic: Optional[bool] = None
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Classify ``urls`` into candidates and rejections.

    Candidates are ``{"url", "extractor", "kind"}``; rejections are
    ``{"url", "reason"}``. ``allow_generic`` defaults to the persisted general
    setting.
    """
    if allow_generic is None:
        allow_generic = _allow_generic_default()

    candidates: List[Dict[str, str]] = []
    rejected: List[Dict[str, str]] = []
    for url in urls:
        if not url or not str(url).lower().startswith(("http://", "https://")):
            rejected.append({"url": url, "reason": REASON_NOT_URL})
            continue

        ie = match_extractor_class(url)
        reason = unsupported_reason(ie)
        if reason:
            rejected.append({"url": url, "reason": reason})
            continue
        if ie is not None:
            candidates.append(
                {
                    "url": url,
                    "extractor": str(getattr(ie, "IE_NAME", ie.__name__)),
                    "kind": "site",
                }
            )
            continue
        if is_direct_media(url):
            candidates.append({"url": url, "extractor": "direct", "kind": "direct"})
            continue
        if allow_generic:
            candidates.append({"url": url, "extractor": "generic", "kind": "generic"})
        else:
            rejected.append({"url": url, "reason": REASON_NO_GENERIC})
    return candidates, rejected


def import_payload(
    payload: str,
    base_url: Optional[str] = None,
    allow_generic: Optional[bool] = None,
) -> Dict[str, object]:
    """Full pipeline: extract -> dedupe -> classify."""
    if allow_generic is None:
        allow_generic = _allow_generic_default()
    urls = extract_urls(payload, base_url)
    candidates, rejected = validate_urls(urls, allow_generic=allow_generic)
    return {
        "candidates": candidates,
        "rejected": rejected,
        "total_found": len(urls),
        "allow_generic": bool(allow_generic),
    }


# Warm the extractor list at import so the first request is not slow.
try:  # pragma: no cover - best effort only
    _extractors()
except Exception:  # pragma: no cover
    pass
