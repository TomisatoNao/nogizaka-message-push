"""Instagram public Embed fallback.

Instagram's private ``media/info`` endpoint increasingly requires a logged-in
session, even for public posts.  Public post/reel Embed pages are a separate,
anonymous surface and expose the CDN media that a normal browser can display.
This module deliberately does *not* load the application's Instagram cookies:
the fallback is anonymous by design and Stories are not supported here.

The public Embed is a best-effort compatibility path.  Instagram can still
hide a post behind login, age/region checks, a WAF challenge, or remove it;
callers should keep their authenticated/API and yt-dlp fallbacks around.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import threading
from datetime import datetime
from urllib.parse import urlparse

from src.social.models import MediaItem, Post

log = logging.getLogger("collink")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_SHORTCODE_RE = re.compile(
    r"instagram\.com/(p|reel|tv)/([^/?#\s]+)", re.IGNORECASE
)
_RESERVED_PATHS = {
    "accounts", "about", "direct", "directory", "emails", "explore",
    "legal", "reels", "session", "stories", "web", "p", "reel", "tv",
}


class InstagramEmbedError(RuntimeError):
    """Base error for the anonymous Embed path."""


class InstagramEmbedUnavailable(InstagramEmbedError):
    """Embed could not be loaded or did not expose any media."""


def _post_parts(url: str) -> tuple[str, str, str]:
    """Return ``(kind, shortcode, embed_url)`` for a public post URL."""
    match = _SHORTCODE_RE.search(url or "")
    if not match:
        raise InstagramEmbedUnavailable("仅支持 Instagram 公开帖子、Reel 或 TV 链接")
    kind, shortcode = match.group(1).lower(), match.group(2)
    # Keep the canonical path.  The query string on a shared link is not
    # needed by Embed and can contain tracking parameters or an img_index.
    embed_url = f"https://www.instagram.com/{kind}/{shortcode}/embed/captioned/"
    return kind, shortcode, embed_url


def _best_srcset(value: str) -> str:
    """Choose the largest URL in an image ``srcset`` attribute."""
    candidates: list[tuple[int, str]] = []
    for part in (value or "").split(","):
        fields = part.strip().split()
        if not fields:
            continue
        width = 0
        if len(fields) > 1:
            m = re.match(r"(\d+)w$", fields[1])
            if m:
                width = int(m.group(1))
        candidates.append((width, fields[0]))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1]


def _normalise_url(value: str) -> str:
    """Drop whitespace/HTML escaping without touching signed CDN queries."""
    return (value or "").strip().replace("&amp;", "&")


def _is_media_url(value: str) -> bool:
    value = (value or "").strip()
    if not value.startswith(("http://", "https://")):
        return False
    # Do not accidentally treat an Instagram page URL as a downloadable
    # asset.  ``scontent*.cdninstagram.com`` is intentionally allowed.
    host = (urlparse(value).hostname or "").lower()
    return host not in {"instagram.com", "www.instagram.com", "i.instagram.com"}


def _media_key(value: str) -> str:
    """Return a stable identity for alternate renditions of one CDN asset.

    Embed data includes both the root media and its sidecar child.  They can
    point at the same path with different signed resize/query parameters, so
    comparing the complete URL would incorrectly count one photo twice.
    """
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host.endswith(".cdninstagram.com") or host.endswith(".fbcdn.net"):
        return parsed.path.lower() or value
    return value


def _author_from_links(links: list[dict]) -> tuple[str, str]:
    """Infer username/display name from links rendered by the Embed page."""
    for link in links:
        href = str(link.get("href") or "")
        parsed = urlparse(href)
        if parsed.hostname not in {"instagram.com", "www.instagram.com"}:
            continue
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) != 1 or parts[0].lower() in _RESERVED_PATHS:
            continue
        username = parts[0]
        label = str(link.get("text") or "").strip()
        return label or f"@{username}", username
    return "Instagram 用户", ""


def _timestamp_from_value(value: str) -> str:
    if not value:
        return ""
    try:
        # Keep the project's human-readable timestamp convention while
        # accepting the ISO-8601 value used by article:published_time.
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


_STRUCTURED_FIELD_RE = re.compile(
    r"\\?[\"'](?P<field>display_url|video_url)\\?[\"']\s*:\s*",
    re.IGNORECASE,
)


def _read_embedded_string(text: str, start: int) -> tuple[str, int] | None:
    """Read one JSON/JS string beginning at ``start``.

    Instagram currently embeds the carousel payload in an inert ``script``
    node.  Depending on the Embed revision the quotation mark is either a
    normal JSON quote or itself escaped (``\"``).  A small scanner is more
    tolerant than attempting to parse the entire script, which is often
    wrapped in JavaScript assignments and HTML-escaped JSON.
    """
    # In the Embed bootstrap payload the *delimiters* themselves can be
    # escaped (``:\"https:\\/\\/...\"``), while the URL still contains
    # ordinary backslash escapes.  Treat the escaped quote as the delimiter
    # in that mode; otherwise the scanner would consume the following JSON
    # fields and produce a malformed URL containing ``display_resources``.
    escaped_delimiter = (
        start + 1 < len(text)
        and text[start] == "\\"
        and text[start + 1] in {"\"", "'"}
    )
    if escaped_delimiter:
        quote = text[start + 1]
        i = start + 2
    else:
        if start >= len(text) or text[start] not in {"\"", "'"}:
            return None
        quote = text[start]
        i = start + 1
    if quote not in {"\"", "'"}:
        return None
    chars: list[str] = []
    while i < len(text):
        char = text[i]
        if escaped_delimiter and char == "\\" and i + 1 < len(text) and text[i + 1] == quote:
            return "".join(chars), i + 2
        if char == "\\" and i + 1 < len(text):
            chars.extend((char, text[i + 1]))
            i += 2
            continue
        if char == quote:
            return "".join(chars), i + 1
        chars.append(char)
        i += 1
    return None


def _decode_embedded_string(value: str) -> str:
    """Decode one escaped URL from Instagram's structured payload."""
    raw = str(value or "")
    decoded = raw
    # The value can be escaped once by the JSON payload and once more by the
    # JavaScript string containing that payload.  Decode at most three layers
    # so ``\\u00253D`` and ``\\u00253D`` converge to the same signed URL while
    # still treating the input as data (never as executable JavaScript).
    for _ in range(3):
        try:
            candidate = json.loads(f'"{decoded}"')
        except (TypeError, ValueError, json.JSONDecodeError):
            break
        if candidate == decoded:
            break
        decoded = candidate
    decoded = str(decoded or "").replace(r"\/", "/")
    decoded = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        decoded,
    )
    decoded = decoded.replace(r'\"', '"').replace(r"\\", "\\")
    # A second escaping layer can remain after decoding (the response may
    # contain a JSON string inside a JavaScript string).  Removing only the
    # JSON slash escape is safe for URLs and makes both ``\/`` and
    # ``\\\/`` representations converge without evaluating script content.
    return html.unescape(str(decoded or "")).replace(r"\/", "/").strip()


def _structured_media_from_script(text: str) -> list[MediaItem]:
    """Extract ordered ``display_url``/``video_url`` values from one script.

    This is intentionally an inert-field extractor: it never executes or
    deserializes the surrounding script.  The surrounding page is selected by
    :func:`_structured_media_from_page`, which limits extraction to the post's
    GraphSidecar payload whenever Instagram exposes one.
    """
    if not isinstance(text, str) or not text:
        return []
    text = html.unescape(text)
    media: list[MediaItem] = []
    seen: set[str] = set()
    for match in _STRUCTURED_FIELD_RE.finditer(text):
        parsed = _read_embedded_string(text, match.end())
        if parsed is None:
            continue
        raw_value, _ = parsed
        value = _normalise_url(_decode_embedded_string(raw_value))
        key = _media_key(value)
        if not _is_media_url(value) or key in seen:
            continue
        seen.add(key)
        kind = "video" if match.group("field").lower() == "video_url" else "image"
        media.append(MediaItem(type=kind, url=value))
    return media


def _structured_media_from_page(page, *, shortcode: str = "", max_media: int) -> list[MediaItem]:
    """Read carousel media from inert script nodes, if present.

    The rendered Embed DOM often contains only the active slide (or a handful
    of lazy-loaded slides).  ``GraphSidecar.edge_sidecar_to_children`` keeps
    the complete carousel, so it is preferred when available and the DOM
    remains the fallback for older Embed revisions.
    """
    try:
        scripts = page.locator("script").evaluate_all(
            """els => els.map(el => el.textContent || el.innerText || '')"""
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return []
    if not isinstance(scripts, list):
        return []
    candidates = [
        s for s in scripts
        if isinstance(s, str) and ("display_url" in s or "video_url" in s)
    ]
    if not candidates:
        return []

    # Prefer the script carrying this post's sidecar payload.  A few Embed
    # versions omit the shortcode in the script, so retain a conservative
    # marker fallback before considering all display_url scripts.
    shortcode_lower = str(shortcode or "").lower()
    focused = [
        s for s in candidates
        if (shortcode_lower and shortcode_lower in s.lower())
        or "edge_sidecar_to_children" in s
        or "graphsidecar" in s.lower()
    ]
    selected = focused or candidates
    media: list[MediaItem] = []
    seen: set[str] = set()
    for script in selected:
        for item in _structured_media_from_script(script):
            key = _media_key(item.url)
            if key in seen or len(media) >= max_media:
                continue
            seen.add(key)
            media.append(item)
        if len(media) >= max_media:
            break
    return media


def _extract_page_data(
    page,
    *,
    max_media: int,
    shortcode: str = "",
) -> tuple[list[MediaItem], str, str, str, str]:
    """Extract media and lightweight metadata from a Playwright page."""
    # Evaluate only DOM attributes, never page-provided scripts.  CDN links
    # are returned as plain strings and are downloaded later by MediaDownloader.
    images = page.locator("img").evaluate_all(
        """els => els.map(el => ({
            src: el.currentSrc || el.src || el.getAttribute('src') || '',
            srcset: el.getAttribute('srcset') || '',
            alt: el.getAttribute('alt') || '',
            className: typeof el.className === 'string' ? el.className : '',
            width: el.naturalWidth || 0,
            height: el.naturalHeight || 0
        }))"""
    )
    videos = page.locator("video").evaluate_all(
        """els => els.map(el => ({
            src: el.currentSrc || el.src || '',
            poster: el.poster || '',
            sources: Array.from(el.querySelectorAll('source')).map(s => s.src || s.getAttribute('src') || '')
        }))"""
    )
    media: list[MediaItem] = []
    seen: set[str] = set()

    def add(kind: str, value: str, alt: str = "") -> None:
        value = _normalise_url(value)
        key = _media_key(value)
        if not _is_media_url(value) or key in seen or len(media) >= max_media:
            return
        seen.add(key)
        media.append(MediaItem(type=kind, url=value, alt_text=alt))

    # The structured payload is the only reliable source for all carousel
    # slides.  Add it before DOM media so the returned order follows
    # Instagram's sidecar order and DOM fallback cannot displace a slide.
    structured_media = _structured_media_from_page(
        page,
        shortcode=shortcode,
        max_media=max_media,
    )
    for item in structured_media:
        add(item.type, item.url, item.alt_text)

    # A complete GraphSidecar is authoritative.  Its DOM counterpart often
    # exposes a different rendition of the active slide, which would create a
    # false extra attachment after URL-based deduplication.  Only use DOM
    # extraction when structured data is absent (older Embed revisions).
    dom_media_count = 0
    if not structured_media:
        dom_media_start = len(media)
        for item in videos or []:
            if not isinstance(item, dict):
                continue
            sources = item.get("sources") or []
            source = item.get("src") or (sources[0] if sources else "")
            if source:
                add("video", source)
            elif item.get("poster"):
                # A video without a source is not downloadable; the poster is
                # a useful, honest fallback rather than an empty post.
                add("image", item["poster"])

        # Newer Embed builds mark the active media as ``EmbeddedMediaImage``;
        # older builds also keep carousel slides as hidden ``img`` nodes.
        # Include both, while filtering profile avatars and tiny thumbnails.
        for item in images or []:
            if not isinstance(item, dict):
                continue
            src_value = _best_srcset(item.get("srcset") or "") or item.get("src") or ""
            class_name = str(item.get("className") or "")
            is_primary = "embeddedmediaimage" in class_name.lower()
            small_variant = bool(re.search(r"s(?:100|150)x(?:100|150)", src_value.lower()))
            small_dimensions = 0 < int(item.get("width") or 0) <= 200 and 0 < int(item.get("height") or 0) <= 200
            if not is_primary and (small_variant or small_dimensions):
                continue
            add("image", src_value, str(item.get("alt") or ""))
        dom_media_count = len(media) - dom_media_start

    # OG metadata is useful when the Embed renderer lazy-loads the first image
    # after our selector timeout.  It is deliberately a last resort.

    if not media:
        for selector in ('meta[property="og:image"]', 'meta[name="twitter:image"]'):
            try:
                locator = page.locator(selector)
                og_image = (
                    locator.first.get_attribute("content") if locator.count() else ""
                ) or ""
            except Exception:  # pragma: no cover - browser-specific DOM race
                og_image = ""
            if og_image:
                add("image", og_image)
                if media:
                    break

    try:
        links = page.locator("a").evaluate_all(
            """els => els.map(a => ({href: a.href || '', text: (a.textContent || '').trim()}))"""
        )
    except Exception:  # pragma: no cover - browser-specific DOM race
        links = []
    author, username = _author_from_links(links if isinstance(links, list) else [])

    caption = ""
    for selector in ('meta[property="og:description"]', 'meta[name="description"]'):
        try:
            locator = page.locator(selector)
            caption = (
                locator.first.get_attribute("content") if locator.count() else ""
            ) or ""
            caption = caption.strip()
        except Exception:  # pragma: no cover - browser-specific DOM race
            caption = ""
        if caption:
            break

    published = ""
    try:
        locator = page.locator('meta[property="article:published_time"]')
        published = (
            locator.first.get_attribute("content") if locator.count() else ""
        ) or ""
    except Exception:  # pragma: no cover - browser-specific DOM race
        pass
    log.debug(
        "[single_fetcher] Instagram Embed 媒体候选 | structured=%d | dom=%d | unique=%d",
        len(structured_media),
        dom_media_count,
        len(media),
    )
    return media, author, username, caption, _timestamp_from_value(published)


def _fetch_public_post_sync(url: str, *, proxy: str, timeout: float, max_media: int) -> Post:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise InstagramEmbedUnavailable("Playwright 未安装，无法使用匿名 Embed 回退") from exc

    kind, shortcode, embed_url = _post_parts(url)
    timeout_ms = max(5_000, int(float(timeout) * 1000))
    try:
        with sync_playwright() as playwright:
            launch_args = {"headless": True}
            if proxy:
                launch_args["proxy"] = {"server": proxy}
            browser = playwright.chromium.launch(**launch_args)
            try:
                context = browser.new_context(
                    user_agent=_UA,
                    locale="en-US",
                    viewport={"width": 1280, "height": 900},
                )
                page = context.new_page()
                try:
                    page.goto(embed_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        page.wait_for_selector(
                            ".EmbeddedMediaImage,video,meta[property='og:image']",
                            timeout=min(timeout_ms, 8_000),
                        )
                    except PlaywrightTimeoutError:
                        # Some Embed revisions do not use the class above. A
                        # short generic wait still gives their lazy image a
                        # chance to render, while extraction decides whether it
                        # is an actual post media or just an avatar.
                        try:
                            page.wait_for_selector(
                                "img,video,meta[property='og:image']",
                                timeout=min(1_000, timeout_ms),
                            )
                        except PlaywrightTimeoutError:
                            pass
                    # Give lazy media one rendering tick without waiting for
                    # networkidle (Instagram keeps analytics sockets open).
                    page.wait_for_timeout(250)
                    media, author, username, caption, timestamp = _extract_page_data(
                        page, max_media=max_media, shortcode=shortcode
                    )
                finally:
                    context.close()
            finally:
                browser.close()
    except (PlaywrightError, PlaywrightTimeoutError, OSError) as exc:
        raise InstagramEmbedUnavailable(f"公开 Embed 加载失败：{type(exc).__name__}") from exc

    if not media:
        raise InstagramEmbedUnavailable(
            "公开 Embed 未返回媒体（帖子可能已删除、受限、需要登录或被 Instagram WAF 拦截）"
        )

    log.info(
        "[single_fetcher] Instagram 公开 Embed 解析成功 | shortcode=%s | kind=%s | media=%d | auth=anonymous",
        shortcode,
        kind,
        len(media),
    )
    return Post(
        platform="instagram",
        post_id=shortcode,
        author=author,
        text=caption,
        media=media,
        timestamp=timestamp,
        extra={
            "url": url,
            "username": username,
            "author": author,
            "kind": "reel" if kind == "reel" else "post",
            "source": "public_embed",
            "auth": "anonymous",
            "embed_url": embed_url,
        },
    )


def fetch_public_post(
    url: str,
    *,
    proxy: str = "",
    timeout: float = 25,
    max_media: int = 20,
) -> Post:
    """Parse one public Instagram post/reel without application cookies.

    Playwright's synchronous API cannot run in a thread that already owns an
    asyncio event loop (QQ Bot commands do).  In that case the browser work is
    isolated in a short-lived worker thread; callers remain synchronous.
    """
    try:
        timeout = max(5.0, float(timeout))
    except (TypeError, ValueError):
        timeout = 25.0
    try:
        max_media = max(1, min(50, int(max_media)))
    except (TypeError, ValueError):
        max_media = 20
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _fetch_public_post_sync(url, proxy=proxy, timeout=timeout, max_media=max_media)

    result: dict[str, Post] = {}
    error: list[BaseException] = []

    def worker() -> None:
        try:
            result["post"] = _fetch_public_post_sync(
                url, proxy=proxy, timeout=timeout, max_media=max_media
            )
        except BaseException as exc:  # propagate the original typed error
            error.append(exc)

    thread = threading.Thread(target=worker, name="instagram-embed", daemon=True)
    thread.start()
    thread.join(max(5.0, timeout + 5.0))
    if thread.is_alive():
        raise InstagramEmbedUnavailable("公开 Embed 解析超时")
    if error:
        raise error[0]
    return result["post"]


__all__ = ["InstagramEmbedError", "InstagramEmbedUnavailable", "fetch_public_post"]
