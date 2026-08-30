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
from src.social import ig_session
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

    # 视频缩略图与卡片图不支持 name=orig（请求会 404），改用 name=large
    if any(x in url for x in ("/amplify_video_thumb/", "/tweet_video_thumb/", "/ext_tw_video_thumb/", "/card_img/")):
        base, _, query = url.partition("?")
        if query:
            parts = [p for p in query.split("&") if p and not p.startswith("name=")]
            parts.append("name=large")
            return f"{base}?{'&'.join(parts)}"
        return f"{base}?name=large"

    base, _, query = url.partition("?")
    m = re.match(r"(.*/[\w-]+)\.(jpg|jpeg|png|webp)$", base, re.I)
    if m:
        return f"{m.group(1)}?format={m.group(2).lower()}&name=orig"
    if query:
        parts = [p for p in query.split("&") if p and not p.startswith("name=")]
        parts.append("name=orig")
        return f"{base}?{'&'.join(parts)}"
    return f"{base}?name=orig"


def _shortcode_to_media_id(shortcode: str) -> int:
    """将 Instagram shortcode（如 DcG7iiqk5NW）解码为数字 media_id。"""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    media_id = 0
    for char in shortcode:
        idx = alphabet.find(char)
        if idx == -1:
            raise ValueError(f"Invalid character in shortcode: {char}")
        media_id = media_id * 64 + idx
    return media_id


def _get_proxy(config: dict | None = None) -> str:
    """提取代理配置：优先 config 显式设置，其次全局 cfg.PROXY / SOCIAL_CONFIG，最后环境变量。"""
    cfg_dict = config or {}
    try:
        import config.config as cfg
    except Exception:
        cfg = None

    candidate = (
        cfg_dict.get("proxy")
        or cfg_dict.get("social", {}).get("proxy")
        or (getattr(cfg, "PROXY", "") if cfg else "")
        or (getattr(cfg, "SOCIAL_CONFIG", {}).get("proxy", "") if cfg else "")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("ALL_PROXY")
        or ""
    )
    return str(candidate).strip()


class SocialUrlParser:
    """解析单条社媒链接为统一 Post 对象。"""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.proxy = _get_proxy(self.config)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA})
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})

    def parse(self, url: str) -> Post:
        url = (url or "").strip()
        if not url:
            raise ValueError("URL 不能为空")

        # 1. 判定 X / Twitter
        if "twitter.com" in url or "x.com" in url or "vxtwitter.com" in url or "fixupx.com" in url:
            post = self._parse_x(url)
        # 2. 判定 Instagram
        elif "instagram.com" in url:
            post = self._parse_instagram(url)
        # 3. 判定 TikTok / 抖音
        elif "tiktok.com" in url or "douyin.com" in url:
            post = self._parse_tiktok(url)
        else:
            raise ValueError("不支持的平台链接，仅支持 X (Twitter)、Instagram、TikTok 或 抖音")

        post.extra["source_url"] = url
        return post

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
        # 1. 优先判定 Story 链接（如 /stories/suzuno_mio/3974361302645425080/ 或 /stories/highlights/123/）
        m_story = re.search(r"instagram\.com/stories/(?:highlights/(\d+)|([a-zA-Z0-9_.]+)(?:/(\d+))?)", url)
        if m_story:
            hl_id = m_story.group(1)
            username = m_story.group(2)
            story_id = m_story.group(3) or ""
            try:
                post = self._parse_instagram_story(username=username, story_id=story_id, highlight_id=hl_id, url=url)
                if post and post.media:
                    return post
            except Exception as ex:
                log.warning("[single_fetcher] Instagram Story 官方 API 解析失败 (@%s, %s)，回退 yt-dlp: %s", username or hl_id, story_id, ex)

        # 2. 判定普通 Post/Reel 链接（如 /p/xxx/ 或 /reel/xxx/）
        m = re.search(r"instagram\.com/(?:p|reel|tv)/([^/?#\s]+)", url)
        shortcode = m.group(1) if m else ""
        if shortcode:
            try:
                post = self._parse_instagram_api(shortcode, url)
                if post and post.media:
                    return post
            except Exception as ex:
                log.warning("[single_fetcher] Instagram 官方 API 单帖解析失败 (%s)，回退 yt-dlp: %s", shortcode, ex)

        return self._extract_with_ytdlp(url, platform="instagram", post_id=shortcode or "ig_post")

    def _parse_instagram_story(
        self,
        username: str | None = None,
        story_id: str = "",
        highlight_id: str | None = None,
        url: str = "",
    ) -> Post:
        cookies = ig_session.read_cookie_file()
        session = requests.Session()
        if self.proxy:
            session.proxies.update({"http": self.proxy, "https": self.proxy})
        if cookies:
            session.cookies.update(cookies)
        session.headers.update({
            "User-Agent": _UA,
            "X-IG-App-ID": "936619743392459",
            "Accept": "*/*",
            "Accept-Language": "ja,en;q=0.8",
        })
        if cookies.get("csrftoken"):
            session.headers["X-CSRFToken"] = cookies["csrftoken"]

        items = []
        author = username or "Instagram 用户"
        avatar_url = ""
        taken_at = 0

        # 若为 Story Highlight
        if highlight_id:
            reel_id = f"highlight:{highlight_id}"
            r_reel = session.get(
                "https://i.instagram.com/api/v1/feed/reels_media/",
                params={"reel_ids": reel_id},
                timeout=15,
            )
            if r_reel.status_code == 200:
                reel_data = r_reel.json()
                reels = reel_data.get("reels") or {}
                reel = reels.get(reel_id) or (list(reels.values())[0] if reels else {})
                items = reel.get("items") or []
                u = reel.get("user") or {}
                author = u.get("full_name") or u.get("username") or author
                avatar_url = u.get("profile_pic_url") or ""
        elif username:
            # 1. 尝试直接获取 reels_media (通过 username 解析 uid)
            uid = ""
            try:
                r_user = session.get(
                    f"https://i.instagram.com/api/v1/feed/user/{username}/username/",
                    timeout=15,
                )
                if r_user.status_code == 200:
                    u_data = r_user.json()
                    u_obj = u_data.get("user") or {}
                    uid = str(u_obj.get("pk") or u_obj.get("id") or "")
                    author = u_obj.get("full_name") or u_obj.get("username") or username
                    avatar_url = u_obj.get("profile_pic_url") or ""
            except Exception as e:
                log.debug("[single_fetcher] Story 用户 UID 解析异常: %s", e)

            if uid:
                r_reel = session.get(
                    "https://i.instagram.com/api/v1/feed/reels_media/",
                    params={"reel_ids": uid},
                    timeout=15,
                )
                if r_reel.status_code == 200:
                    reel_data = r_reel.json()
                    reels = reel_data.get("reels") or {}
                    reel = reels.get(uid) or (list(reels.values())[0] if reels else {})
                    items = reel.get("items") or []

            # 如果 reels_media 没拿到但有 story_id，尝试 media/{story_id}/info/
            if not items and story_id:
                try:
                    resp = session.get(
                        f"https://i.instagram.com/api/v1/media/{story_id}/info/",
                        timeout=15,
                    )
                    if resp.status_code == 200:
                        items = resp.json().get("items") or []
                except Exception as e:
                    log.debug("[single_fetcher] media/%s/info 尝试失败: %s", story_id, e)

        if not items:
            raise RuntimeError("未能从 Instagram Story 接口获取到媒体内容（可能已超过 24 小时过期或需要登录）")

        media_items = []
        for it in items:
            u = it.get("user") or {}
            if not avatar_url:
                avatar_url = u.get("profile_pic_url") or ""
            if author == username:
                author = u.get("full_name") or u.get("username") or username
            if not taken_at:
                taken_at = int(it.get("taken_at") or 0)

            mt = int(it.get("media_type") or 1)
            # 视频
            if mt == 2:
                video_versions = it.get("video_versions") or []
                if video_versions:
                    video_versions.sort(key=lambda x: (x.get("width", 0) * x.get("height", 0)), reverse=True)
                    best_video = video_versions[0].get("url")
                    if best_video:
                        media_items.append(MediaItem(type="video", url=best_video))
                        continue
            # 图片
            image_candidates = (it.get("image_versions2") or {}).get("candidates") or []
            if image_candidates:
                image_candidates.sort(key=lambda x: (x.get("width", 0) * x.get("height", 0)), reverse=True)
                best_img = image_candidates[0].get("url")
                if best_img:
                    media_items.append(MediaItem(type="image", url=best_img))

        if not media_items:
            raise RuntimeError("未在 Story 中提取到有效图片或视频")

        timestamp = ""
        if taken_at:
            dt = datetime.fromtimestamp(taken_at, tz=timezone.utc).astimezone(_JST)
            timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")

        count_desc = f" (共 {len(media_items)} 条)" if len(media_items) > 1 else ""
        text = f"Instagram Story by @{username or author}{count_desc}"
        log.info("[single_fetcher] Instagram Story API 成功解析 @%s，获得 %d 条媒体", username or author, len(media_items))

        return Post(
            platform="instagram",
            post_id=story_id or f"story_{username}_{taken_at}",
            author=author,
            text=text,
            media=media_items,
            timestamp=timestamp,
            extra={
                "url": url,
                "username": username or "",
                "author": author,
                "avatar_url": avatar_url,
                "kind": "story",
                "story_count": len(media_items),
            },
        )

    def _parse_instagram_api(self, shortcode: str, url: str) -> Post:
        media_id = _shortcode_to_media_id(shortcode)
        cookies = ig_session.read_cookie_file()
        session = requests.Session()
        if self.proxy:
            session.proxies.update({"http": self.proxy, "https": self.proxy})
        if cookies:
            session.cookies.update(cookies)
        app_ua = "Instagram 278.0.0.19.115 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; en_US; 458212170)"
        session.headers.update({
            "User-Agent": app_ua,
            "X-IG-App-ID": "936619743392459",
            "Accept": "*/*",
            "Accept-Language": "ja,en;q=0.8",
        })
        if cookies.get("csrftoken"):
            session.headers["X-CSRFToken"] = cookies["csrftoken"]

        api_url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
        resp = session.get(api_url, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"Instagram media/info API HTTP {resp.status_code}")

        data = resp.json()
        items = data.get("items") or []
        if not items:
            raise RuntimeError("Instagram 返回数据中未包含 items")

        item = items[0]
        user = item.get("user") or {}
        author = user.get("full_name") or user.get("username") or "Instagram 用户"
        username = user.get("username") or ""

        caption_obj = item.get("caption") or {}
        text = caption_obj.get("text", "") or ""

        taken_at = item.get("taken_at")
        timestamp = ""
        if taken_at:
            dt = datetime.fromtimestamp(taken_at, tz=timezone.utc).astimezone(_JST)
            timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")

        media_items = []
        carousel = item.get("carousel_media") or []
        raw_items = carousel if carousel else [item]

        for m in raw_items:
            # 优先提取视频
            video_versions = m.get("video_versions") or []
            if video_versions:
                video_versions.sort(key=lambda x: (x.get("width", 0) * x.get("height", 0)), reverse=True)
                best_video = video_versions[0].get("url")
                if best_video:
                    media_items.append(MediaItem(type="video", url=best_video))
                    continue

            # 其次提取最高分辨率图片
            image_candidates = (m.get("image_versions2") or {}).get("candidates") or []
            if image_candidates:
                image_candidates.sort(key=lambda x: (x.get("width", 0) * x.get("height", 0)), reverse=True)
                best_img = image_candidates[0].get("url")
                if best_img:
                    media_items.append(MediaItem(type="image", url=best_img))

        log.info("[single_fetcher] Instagram API 成功解析 @%s shortcode %s，获得 %d 条媒体",
                 username, shortcode, len(media_items))

        return Post(
            platform="instagram",
            post_id=shortcode,
            author=author,
            text=text,
            media=media_items,
            timestamp=timestamp,
            extra={
                "url": url,
                "username": username,
                "author": author,
                "avatar_url": user.get("profile_pic_url", ""),
            },
        )


    def _parse_tiktok(self, url: str) -> Post:
        # 1. 预处理短链接与追踪参数
        raw_url = url
        if any(x in url for x in ("vt.tiktok.com", "vm.tiktok.com", "v.douyin.com", "/t/")):
            try:
                r = self.session.head(url, allow_redirects=True, timeout=10)
                url = r.url
            except Exception:  # nosec B110
                pass

        m = re.search(r"(?:video|photo|v)/(\d+)", url)
        item_id = m.group(1) if m else "tiktok_post"
        u_match = re.search(r"@([a-zA-Z0-9_.]+)", url)
        username = u_match.group(1) if u_match else ""

        # 构造纯净的标准 URL
        clean_url = f"https://www.tiktok.com/@{username}/video/{item_id}" if (username and item_id != "tiktok_post") else url.split("?")[0]

        # 策略 1：使用专用免登录 TikWM / Web API（支持无水印高清视频、完整图文多图、作者头像与文案）
        try:
            tikwm_api = f"https://www.tikwm.com/api/?url={requests.utils.quote(clean_url or raw_url)}"
            resp = self.session.get(tikwm_api, timeout=15)
            if resp.status_code == 200:
                res_data = resp.json()
                if res_data.get("code") == 0 and res_data.get("data"):
                    d = res_data["data"]
                    author_obj = d.get("author") or {}
                    author_name = author_obj.get("nickname") or author_obj.get("unique_id") or username or "TikTok 用户"
                    handle = author_obj.get("unique_id") or username or ""
                    avatar = author_obj.get("avatar") or ""
                    text = d.get("title") or ""
                    post_id = str(d.get("id") or item_id)

                    media_items = []
                    images = d.get("images") or []
                    if images:
                        for img in images:
                            if img:
                                media_items.append(MediaItem(type="image", url=img))
                    else:
                        play_url = d.get("play") or d.get("wmplay") or d.get("hdplay")
                        if play_url:
                            media_items.append(MediaItem(type="video", url=play_url))
                        elif d.get("cover"):
                            media_items.append(MediaItem(type="image", url=d["cover"]))

                    timestamp = ""
                    create_time = d.get("create_time")
                    if create_time:
                        try:
                            dt = datetime.fromtimestamp(int(create_time), tz=timezone.utc).astimezone(_JST)
                            timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception:  # nosec B110
                            pass

                    if media_items:
                        log.info("[single_fetcher] TikTok 通过专用 API 成功解析 %s，获得 %d 个媒体", post_id, len(media_items))
                        return Post(
                            platform="tiktok",
                            post_id=post_id,
                            author=author_name,
                            text=text,
                            media=media_items,
                            timestamp=timestamp,
                            extra={
                                "url": f"https://www.tiktok.com/@{handle}/video/{post_id}" if handle else clean_url,
                                "username": handle,
                                "author": author_name,
                                "avatar_url": avatar,
                                "source_url": raw_url,
                            },
                        )
        except Exception as ex:
            log.warning("[single_fetcher] TikTok 专用 API 解析失败 (%s)，尝试备用通道: %s", item_id, ex)

        # 策略 2：回退至 yt-dlp（传入纯净链接并附带 HTTP Headers）
        try:
            return self._extract_with_ytdlp(clean_url or raw_url, platform="tiktok", post_id=item_id)
        except Exception as ytdlp_err:
            log.warning("[single_fetcher] TikTok yt-dlp 解析失败: %s", ytdlp_err)

        # 策略 3：TikTok 官方 oEmbed API 兜底（至少提取到作者、文案与封面）
        try:
            oembed_url = f"https://www.tiktok.com/oembed?url={requests.utils.quote(clean_url or raw_url)}"
            oresp = self.session.get(oembed_url, timeout=12)
            if oresp.status_code == 200:
                odata = oresp.json()
                author_name = odata.get("author_name") or username or "TikTok 用户"
                handle = odata.get("author_unique_id") or username or ""
                text = odata.get("title") or ""
                thumb = odata.get("thumbnail_url") or ""
                media_items = [MediaItem(type="image", url=thumb)] if thumb else []
                return Post(
                    platform="tiktok",
                    post_id=item_id,
                    author=author_name,
                    text=text,
                    media=media_items,
                    timestamp="",
                    extra={
                        "url": clean_url,
                        "username": handle,
                        "author": author_name,
                        "avatar_url": "",
                        "source_url": raw_url,
                    },
                )
        except Exception as oembed_err:
            log.warning("[single_fetcher] TikTok oEmbed 解析失败: %s", oembed_err)

        raise RuntimeError("TikTok 链接解析失败（已尝试 API/yt-dlp/oEmbed 全通道）")

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
        if self.proxy:
            ydl_opts["proxy"] = self.proxy

        # Instagram 需要登录态 cookies，自动从 SQLite 数据库提取注入
        if platform == "instagram":
            c_header = ig_session.get_cookie_header()
            if c_header:
                ydl_opts.setdefault("http_headers", {})["Cookie"] = c_header
                log.debug("[single_fetcher] Instagram 已从 SQLite 数据库注入 Cookies Header")
            else:
                log.warning(
                    "[single_fetcher] Instagram 尚未配置登录态 Cookies，"
                    "抓取可能因需要登录而失败。请在后台「社媒监控」页配置账号 Cookies。"
                )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                err_str = str(e)
                if platform == "instagram" and "No video formats found" in err_str:
                    raise RuntimeError("该 Instagram 帖子为纯图片/图集，由于登录态未配置或已失效被登出，无法提取媒体。请在后台重新配置完整 Instagram Cookies。")
                raise RuntimeError(f"解析失败: {e}")

        if not info:
            raise RuntimeError("未能从链接提取到内容信息")

        author = info.get("uploader") or info.get("uploader_id") or ""
        if not author or author.upper() == platform.upper():
            if platform == "instagram":
                m_u = re.search(r"instagram\.com/(?:stories/)?([^/?#\s]+)", url)
                author = f"@{m_u.group(1)}" if m_u else "Instagram 用户"
            elif platform == "tiktok":
                m_u = re.search(r"@([a-zA-Z0-9_.]+)", url)
                author = f"@{m_u.group(1)}" if m_u else "TikTok 用户"
            else:
                author = platform.upper()

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
                vcodec = e.get("vcodec") or ""
                v_url = e.get("url") or ""
                ext = (e.get("ext") or "").lower()
                is_video = (vcodec and vcodec != "none") or ext in ("mp4", "webm", "m4v", "mov") or (".mp4" in v_url.lower())
                if v_url:
                    mtype = "video" if is_video else "image"
                    media_items.append(MediaItem(type=mtype, url=v_url))
                else:
                    thumbnails = e.get("thumbnails") or []
                    if thumbnails:
                        best_th = thumbnails[-1].get("url") or ""
                        if best_th:
                            mtype = "video" if (".mp4" in best_th.lower()) else "image"
                            media_items.append(MediaItem(type=mtype, url=best_th))
        else:
            # 单视频或单图
            v_url = info.get("url") or ""
            vcodec = info.get("vcodec") or ""
            ext = (info.get("ext") or "").lower()
            is_video = (vcodec and vcodec != "none") or ext in ("mp4", "webm", "m4v", "mov") or (".mp4" in v_url.lower())
            if v_url and is_video:
                media_items.append(MediaItem(type="video", url=v_url))
            elif v_url:
                media_items.append(MediaItem(type="image", url=v_url))
            elif info.get("thumbnails"):
                best_th = info["thumbnails"][-1].get("url") or ""
                if best_th:
                    mtype = "video" if (".mp4" in best_th.lower()) else "image"
                    media_items.append(MediaItem(type=mtype, url=best_th))

        log.info("[single_fetcher] %s 解析完成，获得 %d 条媒体", platform, len(media_items))

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
    target_channels: list[str] | None = None,
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
    if not translate:
        post.extra["_skip_translate"] = True
    elif post.text:
        translated_text = forwarder._translate(post.text)
        if translated_text:
            post.extra["_translated"] = translated_text

    # 若指定了通道则定向推，否则走标准 forward_post
    forwarder.forward_post(post, target_channels=target_channels)

    # 归档到 SQLite（若开启）
    if archive:
        try:
            from src.social.archive import get_archive
            get_archive().add_post(post)
        except Exception as e:
            log.warning("[社媒归档] 保存失败: %s", e)

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

