"""
social/single_fetcher.py — 单条社媒链接解析与手动推送实用工具
支持输入 X (Twitter)、Instagram、TikTok 链接，解析媒体并推送到指定通道。
"""

import logging
import math
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from src.social.downloader import MediaDownloader
from src.social.forwarder import SocialForwarder
from src.social.models import MediaItem, Post

log = logging.getLogger("collink")

_JST = timezone(timedelta(hours=9))
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TWEET_RESULT_URL = "https://cdn.syndication.twimg.com/tweet-result"


def _syndication_token(tweet_id: str) -> str:
    """计算 cdn.syndication.twimg.com 单推接口所需的 token（免登录）。"""
    try:
        n = (int(tweet_id) / 1e15) * math.pi
    except (TypeError, ValueError):
        return "0"
    whole = int(n)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    head = ""
    if whole == 0:
        head = "0"
    while whole:
        whole, r = divmod(whole, 36)
        head = digits[r] + head
    tail = ""
    for _ in range(20):
        n = (n - math.floor(n)) * 36
        d = math.floor(n)
        tail += digits[d]
    combined = (head + tail).replace("0", "").replace(".", "")
    return combined or "0"


def _orig_image(url: str) -> str:
    """把 pbs.twimg.com 图片地址改写成原图（最高画质）。"""
    if not url or "pbs.twimg.com" not in url:
        return url
    base, _, query = url.partition("?")
    m = re.match(r"(.*/[\w-]+)\.(jpg|jpeg|png|webp)$", base, re.I)
    if m:
        return f"{m.group(1)}?format={m.group(2).lower()}&name=orig"
    if query:
        parts = [p for p in query.split("&") if p and not p.startswith("name=")]
        parts.append("name=orig")
        return f"{base}?{'&'.join(parts)}"
    return f"{base}?name=orig"


class SocialUrlParser:
    """解析单条社媒链接为统一 Post 对象。"""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA})

    def parse(self, url: str) -> Post:
        url = (url or "").strip()
        if not url:
            raise ValueError("URL 不能为空")

        # 1. 判定 X / Twitter
        if "twitter.com" in url or "x.com" in url or "vxtwitter.com" in url or "fixupx.com" in url:
            return self._parse_x(url)

        # 2. 判定 Instagram
        if "instagram.com" in url:
            return self._parse_instagram(url)

        # 3. 判定 TikTok
        if "tiktok.com" in url:
            return self._parse_tiktok(url)

        raise ValueError("不支持的平台链接，仅支持 X (Twitter)、Instagram 或 TikTok")

    def _parse_x(self, url: str) -> Post:
        m = re.search(r"status/(\d+)", url)
        if not m:
            raise ValueError(f"无法从链接提取 Tweet ID: {url}")
        tweet_id = m.group(1)

        # 尝试通过免登录 syndication tweet-result 获取
        try:
            token = _syndication_token(tweet_id)
            resp = self.session.get(
                TWEET_RESULT_URL,
                params={"id": tweet_id, "lang": "ja", "token": token},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                text = data.get("text", "") or ""
                user = data.get("user") or {}
                author = user.get("name") or user.get("screen_name") or "X 用户"
                screen_name = user.get("screen_name") or ""
                created_at = data.get("created_at") or ""

                media_items = []
                # 解析图片与视频
                media_details = data.get("mediaDetails") or []
                for md in media_details:
                    mtype = md.get("type")
                    if mtype == "photo":
                        pic_url = md.get("media_url_https") or md.get("url") or ""
                        alt = md.get("ext_alt_text") or ""
                        if pic_url:
                            media_items.append(MediaItem(type="image", url=_orig_image(pic_url), alt_text=alt))
                    elif mtype == "video" or mtype == "animated_gif":
                        vinfo = md.get("video_info") or {}
                        variants = vinfo.get("variants") or []
                        mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
                        if mp4s:
                            mp4s.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
                            best_video = mp4s[0].get("url")
                            if best_video:
                                media_items.append(MediaItem(type="video", url=best_video))
                        elif md.get("media_url_https"):
                            media_items.append(MediaItem(type="image", url=_orig_image(md.get("media_url_https"))))

                return Post(
                    platform="x",
                    post_id=tweet_id,
                    author=author,
                    text=text,
                    media=media_items,
                    timestamp=created_at,
                    extra={
                        "screen_name": screen_name,
                        "url": f"https://x.com/{screen_name or '_'}/status/{tweet_id}",
                        "avatar_url": user.get("profile_image_url_https", ""),
                    },
                )
        except Exception as ex:
            log.warning("[single_fetcher] X syndication 解析失败 %s: %s", tweet_id, ex)

        # 回退 yt-dlp 抓取单推
        return self._extract_with_ytdlp(url, platform="x", post_id=tweet_id)

    def _parse_instagram(self, url: str) -> Post:
        m = re.search(r"instagram\.com/(?:p|reel|tv)/([^/?#\s]+)", url)
        shortcode = m.group(1) if m else ""
        return self._extract_with_ytdlp(url, platform="instagram", post_id=shortcode or "ig_post")

    def _parse_tiktok(self, url: str) -> Post:
        m = re.search(r"video/(\d+)", url)
        item_id = m.group(1) if m else "tiktok_post"
        return self._extract_with_ytdlp(url, platform="tiktok", post_id=item_id)

    def _extract_with_ytdlp(self, url: str, platform: str, post_id: str) -> Post:
        """通过 yt-dlp 提取通用社交媒体内容（支持 X/IG/TikTok 视频及图文）。"""
        try:
            import yt_dlp
        except ImportError:
            raise RuntimeError("系统未安装 yt-dlp，无法解析此链接")

        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "skip_download": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                raise RuntimeError(f"解析失败: {e}")

        if not info:
            raise RuntimeError("未能从链接提取到内容信息")

        author = info.get("uploader") or info.get("uploader_id") or platform.upper()
        text = info.get("description") or info.get("title") or ""
        post_id = str(info.get("id") or post_id)
        timestamp = ""
        if info.get("timestamp"):
            dt = datetime.fromtimestamp(info["timestamp"], tz=timezone.utc).astimezone(_JST)
            timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")

        media_items = []
        entries = info.get("entries")
        if entries:
            # 多图 / 轮播 Carousel
            for e in entries:
                v_url = e.get("url")
                if v_url:
                    mtype = "video" if e.get("vcodec") != "none" else "image"
                    media_items.append(MediaItem(type=mtype, url=v_url))
                elif e.get("thumbnails"):
                    best_th = e["thumbnails"][-1].get("url")
                    if best_th:
                        media_items.append(MediaItem(type="image", url=best_th))
        else:
            # 单视频或单图
            v_url = info.get("url")
            vcodec = info.get("vcodec")
            if v_url and vcodec and vcodec != "none":
                media_items.append(MediaItem(type="video", url=v_url))
            elif info.get("thumbnails"):
                best_th = info["thumbnails"][-1].get("url")
                if best_th:
                    media_items.append(MediaItem(type="image", url=best_th))

        return Post(
            platform=platform,
            post_id=post_id,
            author=author,
            text=text,
            media=media_items,
            timestamp=timestamp,
            extra={
                "url": url,
                "uploader_id": info.get("uploader_id", ""),
                "webpage_url": info.get("webpage_url", url),
            },
        )


def manual_push_social_url(
    url: str,
    config: dict,
    target_channels: list[dict] | None = None,
    translate: bool = True,
    archive: bool = True,
) -> dict:
    """全流程：解析链接 → AI 翻译 → 媒体下载 → 通道分发 → 归档。"""
    parser = SocialUrlParser(config)
    post = parser.parse(url)

    # 1. 媒体下载（如为直链）
    downloader = MediaDownloader(config)
    downloader.download(post)

    # 2. 调度转发器分发
    forwarder = SocialForwarder(config, downloader)

    # 翻译
    translated_text = None
    if translate and post.text:
        translated_text = forwarder._translate(post.text)
        if translated_text:
            post.extra["_translated"] = translated_text

    # 若指定了通道则定向推，否则走标准 forward_post
    forwarder.forward_post(post)

    media_preview = [
        {
            "type": m.type,
            "url": m.url,
            "local_path": os.path.basename(m.local_path) if m.local_path else "",
        }
        for m in post.media
    ]

    return {
        "ok": True,
        "platform": post.platform,
        "author": post.author,
        "text": post.text,
        "translation": translated_text,
        "media_count": len(post.media),
        "media": media_preview,
        "timestamp": post.timestamp,
        "post_id": post.post_id,
    }
