"""
fetchers/tiktok_fetcher.py — TikTok 动态监控（普通视频 / 图文 Post / Story）

**完全免登录**：列表获取按下面顺序自动回退，第一个成功的即采用。

  1. embed 页面（主通道，免登录、免 cookies）
     `https://www.tiktok.com/embed/@<user>` 返回一个内嵌 JSON
     （script#__FRONTITY_CONNECT_STATE__），其中 `videoList` 含最近 10 条作品的
     id / desc / playAddr / coverUrl。TikTok 对该页面不做 WAF 拦截，
     而普通主页 `/@user` 会被拦（返回验证码页），这也是 yt-dlp 的
     `tiktok:user` 直接失败的原因。
  2. api/post/item_list（需要 secUid，通常被 WAF 拦；能通时可额外识别 Story）
  3. yt-dlp `tiktok:user`（同样依赖主页 HTML，作为最后兜底）

发布时间：embed 通道不返回 createTime，但 TikTok 的 item id 高 32 位就是
Unix 时间戳（`id >> 32`），因此可以离线推导出准确发布时间。

单条内容的下载仍走 yt-dlp（免登录可用，能取最高画质 + 原始音轨）；
若 yt-dlp 失败则回退 embed 给出的 playAddr / 图文原图直链。

Story 说明：
  TikTok Story 是有效期 24 小时的特殊 aweme（aweme_type 常见为 107/150），
  只在 item_list / 登录态接口里与普通作品混排；embed 通道不含 Story。
  因此 Story 检测依赖通道 2 可用（或配置 cookies）；检测到时用
  `tiktok_story_` 前缀独立去重，形态判定见 `_classify_aweme()`。
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests

from src.social.fetchers.social_base import SocialFetcher
from src.social.models import Post

log = logging.getLogger("collink")

_JST = timezone(timedelta(hours=9))
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

USER_URL = "https://www.tiktok.com/@{account}"
EMBED_URL = "https://www.tiktok.com/embed/@{account}"
VIDEO_URL = "https://www.tiktok.com/@{account}/video/{item_id}"
PHOTO_URL = "https://www.tiktok.com/@{account}/photo/{item_id}"
ITEM_LIST_API = "https://www.tiktok.com/api/post/item_list/"

# embed 页面内嵌的状态 JSON
_FRONTITY_RE = re.compile(
    r'<script[^>]*id="__FRONTITY_CONNECT_STATE__"[^>]*>(.*?)</script>', re.S)

# TikTok Story 的 aweme_type（观测值，未来可能变动 → 用集合便于扩展）
_STORY_AWEME_TYPES = {107, 150}
# 图文 Post 的 aweme_type
_PHOTO_AWEME_TYPES = {2, 68, 150}


class TikTokFetcher(SocialFetcher):
    platform_name = "tiktok"
    kinds = ("post", "photo", "story")

    def __init__(self, config: dict, store=None, downloader=None):
        super().__init__(config, store, downloader)
        self._session = requests.Session()
        proxy = self.cfg.get("proxy") or config.get("proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or ""
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})
        self._session.headers.update({
            "User-Agent": _UA,
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "ja,en;q=0.8",
        })
        self._sec_uid_cache: dict[str, str] = {}
        # {account: {item_id: playAddr}} —— 每轮 fetch 开始时清空
        self._play_addr_cache: dict[str, dict[str, str]] = {}

    # ── 主流程 ───────────────────────────────────────────

    def _fetch_account(self, account: str) -> list[Post]:
        # playAddr 直链带签名且有有效期，每轮重新获取
        self._play_addr_cache.pop(account, None)
        items = self._list_items(account)
        if not items:
            return []

        cfg = self.cfg
        # 按配置过滤形态
        filtered = []
        for it in items:
            if it["kind"] == "story" and not cfg.get("include_stories", True):
                continue
            if it["kind"] == "photo" and not cfg.get("include_photos", True):
                continue
            filtered.append(it)

        if self._bootstrap_guard(account, [i["post_id"] for i in filtered]):
            return []

        # TikTok embed 接口不稳定：不同轮次返回的内容子集可能不同，
        # 用 bootstrap 时间戳过滤掉首次监控之前的老内容
        filtered = self._filter_before_bootstrap(
            filtered, account, ts_key="timestamp")

        fresh = [i for i in filtered if not self.is_sent(i["post_id"])]
        fresh.sort(key=lambda i: i.get("timestamp") or 0)
        if len(fresh) > self.max_items_per_poll:
            log.info("[tiktok] @%s 新内容 %s 条，本轮先处理最新 %s 条",
                     account, len(fresh), self.max_items_per_poll)
            fresh = fresh[-self.max_items_per_poll:]

        out: list[Post] = []
        for it in fresh:
            if it["kind"] == "story":
                log.info("[tiktok] 📖 @%s 发现 Story %s", account, it["item_id"])
            try:
                out.append(self._build_post(account, it))
            except Exception as e:
                log.warning("[tiktok] 处理 %s 失败: %s", it.get("item_id"), e)
        return out

    def _build_post(self, account: str, item: dict) -> Post:
        item_id = item["item_id"]
        kind = item["kind"]
        post_id = item["post_id"]
        url = item["url"]
        self.mark_seen(post_id, account, kind)

        text = item.get("text") or ""
        ts = item.get("timestamp") or 0
        if not text or not ts:
            # 扁平列表常缺正文/时间 → 完整解析补齐
            info = self._dl.extract_info(url, platform_cfg=self.cfg) or {}
            text = text or info.get("description") or info.get("title") or ""
            ts = ts or info.get("timestamp") or 0

        log.info("[tiktok] ⬇️ 开始下载 %s（%s）", item_id, kind)
        d = self.item_dir(account, item_id, kind)
        files = self._dl.download_via_ytdlp(
            url, d, platform_cfg=self.cfg,
            outtmpl=f"{item_id}_%(playlist_index|1)s.%(ext)s",
        )
        if not files and item.get("image_urls"):
            # yt-dlp 未取到图文素材 → 用 API 给出的原图直链兜底
            log.info("[tiktok] yt-dlp 未取到图文素材，改用原图直链下载")
            tasks = [(u, os.path.join(d, f"{item_id}_{i}.jpg"))
                     for i, u in enumerate(item["image_urls"], 1)]
            files = self._dl.download_many(tasks, referer="https://www.tiktok.com/")
        if not files:
            # yt-dlp 失败 → 用 playAddr 直链兜底（含原始音轨）。
            # 列表若来自 yt-dlp / API 通道则没有 playAddr，这里按需回查 embed 页面，
            # 保证「哪条通道列出的内容」都能拿到直链兜底。
            play_addr = item.get("play_addr") or self._lookup_play_addr(account, item_id)
            if play_addr:
                log.info("[tiktok] yt-dlp 未取到视频，改用 playAddr 直链下载")
                files = self._dl.download_many(
                    [(play_addr, os.path.join(d, f"{item_id}.mp4"))],
                    referer="https://www.tiktok.com/")

        if not files:
            # TikTok 内容必然带媒体 —— 一个都没下到就不要发出「媒体数量：0」的消息，
            # 抛异常让本条跳过（只标记 seen 未标记 sent），下轮自动重试。
            raise RuntimeError(
                f"{item_id} 媒体下载全部失败（yt-dlp 与直链兜底均未成功），"
                f"本条稍后重试")

        # TikTok 的 MP4 有时是 HEVC；下载完成后统一转成手机浏览器兼容的
        # H.264/AAC。此操作只会处理实际检测为 HEVC 的视频，图片不受影响。
        files = self._dl.ensure_mobile_video_compatibility(files)

        return Post(
            platform="tiktok",
            post_id=post_id,
            author=self.display_name(account),
            text=str(text).strip(),
            media=self.build_media_items(files),
            timestamp=(datetime.fromtimestamp(float(ts), tz=_JST)
                       .strftime("%Y-%m-%d %H:%M:%S JST") if ts
                       else time.strftime("%Y-%m-%d %H:%M:%S")),
            extra={"account": account, "kind": kind, "url": url,
                   "item_id": item_id},
        )

    # ── 列表获取 ─────────────────────────────────────────

    def _list_items(self, account: str) -> list[dict]:
        """统一的内容列表：[{item_id, post_id, kind, url, text, timestamp}]

        三通道自动回退，全部免登录；只有 Story 需要通道 2 可用。
        """
        # 通道 1：embed 页面（免登录，最稳定）
        try:
            items = self._embed_items(account)
            if items:
                log.debug("[tiktok] embed 通道取得 @%s 的 %s 条内容",
                          account, len(items))
                # embed 不含 Story —— 若配置要求 Story，再顺带尝试 API 通道补充
                if self.cfg.get("include_stories", True):
                    items = self._merge_stories(account, items)
                return items
        except Exception as e:
            log.debug("[tiktok] embed 通道失败: %s",
                      str(e).replace("\n", " ")[:160])

        # 通道 2：Web API（能拿到 aweme_type，可靠区分 Story / 图文）
        try:
            api_items = self._api_items(account)
            if api_items:
                log.debug("[tiktok] item_list API 取得 @%s 的 %s 条内容",
                          account, len(api_items))
                return api_items
        except Exception as e:
            log.debug("[tiktok] item_list API 失败: %s",
                      str(e).replace("\n", " ")[:160])

        # 通道 3：yt-dlp 扁平列表（无 aweme_type，按 URL 判定形态）
        info = self._dl.extract_info(
            USER_URL.format(account=account),
            platform_cfg=self.cfg,
            extra_opts={"extract_flat": "in_playlist",
                        "playlistend": self.max_items_per_poll * 3},
        )
        out: list[dict] = []
        for e in ((info or {}).get("entries") or []):
            if not isinstance(e, dict) or not e.get("id"):
                continue
            item_id = str(e["id"])
            # 只推送本人发布的内容（过滤转发）
            author = str(e.get("uploader") or e.get("uploader_id")
                         or e.get("channel") or "").lstrip("@").lower()
            if author and author != account.lower():
                log.debug("[tiktok] 跳过转发内容 %s（作者 @%s）", item_id, author)
                continue
            url = e.get("url") or e.get("webpage_url") or VIDEO_URL.format(
                account=account, item_id=item_id)
            kind = "photo" if "/photo/" in url else "post"
            out.append({
                "item_id": item_id,
                "post_id": f"tiktok_{item_id}",
                "kind": kind,
                "url": url,
                "text": e.get("description") or e.get("title") or "",
                "timestamp": e.get("timestamp") or 0,
                "image_urls": [],
            })
        if out:
            log.debug("[tiktok] yt-dlp 列出 @%s 的 %s 条内容（Story 需 API 通道识别）",
                      account, len(out))
        return out

    def _lookup_play_addr(self, account: str, item_id: str) -> str:
        """按需从 embed 页面回查某条内容的 playAddr 直链（结果缓存到本轮）。"""
        cache = self._play_addr_cache.get(account)
        if cache is None:
            cache = {}
            try:
                for it in self._embed_items(account):
                    if it.get("play_addr"):
                        cache[it["item_id"]] = it["play_addr"]
            except Exception as e:
                log.debug("[tiktok] 回查 playAddr 失败: %s",
                          str(e).replace("\n", " ")[:120])
            self._play_addr_cache[account] = cache
        return cache.get(item_id, "")

    def _embed_items(self, account: str) -> list[dict]:
        """通道 1：解析 embed 页面内嵌 JSON（免登录、无需 cookies）。"""
        r = self._session.get(EMBED_URL.format(account=account),
                              timeout=self._dl.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"embed HTTP {r.status_code}")
        m = _FRONTITY_RE.search(r.text)
        if not m:
            raise RuntimeError("embed 页面未找到 __FRONTITY_CONNECT_STATE__")
        data = json.loads(m.group(1))

        source = ((data.get("source") or {}).get("data") or {})
        node = source.get(f"/embed/@{account}")
        if not isinstance(node, dict):
            # 页面路径 key 可能带大小写差异 → 退化为找第一个含 videoList 的节点
            node = next((v for v in source.values()
                         if isinstance(v, dict) and v.get("videoList")), {})
        video_list = node.get("videoList") or []
        if not video_list:
            raise RuntimeError("embed 页面 videoList 为空")

        # embed 只给 uniqueId/nickname，正好可以校验账号拼写
        info = node.get("userInfo") or {}
        if info.get("privateAccount"):
            log.warning("[tiktok] @%s 是私密账号，免登录无法获取内容", account)

        out: list[dict] = []
        for v in video_list:
            item_id = str(v.get("id") or "")
            if not item_id:
                continue
            # 只推送本人发布的内容：作者不是被监控账号 → 跳过（转发的视频）
            author = str(v.get("authorUniqueId") or "").lstrip("@").lower()
            if author and author != account.lower():
                log.debug("[tiktok] 跳过转发内容 %s（作者 @%s）", item_id, author)
                continue
            play_addr = v.get("playAddr") or ""
            # 无 playAddr 基本就是图文 Post
            kind = "post" if play_addr else "photo"
            url = (VIDEO_URL if kind == "post" else PHOTO_URL).format(
                account=account, item_id=item_id)
            out.append({
                "item_id": item_id,
                "post_id": f"tiktok_{item_id}",
                "kind": kind,
                "url": url,
                "text": v.get("desc") or "",
                "timestamp": _ts_from_item_id(item_id),
                "image_urls": [],
                # 直链兜底：yt-dlp 失败时用它下载
                "play_addr": play_addr,
                "cover_url": v.get("originCoverUrl") or v.get("coverUrl") or "",
            })
        return out

    def _merge_stories(self, account: str, items: list[dict]) -> list[dict]:
        """embed 结果之上，尽力补充 Story（依赖 item_list 通道可用）。"""
        try:
            api_items = self._api_items(account)
        except Exception as e:
            log.debug("[tiktok] Story 补充失败（item_list 不可用）: %s",
                      str(e).replace("\n", " ")[:120])
            return items
        stories = [i for i in api_items if i["kind"] == "story"]
        if stories:
            log.info("[tiktok] 📖 @%s 通过 API 通道补充 %s 条 Story",
                     account, len(stories))
            known = {i["post_id"] for i in items}
            items = items + [s for s in stories if s["post_id"] not in known]
        return items

    def _api_items(self, account: str) -> list[dict]:
        """通过 api/post/item_list 拉取作品列表（含 aweme_type 与图文原图）。"""
        sec_uid = self._resolve_sec_uid(account)
        if not sec_uid:
            return []
        params = {
            "aid": "1988",
            "app_language": "ja",
            "app_name": "tiktok_web",
            "browser_language": "ja",
            "browser_platform": "Win32",
            "channel": "tiktok_web",
            "count": str(max(20, self.max_items_per_poll * 3)),
            "cursor": "0",
            "device_platform": "web_pc",
            "secUid": sec_uid,
        }
        r = self._session.get(ITEM_LIST_API, params=params,
                              timeout=self._dl.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        body = r.json() if r.content else {}
        awemes = body.get("itemList") or []
        out: list[dict] = []
        for a in awemes:
            item_id = str(a.get("id") or "")
            if not item_id:
                continue
            # 只推送本人发布的内容（过滤转发）
            author = str(((a.get("author") or {}).get("uniqueId"))
                         or a.get("authorUniqueId") or "").lstrip("@").lower()
            if author and author != account.lower():
                log.debug("[tiktok] 跳过转发内容 %s（作者 @%s）", item_id, author)
                continue
            kind, image_urls = self._classify_aweme(a)
            url = (PHOTO_URL if kind in ("photo",) else VIDEO_URL).format(
                account=account, item_id=item_id)
            prefix = "tiktok_story_" if kind == "story" else "tiktok_"
            out.append({
                "item_id": item_id,
                "post_id": f"{prefix}{item_id}",
                "kind": kind,
                "url": url,
                "text": a.get("desc") or "",
                "timestamp": int(a.get("createTime") or 0),
                "image_urls": image_urls,
            })
        return out

    @staticmethod
    def _classify_aweme(a: dict) -> tuple[str, list[str]]:
        """判定内容形态并提取图文原图直链。

        返回 ("post" | "photo" | "story", [原图 URL...])
        """
        atype = int(a.get("aweme_type") or a.get("awemeType") or 0)
        image_post = a.get("imagePost") or a.get("image_post_info") or {}
        images = image_post.get("images") or []
        image_urls: list[str] = []
        for img in images:
            # displayImage.urlList[0] 即原始尺寸图
            disp = img.get("displayImage") or img.get("display_image") or {}
            urls = disp.get("urlList") or disp.get("url_list") or []
            if urls:
                image_urls.append(urls[0])

        if atype in _STORY_AWEME_TYPES and not image_urls:
            return "story", []
        if a.get("isStory") or a.get("is_story"):
            return "story", image_urls
        if image_urls or atype in _PHOTO_AWEME_TYPES:
            return "photo", image_urls
        return "post", []

    def _resolve_sec_uid(self, account: str) -> str:
        """从用户主页 HTML 解析 secUid（item_list API 必需）。"""
        if account in self._sec_uid_cache:
            return self._sec_uid_cache[account]
        import re
        try:
            r = self._session.get(USER_URL.format(account=account),
                                  timeout=self._dl.timeout)
            if r.status_code != 200:
                return ""
            for pattern in (r'"secUid"\s*:\s*"([\w\-=]+)"',
                            r'"secUid":"([^"]+)"',
                            r'secUid\\":\\"([^\\"]+)'):
                m = re.search(pattern, r.text)
                if m:
                    self._sec_uid_cache[account] = m.group(1)
                    return m.group(1)
        except Exception as e:
            log.debug("[tiktok] 解析 secUid 失败 @%s: %s", account, e)
        return ""


def _ts_from_item_id(item_id: str) -> int:
    """从 TikTok item id 推导发布时间。

    TikTok 的 aweme id 是雪花式 ID，高 32 位即 Unix 时间戳，
    因此 embed 通道即使不返回 createTime，也能离线得到准确发布时间。
    """
    try:
        ts = int(item_id) >> 32
        # 合理区间校验：2016-01-01 ~ 2100-01-01
        if 1451606400 < ts < 4102444800:
            return ts
    except (TypeError, ValueError):
        pass
    return 0
