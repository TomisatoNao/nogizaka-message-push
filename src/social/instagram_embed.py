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


def _extract_page_data(page, *, max_media: int) -> tuple[list[MediaItem], str, str, str, str]:
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
        if not _is_media_url(value) or value in seen or len(media) >= max_media:
            return
        seen.add(value)
        media.append(MediaItem(type=kind, url=value, alt_text=alt))

    for item in videos or []:
        if not isinstance(item, dict):
            continue
        sources = item.get("sources") or []
        source = item.get("src") or (sources[0] if sources else "")
        if source:
            add("video", source)
        elif item.get("poster"):
            # A video without a source is not downloadable; the poster is a
            # useful, honest fallback rather than an empty post.
            add("image", item["poster"])

    # Newer Embed builds mark the active media as ``EmbeddedMediaImage``;
    # older builds also keep carousel slides as hidden ``img`` nodes.  Include
    # both, while filtering profile avatars and 100/150px recommendation
    # thumbnails that otherwise look like valid CDN media.
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
                        page, max_media=max_media
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
