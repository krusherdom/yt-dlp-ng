"""yt-dlp extractors for imaglr.com (videos only).

imaglr.com is a Laravel + Inertia.js site: every HTML page embeds its page
props as HTML-escaped JSON in ``<div data-page="...">``. Post pages are public;
profiles and community pages also expose public RSS and JSON endpoints, while
tag pages require a logged-in session.

Image and GIF media are deliberately skipped everywhere -- this app only wants
videos. A single post that carries no video raises an *expected* error and the
playlist enumerators simply skip such posts.
"""

from __future__ import annotations

import html as _html
import itertools
import json
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import (
    ExtractorError,
    clean_html,
    determine_ext,
    int_or_none,
    traverse_obj,
    url_or_none,
)

# Only these are exported as extractors. yt-dlp's plugin loader picks up every
# class whose name ends in "IE", so the shared base *must* stay out of __all__
# (a base class without a _VALID_URL would break suitable() for every URL).
__all__ = ['ImaglrPostIE', 'ImaglrProfileIE', 'ImaglrPageIE', 'ImaglrTagIE']

_BASE = 'https://imaglr.com'
_REFERER = _BASE + '/'
_DEFAULT_MAX_PAGES = 500
_VIDEO_EXTS = ('mp4', 'webm', 'mov', 'm4v', 'mkv')

# Inertia props that are page furniture, never lists of posts.
_NON_POST_PROPS = frozenset({
    'errors', 'auth', 'isImpersonating', 'layoutStyle', 'currentUser',
    'loggedInUser', 'isPremium', 'userCategories', 'meta', 'oembed', 'user',
    'suggestions', 'flash', 'ziggy', 'status', 'canResetPassword',
    'recaptcha_site_key', 'showRegister', 'userPermissions', 'profileName',
})


def _max_pages() -> int:
    """Pagination cap, read from the environment on every call.

    Read lazily (not at import time) so the value can be changed per process
    and monkeypatched in tests.
    """
    try:
        value = int(os.environ.get('IMAGLR_MAX_PAGES') or _DEFAULT_MAX_PAGES)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_PAGES
    return value if value > 0 else _DEFAULT_MAX_PAGES


def _abs_url(raw):
    """Absolutise a possibly-relative media URL, or return None."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    return url_or_none(raw if '://' in raw else urljoin(_BASE, raw))


class ImaglrBaseIE(InfoExtractor):
    """Shared helpers. Never registered as an extractor (see __all__)."""

    _API_HEADERS = {
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': _REFERER,
    }

    # -- generic plumbing ---------------------------------------------------

    def _logged_in(self) -> bool:
        """True when the cookie jar holds *any* imaglr.com cookie.

        This cannot tell a real session from a guest cookie Laravel handed out
        during an earlier request in the same run, so callers must treat it as
        a hint and fall back gracefully on 401/403.
        """
        try:
            cookies = self._get_cookies(_BASE)
        except Exception:
            return False
        return bool(cookies)

    def _inertia_page(self, url, video_id, note='Downloading webpage'):
        """Return ``(inertia_page_dict_or_None, webpage)``."""
        webpage = self._download_webpage(url, video_id, note=note)
        raw = self._search_regex(
            r'data-page="([^"]+)"', webpage, 'inertia page data', default=None)
        if not raw:
            return None, webpage
        try:
            page = json.loads(_html.unescape(raw))
        except ValueError:
            return None, webpage
        return (page if isinstance(page, dict) else None), webpage

    def _api_json(self, url, video_id, query=None, note='Downloading JSON metadata',
                  login_hint=None):
        """JSON GET with the XHR headers imaglr's API expects.

        A 401/403 is translated into an *expected* ExtractorError mentioning
        cookies, so callers can either fall back or surface a useful message.
        """
        try:
            return self._download_json(
                url, video_id, note=note, query=query or {},
                headers=self._API_HEADERS)
        except ExtractorError as exc:
            cause = exc.cause
            if isinstance(cause, HTTPError) and cause.status in (401, 403):
                raise ExtractorError(
                    login_hint or 'imaglr rejected the request (HTTP %d); a valid '
                    'cookies.txt for imaglr.com is required' % cause.status,
                    expected=True, video_id=video_id) from exc
            raise

    # -- post -> entries ----------------------------------------------------

    @staticmethod
    def _unwrap(post):
        """``{'data': {...}}`` (PostDetail) vs a flat post dict (ShowPost)."""
        if isinstance(post, dict) and isinstance(post.get('data'), dict):
            return post['data']
        return post if isinstance(post, dict) else None

    @staticmethod
    def _is_video_media(media) -> bool:
        if not isinstance(media, dict):
            return False
        mtype = media.get('type')
        if isinstance(mtype, str) and mtype.strip():
            # 'image' and 'gif' are skipped even if stored as .mp4.
            return mtype.strip().lower() == 'video'
        url = media.get('media_url') or media.get('cdn_url')
        return determine_ext(url or '', '').lower() in _VIDEO_EXTS

    @classmethod
    def _media_source(cls, post):
        """Reposts carry an empty outer ``media``; the payload is nested."""
        if post.get('media'):
            return post
        for key in ('original_post', 'reposted_from'):
            nested = cls._unwrap(post.get(key))
            if nested and nested.get('media'):
                return nested
        return post

    @staticmethod
    def _post_title(post, source, post_id, uploader):
        for candidate in (post.get('title'), post.get('text_content'),
                          source.get('title'), source.get('text_content')):
            text = clean_html(candidate) if candidate else None
            if text and text.strip():
                text = ' '.join(text.split())
                return text if len(text) <= 80 else text[:77] + '...'
        return f'{uploader} - {post_id}' if uploader else f'imaglr {post_id}'

    @staticmethod
    def _post_tags(post):
        tags = []
        for tag in post.get('tags') or []:
            if isinstance(tag, str):
                name = tag
            elif isinstance(tag, dict):
                name = tag.get('name') or tag.get('tag') or tag.get('slug')
            else:
                name = None
            if name:
                tags.append(str(name))
        return tags

    def _post_entries(self, post):
        """Video info dicts for one post JSON object. ``[]`` when image-only."""
        post = self._unwrap(post)
        if not post:
            return []
        post_id = str(post.get('id') or '').strip()
        if not post_id:
            return []

        source = self._media_source(post)
        videos = [m for m in (source.get('media') or []) if self._is_video_media(m)]
        if not videos:
            return []

        uploader = (traverse_obj(post, ('user', 'name'))
                    or traverse_obj(source, ('user', 'name')))
        title = self._post_title(post, source, post_id, uploader)
        description = clean_html(post.get('text_content') or source.get('text_content')) or None
        webpage_url = f'{_BASE}/post/{post_id}'
        timestamp = int_or_none(post.get('created_at_timestamp')
                                or source.get('created_at_timestamp'))
        tags = self._post_tags(post) or self._post_tags(source)

        entries = []
        multi = len(videos) > 1
        for idx, media in enumerate(videos, 1):
            url = _abs_url(media.get('media_url')) or _abs_url(media.get('cdn_url'))
            if not url:
                continue
            entries.append({
                'id': f'{post_id}-{idx}' if multi else post_id,
                'title': f'{title} ({idx})' if multi else title,
                'url': url,
                'ext': determine_ext(url, 'mp4'),
                'thumbnail': _abs_url(media.get('thumb_url')),
                'width': int_or_none(media.get('width')),
                'height': int_or_none(media.get('height')),
                'description': description,
                'uploader': uploader,
                'uploader_url': f'{_BASE}/profile/{uploader}' if uploader else None,
                'timestamp': timestamp,
                'tags': tags,
                'view_count': int_or_none(post.get('views')),
                'like_count': int_or_none(post.get('likes')),
                'webpage_url': webpage_url,
                'http_headers': {'Referer': _REFERER},
            })
        return entries

    @classmethod
    def _claims_video_without_media(cls, post) -> bool:
        """True for a post that says it is a video but ships no media array.

        The logged-in profile API and the tag page props could not be captured
        while writing this extractor, so their post serialisers are unverified.
        If one of them omits ``media``, trusting ``post_type`` keeps the post in
        the playlist -- ImaglrPostIE then either finds the video on the post page
        or raises the expected "No video in this post" -- instead of silently
        producing an empty playlist.
        """
        if cls._media_source(post).get('media'):
            # Media *was* present and simply held no video: a genuine skip.
            return False
        candidates = [post]
        for key in ('original_post', 'reposted_from'):
            nested = cls._unwrap(post.get(key))
            if nested:
                candidates.append(nested)
        return any(str(c.get('post_type') or '').lower() == 'video' for c in candidates)

    def _post_url_result(self, post):
        """A cheap ``url_result`` for a post, or ``None`` if it has no video.

        Playlists always yield url_results (never full info dicts) so that
        ``extract_flat`` stays cheap and every child job is a post page URL
        rather than a bare CDN mp4.
        """
        unwrapped = self._unwrap(post)
        if not unwrapped:
            return None
        post_id = unwrapped.get('id')
        if not post_id:
            return None

        entries = self._post_entries(post)
        if entries:
            title = entries[0].get('title')
        elif self._claims_video_without_media(unwrapped):
            title = self._post_title(
                unwrapped, self._media_source(unwrapped), str(post_id),
                traverse_obj(unwrapped, ('user', 'name')))
        else:
            return None

        return self.url_result(
            f'{_BASE}/post/{post_id}', ie=ImaglrPostIE.ie_key(),
            video_id=str(post_id), video_title=title)

    @staticmethod
    def _iter_post_objects(items):
        """Feed items are either bare posts or ``{'post': {...}}`` wrappers."""
        for item in items or []:
            if not isinstance(item, dict):
                continue
            inner = item.get('post')
            yield inner if isinstance(inner, dict) else item

    # -- enumerators --------------------------------------------------------

    def _rss_entries(self, feed_path, playlist_id):
        """Public RSS pagination: ``?page=N`` until an empty page."""
        seen = set()
        for page in itertools.count(1):
            if page > _max_pages():
                self.report_warning(
                    f'Stopped after IMAGLR_MAX_PAGES ({_max_pages()}) RSS pages')
                return
            body = self._download_webpage(
                f'{_BASE}{feed_path}', playlist_id,
                note=f'Downloading RSS page {page}',
                errnote='Unable to download RSS feed',
                query={'page': page}, fatal=(page == 1))
            if not body:
                return
            try:
                root = ET.fromstring(body.strip())
            except ET.ParseError as exc:
                if page == 1:
                    raise ExtractorError(
                        f'Could not parse imaglr RSS feed: {exc}', expected=True)
                return
            items = root.findall('./channel/item')
            if not items:
                return
            fresh = 0
            for item in items:
                link = (item.findtext('link') or '').strip()
                if not link or link in seen:
                    continue
                seen.add(link)
                fresh += 1
                if not self._rss_item_is_video(item):
                    continue
                video_id = self._search_regex(
                    r'/post/(\d+)', link, 'post id', default=None)
                yield self.url_result(
                    link, ie=ImaglrPostIE.ie_key(), video_id=video_id,
                    video_title=(item.findtext('title') or '').strip() or None)
            if not fresh:
                # Same page served again -- stop rather than loop forever.
                return

    @staticmethod
    def _rss_item_is_video(item) -> bool:
        for category in item.findall('category'):
            if (category.text or '').strip().lower() == 'video':
                return True
        for enclosure in item.findall('enclosure'):
            if (enclosure.get('type') or '').lower().startswith('video/'):
                return True
            if determine_ext(enclosure.get('url') or '', '').lower() in _VIDEO_EXTS:
                return True
        return False

    def _paginated_json_entries(self, first_url, playlist_id, note_label):
        """Follow ``next_page_url`` / ``has_more`` over ``?page=N`` responses."""
        url = first_url
        page = 1
        seen_ids = set()
        while url and page <= _max_pages():
            data = self._api_json(
                url, playlist_id, note=f'Downloading {note_label} page {page}')
            items = data if isinstance(data, list) else traverse_obj(data, 'data') or []
            if not items:
                return
            for post in self._iter_post_objects(items):
                post_id = self._unwrap(post).get('id') if self._unwrap(post) else None
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                result = self._post_url_result(post)
                if result:
                    yield result
            next_url = traverse_obj(data, 'next_page_url') if isinstance(data, dict) else None
            page += 1
            if next_url:
                url = urljoin(_BASE, next_url)
            elif isinstance(data, dict) and data.get('has_more'):
                url = self._with_page(first_url, page)
            else:
                return
        if url:
            self.report_warning(
                f'Stopped after IMAGLR_MAX_PAGES ({_max_pages()}) pages')

    @staticmethod
    def _with_page(url, page):
        base = re.sub(r'([?&])page=\d+', r'\1', url).rstrip('?&')
        sep = '&' if '?' in base else '?'
        return f'{base}{sep}page={page}'


class ImaglrPostIE(ImaglrBaseIE):
    IE_NAME = 'imaglr:post'
    IE_DESC = 'imaglr.com single post (videos only)'
    _VALID_URL = r'https?://(?:www\.)?imaglr\.com/(?:p/[^/?#]+/)?post/(?P<id>\d+)'
    _TESTS = [{
        'url': 'https://imaglr.com/post/90040737',
        'info_dict': {
            'id': '90040737',
            'ext': 'mp4',
            'uploader': 'My My My',
        },
        'params': {'skip_download': True},
    }, {
        # Image-only repost -- must fail with an expected error.
        'url': 'https://imaglr.com/post/84973417',
        'only_matching': True,
    }, {
        'url': 'https://imaglr.com/p/art/post/84973417',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        post_id = self._match_id(url)
        page, webpage = self._inertia_page(url, post_id)
        post = traverse_obj(page, ('props', 'post'))

        entries = self._post_entries(post) if post else []
        if entries:
            if len(entries) == 1:
                return entries[0]
            return self.playlist_result(
                entries, post_id, entries[0].get('title'), multi_video=True)

        if post:
            # Props parsed fine and there simply is no video in this post.
            raise ExtractorError('No video in this post', expected=True,
                                 video_id=post_id)

        # No usable props (layout change?) -- fall back to the og:video tag.
        og_url = _abs_url(self._og_search_video_url(webpage, default=None))
        if og_url and determine_ext(og_url, '').lower() in _VIDEO_EXTS:
            return {
                'id': post_id,
                'title': self._og_search_title(webpage, default=None) or f'imaglr {post_id}',
                'url': og_url,
                'ext': determine_ext(og_url, 'mp4'),
                'thumbnail': self._og_search_thumbnail(webpage, default=None),
                'webpage_url': f'{_BASE}/post/{post_id}',
                'http_headers': {'Referer': _REFERER},
            }
        raise ExtractorError('No video in this post', expected=True, video_id=post_id)


class ImaglrProfileIE(ImaglrBaseIE):
    IE_NAME = 'imaglr:profile'
    IE_DESC = 'imaglr.com user profile (videos only)'
    # Trailing anchor keeps /profile/<name>/rss and /profile/<name>/likes out.
    _VALID_URL = r'https?://(?:www\.)?imaglr\.com/profile/(?P<id>[^/?#]+)/?(?:[?#]|$)'
    _TESTS = [{
        'url': 'https://imaglr.com/profile/handyman',
        'only_matching': True,
    }, {
        'url': 'https://imaglr.com/profile/handyman/',
        'only_matching': True,
    }]

    def _profile_json_entries(self, name, first_page):
        take = 20
        skip = 0
        data = first_page
        seen_ids = set()
        for page in itertools.count(1):
            items = data if isinstance(data, list) else traverse_obj(data, 'data') or []
            if not items:
                return
            for post in self._iter_post_objects(items):
                unwrapped = self._unwrap(post)
                post_id = unwrapped.get('id') if unwrapped else None
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                result = self._post_url_result(post)
                if result:
                    yield result
            if isinstance(data, dict) and data.get('has_more') is False:
                return
            if len(items) < take:
                return
            if page + 1 > _max_pages():
                self.report_warning(
                    f'Stopped after IMAGLR_MAX_PAGES ({_max_pages()}) profile pages')
                return
            skip += take
            try:
                data = self._api_json(
                    f'{_BASE}/profile/{name}/posts', name,
                    note=f'Downloading profile JSON page {page + 1}',
                    query={'skip': skip, 'take': take,
                           'paginated': 'false', 'type': 'video'})
            except ExtractorError as exc:
                # Keep the pages we already yielded rather than failing the
                # whole playlist (the worker would then retry the profile URL
                # as a direct download and fail a second time).
                self.report_warning(
                    f'Profile JSON page {page + 1} failed ({exc}); '
                    f'stopping after {page} page(s)')
                return

    def _real_extract(self, url):
        name = self._match_id(url)
        playlist_title = f'{name} - imaglr'

        if self._logged_in():
            try:
                first = self._api_json(
                    f'{_BASE}/profile/{name}/posts', name,
                    note='Downloading profile JSON page 1 (logged-in API)',
                    query={'skip': 0, 'take': 20,
                           'paginated': 'false', 'type': 'video'})
            except ExtractorError as exc:
                self.report_warning(
                    f'Logged-in profile API unavailable ({exc}); falling back to RSS')
                first = None
            if first is not None:
                self.to_screen('Using the logged-in profile API')
                return self.playlist_result(
                    self._profile_json_entries(name, first), name, playlist_title)

        return self.playlist_result(
            self._rss_entries(f'/rss/user/{name}', name), name, playlist_title)


class ImaglrPageIE(ImaglrBaseIE):
    IE_NAME = 'imaglr:page'
    IE_DESC = 'imaglr.com community page (videos only)'
    _VALID_URL = r'https?://(?:www\.)?imaglr\.com/p/(?P<id>[^/?#]+)/?(?:[?#]|$)'
    _TESTS = [{
        'url': 'https://imaglr.com/p/art',
        'only_matching': True,
    }]

    def _entries(self, slug):
        yielded = 0
        try:
            for result in self._paginated_json_entries(
                    f'{_BASE}/p/{slug}/posts?page=1', slug, 'page JSON'):
                yielded += 1
                yield result
            return
        except ExtractorError as exc:
            if yielded:
                # Partial success: don't restart from RSS and duplicate entries.
                self.report_warning(f'Page JSON API stopped early: {exc}')
                return
            self.report_warning(f'Page JSON API failed ({exc}); falling back to RSS')
        yield from self._rss_entries(f'/rss/page/{slug}', slug)

    def _real_extract(self, url):
        slug = self._match_id(url)
        return self.playlist_result(self._entries(slug), slug, f'{slug} - imaglr')


class ImaglrTagIE(ImaglrBaseIE):
    IE_NAME = 'imaglr:tag'
    IE_DESC = 'imaglr.com tag (videos only, needs cookies)'
    _VALID_URL = r'https?://(?:www\.)?imaglr\.com/tag/(?P<id>[^/?#]+)'
    _TESTS = [{
        'url': 'https://imaglr.com/tag/art',
        'only_matching': True,
    }]

    _LOGIN_HINT = ('imaglr tag pages require a logged-in session; upload a '
                   'cookies.txt for imaglr.com in Settings')

    @classmethod
    def _looks_like_post(cls, obj) -> bool:
        if not isinstance(obj, dict):
            return False
        if 'post_type' in obj and 'id' in obj:
            return True
        inner = obj.get('post')
        return isinstance(inner, dict) and 'post_type' in inner

    @classmethod
    def _find_posts_prop(cls, props):
        """Locate the posts collection in an unknown Inertia props shape.

        Returns ``(name, items, next_page_url)`` or ``None``. Written
        defensively: the tag page's prop name could not be confirmed without a
        logged-in session.
        """
        if not isinstance(props, dict):
            return None
        for name, value in props.items():
            if name in _NON_POST_PROPS:
                continue
            if isinstance(value, list):
                if value and cls._looks_like_post(value[0]):
                    return name, value, None
            elif isinstance(value, dict):
                inner = value.get('data')
                if isinstance(inner, list) and inner and cls._looks_like_post(inner[0]):
                    return name, inner, value.get('next_page_url')
        return None

    def _follow_page(self, url, tag, version, page):
        """Fetch one more tag page, tolerating JSON *or* an Inertia HTML body.

        The pagination target may be an API route or the Inertia page route, so
        the Inertia protocol headers are sent (the server then answers with the
        ``{component, props, ...}`` JSON) and an HTML answer is still handled by
        digging ``data-page`` back out of it.
        """
        headers = dict(self._API_HEADERS)
        headers['X-Inertia'] = 'true'
        if version:
            headers['X-Inertia-Version'] = str(version)
        body = self._download_webpage(
            url, tag, note=f'Downloading tag page {page}', headers=headers)
        if not body:
            return None
        body = body.strip()
        if body[:1] in ('{', '['):
            try:
                return json.loads(body)
            except ValueError:
                pass
        raw = self._search_regex(
            r'data-page="([^"]+)"', body, 'inertia page data', default=None)
        if not raw:
            return None
        try:
            return json.loads(_html.unescape(raw))
        except ValueError:
            return None

    def _entries(self, tag, items, next_page_url, version=None):
        seen_ids = set()
        page = 1
        while True:
            for post in self._iter_post_objects(items):
                unwrapped = self._unwrap(post)
                post_id = unwrapped.get('id') if unwrapped else None
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                result = self._post_url_result(post)
                if result:
                    yield result
            if not next_page_url:
                return
            page += 1
            if page > _max_pages():
                self.report_warning(
                    f'Stopped after IMAGLR_MAX_PAGES ({_max_pages()}) tag pages')
                return
            try:
                data = self._follow_page(
                    urljoin(_BASE, next_page_url), tag, version, page)
            except ExtractorError as exc:
                self.report_warning(
                    f'imaglr tag page {page} failed ({exc}); '
                    f'stopping after {page - 1} page(s)')
                return
            if isinstance(data, list):
                items, next_page_url = data, None
                continue
            if not isinstance(data, dict):
                self.report_warning(
                    'Unrecognised imaglr tag pagination response; stopping')
                return
            if isinstance(data.get('data'), list):
                items = data['data']
                next_page_url = data.get('next_page_url')
                continue
            found = self._find_posts_prop(data.get('props') or data)
            if not found:
                self.report_warning(
                    'Unrecognised imaglr tag pagination response; stopping')
                return
            _, items, next_page_url = found

    def _real_extract(self, url):
        tag = self._match_id(url)
        if not self._logged_in():
            raise ExtractorError(self._LOGIN_HINT, expected=True, video_id=tag)

        page, _ = self._inertia_page(f'{_BASE}/tag/{tag}', tag)
        if not page:
            raise ExtractorError(
                'Could not read the imaglr tag page data', expected=True, video_id=tag)

        component = str(page.get('component') or '')
        if 'login' in component.lower() or component.startswith('Auth/'):
            raise ExtractorError(self._LOGIN_HINT, expected=True, video_id=tag)

        found = self._find_posts_prop(page.get('props') or {})
        if not found:
            self.report_warning(
                f'Unrecognised imaglr tag page layout (component {component!r}); '
                f'props: {sorted((page.get("props") or {}).keys())}')
            raise ExtractorError(
                'Could not find any posts on this imaglr tag page', expected=True,
                video_id=tag)

        prop_name, items, next_page_url = found
        self.write_debug(f'Using tag posts prop {prop_name!r} ({len(items)} items)')
        return self.playlist_result(
            self._entries(tag, items, next_page_url, page.get('version')),
            tag, f'#{tag} - imaglr')
