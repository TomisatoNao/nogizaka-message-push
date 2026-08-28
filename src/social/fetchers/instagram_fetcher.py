"""
fetchers/instagram_fetcher.py — Instagram 监控

支持内容：
  * Feed 帖子（单图 / 多图 Carousel / Reel）
  * Story（图片 / 视频）—— Story 更新同步推送

数据来源（三后端，自动回退）：
  1. yt-dlp（主）—— `instagram:user` / `instagram:story` extractor
  2. web_profile_info（备）—— Instagram Web API；请求前先访问首页拿
     csrftoken / mid cookie 再带上 X-IG-App-ID，可提高匿名成功率
  3. 帖子 embed 页面（末）—— 单帖 `/p/<code>/embed/captioned/`，
     用于在列表已知时补全正文与图片

关于「免登录」的实测结论（2026-07）：
  Instagram 已对**匿名**的 `web_profile_info` / `feed/user` 接口统一返回
  429「Please wait a few minutes」，主页 HTML 也不再内嵌帖子数据；
  yt-dlp 的 `instagram:story` 会直接提示需要登录。也就是说
  **Instagram 侧强制要求会话态**，这不是本项目能绕过的实现问题。

  因此这里提供一条「不需要在本程序里登录」的可行路径：
      platforms.instagram.cookies_from_browser = "chrome"   （或 edge / firefox）
  程序会直接复用你浏览器里已有的 Instagram 登录态（不接触账号密码、
  不产生新的登录行为）。X 与 TikTok 则完全免登录，无需任何 cookies。

多图 Carousel 由 yt-dlp 返回成 playlist，下载后目录里会出现多个文件，
downloader 通过「扫描目录差集」把它们全部收集为 MediaItem。
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests

from src.social.fetchers.social_base import SocialFetcher
from src.social.models import Post

log = logging.getLogger("collink")

_JST = timezone(timedelta(hours=9))
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

USER_URL = "https://www.instagram.com/{account}/"
STORY_URL = "https://www.instagram.com/stories/{account}/"
POST_URL = "https://www.instagram.com/p/{shortcode}/"
WEB_PROFILE = ("https://www.instagram.com/api/v1/users/web_profile_info/"
               "?username={account}")
# 公开的 Web App ID（Instagram 前端自身使用的常量）
_IG_APP_ID = "936619743392459"


class InstagramSessionRejected(RuntimeError):
    """A logged-in Instagram endpoint explicitly rejected the session."""


class InstagramFetcher(SocialFetcher):
    platform_name = "instagram"
    kinds = ("post", "carousel", "reel", "story")
    _last_blocked_log: float = 0.0

    def __init__(self, config: dict, store=None, downloader=None,
                 on_session_lost=None):
        super().__init__(config, store, downloader)
        # 登录态失效时的告警回调（由 SyncManager 注入，用于推送 QQ）
        self._on_session_lost = on_session_lost
        self._session = requests.Session()
        proxy = self.cfg.get("proxy") or config.get("proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or ""
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})
        self._session.headers.update({
            "User-Agent": _UA,
            "X-IG-App-ID": _IG_APP_ID,
            "Accept": "*/*",
            "Accept-Language": "ja,en;q=0.8",
        })
        self._warmed = False
        self._login_hint_shown = False
        self._last_blocked_log = 0.0
        # 每个账号单独维护 Story 限频状态；不能让先轮询的账号压住其它账号。
        self._story_checked: dict[str, float] = {}
        self._story_next_gap: dict[str, int] = {}
        # 账号名 → 数字 ID（Story 接口需要，解析一次即缓存）
        self._uid_cache: dict[str, str] = {}

    # ── 会话准备 ─────────────────────────────────────────

    def _warm_session(self) -> None:
        """先访问首页取 csrftoken / mid cookie，提高匿名接口成功率。

        若配置了 cookies_from_browser / cookies_file，则同时把浏览器里已有的
        Instagram 登录态注入本 session —— 不需要在本程序里做任何登录动作。
        """
        if self._warmed:
            return
        self._warmed = True
        cfg = self.cfg

        # UA 应与建立该会话的浏览器一致 —— 同一个 sessionid 换客户端指纹
        # 是明显的风险信号，配置里填了就用配置的
        ua = (cfg.get("user_agent") or "").strip()
        if ua:
            self._session.headers["User-Agent"] = ua

        # 1) 复用已有登录态：cookies_file 优先，其次默认 data/instagram_cookies.txt 与环境变量，其次浏览器
        cookies: dict = {}
        cfile = (cfg.get("cookies_file") or "").strip()
        from src.social import ig_session
        if not cfile:
            cookies = ig_session.read_cookie_file()
        else:
            cookies = ig_session.read_cookie_file(cfile)
        if cookies:
            log.info("[instagram] 已加载登录态 cookies（%s 个）", len(cookies))
        if not cookies:
            browser = (cfg.get("cookies_from_browser") or "").strip()
            if browser:
                cookies = _cookies_from_browser(browser)
                if cookies:
                    log.info("[instagram] 已复用 %s 浏览器中的登录态（%s 个 cookie）",
                             browser, len(cookies))

        if cookies:
            self._session.cookies.update(cookies)
            # csrftoken 必须取自**同一套** cookies；若用预热请求拿到的匿名
            # token，会和 sessionid 对不上，导致接口直接拒绝
            if cookies.get("csrftoken"):
                self._session.headers["X-CSRFToken"] = cookies["csrftoken"]
            return      # 已有登录态，无需再做匿名预热
        try:
            self._session.get("https://www.instagram.com/", timeout=self._dl.timeout)
            self._warmed = True
        except Exception as e:
            log.debug("[instagram] 预热 session 失败（不影响主流程）: %s", e)

    def _session_failed(self, reason: str) -> None:
        """标记登录态失效并（首次）告警。

        cookies 的失效时间不可预测（浏览器退出登录、改密码、IG 判定异常都会
        立刻作废），所以不能靠猜 —— 只能在真的用不了时立刻发现并通知。
        """
        from src.social import ig_session
        if not (self._session.cookies.get("sessionid")
                or self.cfg.get("cookies_file")
                or self.cfg.get("cookies_from_browser")):
            return          # 本来就没配 cookies，不算「失效」
        if not ig_session.mark_invalid(reason):
            return          # 之前已经标记过，不重复告警
        msg = ("【collink 提醒】Instagram 登录态已失效\n"
               f"原因：{reason}\n"
               "影响：Instagram 的 Feed 与 Story 已停止抓取（其它平台不受影响）\n"
               "处理：在后台「Instagram 登录态」页重新粘贴 cookies 即可")
        log.warning("[instagram] ⚠️ 登录态失效：%s", reason)
        if self._on_session_lost:
            try:
                self._on_session_lost(msg)
            except Exception as e:
                log.debug("[instagram] 登录态失效告警发送失败: %s", e)

    def _session_ok(self) -> None:
        from src.social import ig_session
        try:
            ig_session.mark_valid()
        except Exception:
            pass

    # ── 登录态引导（仅触发一次）─────────────────────────────

    def _hint_login_once(self) -> None:
        if self._login_hint_shown:
            return
        self._login_hint_shown = True
        log.warning(
            "[instagram] ⚠️ 当前未配置有效登录态，只能抓取公开账号的基本 Feed，"
            "且无法抓取 Story。\n"
            "建议在具有 Instagram 正常访问权限的环境中配置登录态（任选其一）：\n"
            "  ① 安装浏览器扩展（如 Get cookies.txt LOCALLY），导出 "
            "instagram.com 的 cookies.txt，填到 config.json → "
            'platforms.instagram.cookies_file；\n'
            '  ② 或设置 "cookies_from_browser": "chrome"（需完全关闭该浏览器；'
            "较新版 Chrome/Edge 启用了 App-Bound 加密，可能读取失败）。\n"
            "两种方式都只是复用你浏览器里已有的登录态，程序不接触账号密码。"
            "X 与 TikTok 完全免登录，不受此限制。")

    # ── 主流程 ───────────────────────────────────────────

    def _fetch_account(self, account: str) -> list[Post]:
        """抓取单个账号。所有网络行为都先经过风控闸门。"""
        from src.social.ig_safety import Blocked, get_guard
        posts: list[Post] = []
        cfg = self.cfg
        guard = get_guard()

        try:
            guard.peek_blocked(self._config)
        except Blocked as e:
            now = time.time()
            if now - self._last_blocked_log > 60:
                self._last_blocked_log = now
                log.info("[instagram] ⏸ %s", e)
            else:
                log.debug("[instagram] ⏸ %s", e)
            return posts

        self._warm_session()
        if cfg.get("include_feed", True):
            try:
                posts.extend(self._fetch_feed(account))
                guard.record_ok()
            except Blocked as e:
                log.info("[instagram] ⏸ %s", e)
                return posts
            except Exception as e:
                self._note_risk(e)
                log.warning("[instagram] @%s Feed 检查失败: %s", account,
                            str(e).replace("\n", " ")[:200])

        # Story 是强登录态接口，审查更严 —— 单独用更低的频率
        if cfg.get("include_stories", True) and self._story_due(account):
            try:
                posts.extend(self._fetch_stories(account))
                guard.record_ok()
                self._story_checked[account] = time.time()
            except Blocked as e:
                log.info("[instagram] ⏸ Story：%s", e)
            except Exception as e:
                self._note_risk(e)
                log.warning("[instagram] @%s Story 检查失败: %s", account,
                            str(e).replace("\n", " ")[:200])
        return posts

    def _story_due(self, account: str) -> bool:
        """该账号的 Story 是否到检查时间。

        每个账号独立抽取并维持等待时长；轮询是串行的，若共享最近检查时间，
        排在前面的账号会让后续账号永远不再检查 Story。
        """
        last = self._story_checked.get(account, 0)
        if not last:
            return True
        gap = self._story_next_gap.get(account)
        if gap is None:
            gap = self._roll_story_gap()
            self._story_next_gap[account] = gap
        if (time.time() - last) >= gap:
            self._story_next_gap[account] = self._roll_story_gap()
            return True
        return False

    def _roll_story_gap(self) -> int:
        import random
        rng = self.cfg.get("story_interval_range_seconds")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            try:
                lo, hi = int(rng[0]), int(rng[1])
                if lo > hi:
                    lo, hi = hi, lo
                if lo > 0:
                    return random.randint(lo, hi)
            except (TypeError, ValueError):
                pass
        return int(self.cfg.get("story_interval_seconds", 3600) or 3600)

    def _note_risk(self, e: Exception) -> None:
        """把 401/403/429 记为风控信号，累计到阈值就熔断。

        ⚠️ 关键区分：
          401 / 403 → **登录态真的失效**，需要提示重新导出 cookies
          429       → 只是**限流**，会话可能完全正常。实测某些接口
                      （如 web_profile_info）对本机 IP 恒返回 429，
                      拿它判定失效会造成「一上来就说登录态失效」的误报。
        """
        from src.social.ig_safety import get_guard
        msg = str(e)
        for code in (401, 403):
            if str(code) in msg:
                get_guard().record_failure(self._config, code)
                self._session_failed(f"接口返回 HTTP {code}，登录态已失效")
                return
        if "429" in msg:
            # 只计入熔断（降低访问频率），不判定登录态失效
            if get_guard().record_failure(self._config, 429):
                log.warning("[instagram] 连续限流已触发熔断 —— "
                            "这是频率问题，登录态未必失效")
            return

    # ── Feed ─────────────────────────────────────────────

    def _fetch_feed(self, account: str) -> list[Post]:
        entries = self._list_feed_entries(account)
        if not entries:
            return []

        ids = [f"instagram_{e['id']}" for e in entries]
        if self._bootstrap_guard(account, ids, kind="feed"):
            return []

        fresh = [e for e in entries
                 if not self.is_sent(f"instagram_{e['id']}")]
        fresh.sort(key=lambda e: e.get("timestamp") or 0)
        if len(fresh) > self.max_items_per_poll:
            log.info("[instagram] @%s Feed 新内容 %s 条，本轮先处理最新 %s 条",
                     account, len(fresh), self.max_items_per_poll)
            fresh = fresh[-self.max_items_per_poll:]

        out: list[Post] = []
        for e in fresh:
            try:
                p = self._build_feed_post(account, e)
                if p is not None:
                    out.append(p)
            except Exception as ex:
                log.warning("[instagram] 处理帖子 %s 失败: %s", e.get("id"), ex)
        return out

    def _api_feed_entries(self, account: str) -> list[dict]:
        """带登录态的 Feed 接口（实测唯一稳定可用的一条）。

        `i.instagram.com/api/v1/feed/user/<name>/username/` 在有会话时返回
        完整的 items 列表（含 shortcode / 时间戳 / 正文 / 轮播 / 原图与视频直链）。

        为什么不用其它路径（都实测过）：
          * yt-dlp 的 instagram:user → "Unable to extract data"，已失效
          * web_profile_info → 对本机 IP **恒返回 429**，与登录态无关
        直接拿到媒体直链还有个额外好处：不必再让 yt-dlp 逐帖解析，请求数大幅减少。
        """
        from src.social.ig_safety import get_guard
        get_guard().check(self._config, what="feed API")
        url = (f"https://i.instagram.com/api/v1/feed/user/"
               f"{account}/username/")
        r = self._session.get(url, timeout=self._dl.timeout)
        if r.status_code in (401, 403):
            self._session_failed(f"Feed 接口返回 HTTP {r.status_code}")
            raise RuntimeError(f"feed/username HTTP {r.status_code}（登录态被拒）")
        if r.status_code != 200:
            raise RuntimeError(f"feed/username HTTP {r.status_code}")
        self._session_ok()
        try:
            data = r.json()
        except ValueError as e:
            raise RuntimeError(f"feed/username 返回非 JSON: {e}") from e

        out: list[dict] = []
        for m in (data.get("items") or []):
            code = m.get("code")
            if not code:
                continue
            mt = int(m.get("media_type") or 1)
            kind = ("carousel" if mt == 8 else
                    "reel" if mt == 2 else "post")
            out.append({
                "id": code,
                "url": POST_URL.format(shortcode=code),
                "timestamp": int(m.get("taken_at") or 0),
                "title": ((m.get("caption") or {}).get("text") or ""),
                "kind": kind,
                "media": _extract_media(m),
            })
        log.debug("[instagram] Feed 接口取得 @%s 的 %s 条帖子", account, len(out))
        return out

    def _resolve_uid(self, account: str) -> str:
        """账号名 → 数字 ID（Story 接口需要）。结果缓存，避免重复请求。"""
        if account in self._uid_cache:
            return self._uid_cache[account]
        from src.social.ig_safety import get_guard
        get_guard().check(self._config, what="uid 解析")
        r = self._session.get(
            f"https://i.instagram.com/api/v1/feed/user/{account}/username/",
            timeout=self._dl.timeout)
        if r.status_code in (401, 403):
            reason = f"Story account lookup returned HTTP {r.status_code}"
            self._session_failed(reason)
            raise InstagramSessionRejected(reason)
        if r.status_code != 200:
            raise RuntimeError(f"Story account lookup returned HTTP {r.status_code}")
        uid = ""
        try:
            d = r.json()
            uid = str((d.get("user") or {}).get("pk")
                      or (d.get("user") or {}).get("id") or "")
            if not uid and d.get("items"):
                uid = str(((d["items"][0].get("user") or {}).get("pk")) or "")
        except ValueError as e:
            raise RuntimeError("Story account lookup returned invalid JSON") from e
        if uid:
            self._uid_cache[account] = uid
        return uid

    def _api_story_entries(self, account: str) -> list[dict]:
        """带登录态的 Story 接口。

        `feed/reels_media/?reel_ids=<uid>` 返回该账号当前全部 Story
        （含原图 / 视频直链与过期时间）。

        为什么不用 yt-dlp：实测它的 `instagram:story` extractor 对本账号
        **恒返回 0 条**，即使当时确实有 Story —— 已经失效了。
        """
        from src.social.ig_safety import get_guard
        uid = self._resolve_uid(account)
        if not uid:
            raise RuntimeError("无法解析账号数字 ID")

        get_guard().check(self._config, what="story API")
        r = self._session.get(
            "https://i.instagram.com/api/v1/feed/reels_media/",
            params={"reel_ids": uid}, timeout=self._dl.timeout)
        if r.status_code in (401, 403):
            reason = f"Story API returned HTTP {r.status_code}"
            self._session_failed(reason)
            raise InstagramSessionRejected(reason)
        if r.status_code != 200:
            raise RuntimeError(f"reels_media HTTP {r.status_code}")
        self._session_ok()
        try:
            data = r.json()
        except ValueError as e:
            raise RuntimeError(f"reels_media 返回非 JSON: {e}") from e

        reels = data.get("reels") or {}
        reel = reels.get(uid) or (list(reels.values())[0] if reels else {})
        out: list[dict] = []
        for it in (reel.get("items") or []):
            sid = str(it.get("pk") or it.get("id") or "")
            if not sid:
                continue
            out.append({
                "id": sid,
                "url": STORY_URL.format(account=account),
                "timestamp": int(it.get("taken_at") or 0),
                "title": "",
                "kind": "story",
                "media": _extract_media(it),
                "expiring_at": reel.get("expiring_at", 0),
            })
        log.debug("[instagram] Story 接口取得 @%s 的 %s 条", account, len(out))
        return out

    def _list_feed_entries(self, account: str) -> list[dict]:
        """返回 [{id, url, timestamp, title, kind}]（多后端自动回退）。"""
        # 后端 0：带登录态的 Feed 接口 —— 有 cookies 时最可靠
        if self._session.cookies.get("sessionid") or self.cfg.get("cookies_file") or self.cfg.get("cookies_from_browser"):
            try:
                got = self._api_feed_entries(account)
                if got:
                    return got
            except Exception as e:
                log.debug("[instagram] Feed 接口失败，回退其它后端: %s",
                          str(e).replace("\n", " ")[:160])

        # 后端 1：yt-dlp 扁平列出用户主页
        info = self._dl.extract_info(
            USER_URL.format(account=account),
            platform_cfg=self.cfg,
            extra_opts={"extract_flat": "in_playlist",
                        "playlistend": self.max_items_per_poll * 3},
        )
        entries = []
        if info and info.get("entries"):
            for e in info["entries"]:
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                entries.append({
                    "id": str(e["id"]),
                    "url": e.get("url") or e.get("webpage_url")
                           or POST_URL.format(shortcode=e["id"]),
                    "timestamp": e.get("timestamp") or 0,
                    "title": e.get("title") or e.get("description") or "",
                    "kind": "reel" if "/reel" in str(e.get("url") or "") else "post",
                })
        if entries:
            log.debug("[instagram] yt-dlp 列出 @%s 的 %s 条帖子", account, len(entries))
            return entries

        # 后端 2：web_profile_info
        log.debug("[instagram] yt-dlp 未列出内容，回退 web_profile_info")
        return self._web_profile_entries(account)

    def _web_profile_entries(self, account: str) -> list[dict]:
        from src.social.ig_safety import get_guard
        self._warm_session()
        get_guard().check(self._config, what="web_profile_info")
        hdrs = {"Referer": USER_URL.format(account=account)}
        r = self._session.get(WEB_PROFILE.format(account=account),
                              headers=hdrs, timeout=self._dl.timeout)
        if r.status_code in (401, 403):
            self._hint_login_once()
            self._session_failed(f"web_profile_info 返回 HTTP {r.status_code}")
            raise RuntimeError(f"web_profile_info HTTP {r.status_code}（登录态被拒）")
        if r.status_code == 429:
            # 该接口对部分 IP 恒 429，与登录态无关 —— 不判失效，交给上层换后端
            raise RuntimeError("web_profile_info HTTP 429（接口限流，非登录态问题）")
        if r.status_code == 200:
            self._session_ok()
        if r.status_code != 200:
            raise RuntimeError(f"web_profile_info HTTP {r.status_code}")
        data = r.json()
        edges = (((data.get("data") or {}).get("user") or {})
                 .get("edge_owner_to_timeline_media") or {}).get("edges") or []
        out = []
        for edge in edges:
            node = edge.get("node") or {}
            code = node.get("shortcode")
            if not code:
                continue
            caption = ""
            cap_edges = ((node.get("edge_media_to_caption") or {}).get("edges") or [])
            if cap_edges:
                caption = (cap_edges[0].get("node") or {}).get("text", "")
            typename = node.get("__typename", "")
            kind = ("carousel" if typename == "GraphSidecar"
                    else "reel" if node.get("is_video") else "post")
            out.append({
                "id": code,
                "url": POST_URL.format(shortcode=code),
                "timestamp": node.get("taken_at_timestamp") or 0,
                "title": caption,
                "kind": kind,
            })
        log.debug("[instagram] web_profile_info 列出 @%s 的 %s 条帖子",
                  account, len(out))
        return out

    def _build_feed_post(self, account: str, entry: dict) -> Post | None:
        item_id = entry["id"]
        post_id = f"instagram_{item_id}"
        url = entry["url"]
        kind = entry.get("kind") or "post"
        self.mark_seen(post_id, account, kind)

        # 完整解析拿正文与准确时间戳（扁平列表里往往缺失）
        info = self._dl.extract_info(url, platform_cfg=self.cfg) or {}

        # 只推送本人发布的内容：作者可识别且不是被监控账号 → 跳过（转发/合拍等）
        uploader = str(info.get("uploader_id") or info.get("channel")
                       or "").lstrip("@").lower()
        if uploader and uploader != account.lower():
            log.info("[instagram] 跳过非本人内容 %s（作者 @%s）", item_id, uploader)
            self._store.mark_sent("instagram", post_id, account, kind)
            return None

        text = (info.get("description") or info.get("title")
                or entry.get("title") or "")
        ts = info.get("timestamp") or entry.get("timestamp") or 0
        if info.get("_type") == "playlist" and (info.get("playlist_count") or 0) > 1:
            kind = "carousel"

        log.info("[instagram] ⬇️ 开始下载 %s（%s）", item_id, kind)
        d = self.item_dir(account, item_id, kind)

        # Feed 接口已经给出媒体直链 → 直接下载，不必让 yt-dlp 再逐帖解析
        # （少一轮请求，对风控更友好，也避开了 yt-dlp 对 IG 已失效的问题）
        files = []
        direct = entry.get("media") or []
        if direct:
            tasks = []
            for i, m in enumerate(direct, 1):
                ext = ".mp4" if m["type"] == "video" else ".jpg"
                tasks.append((m["url"], os.path.join(d, f"{item_id}_{i}{ext}")))
            files = self._dl.download_many(
                tasks, referer="https://www.instagram.com/")
            if files:
                log.info("[instagram] 直链下载 %s/%s 个媒体", len(files), len(tasks))

        if not files:
            files = self._dl.download_via_ytdlp(
                url, d, platform_cfg=self.cfg,
                outtmpl=f"{item_id}_%(playlist_index|1)s.%(ext)s",
            )
        if not files:
            # Instagram 帖子必然带媒体 —— 一个都没下到就跳过本条（未标记 sent），
            # 下轮自动重试，避免推送出「媒体数量：0」的空消息
            self._hint_login_once()
            raise RuntimeError(f"{item_id} 未下载到任何媒体（通常是缺少登录态），本条稍后重试")

        return Post(
            platform="instagram",
            post_id=post_id,
            author=self.display_name(account),
            text=str(text).strip(),
            media=self.build_media_items(files),
            timestamp=(datetime.fromtimestamp(float(ts), tz=_JST)
                       .strftime("%Y-%m-%d %H:%M:%S JST") if ts else ""),
            extra={"account": account, "kind": kind, "url": url,
                   "item_id": item_id},
        )

    def _fetch_posts(self, account: str) -> list[Post]:
        """[兼容] scripts/integration_test.py 使用的旧方法名，等价于 _fetch_feed()。"""
        self._warm_session()
        return self._fetch_feed(account)

    # ── Story ────────────────────────────────────────────

    def _fetch_stories(self, account: str) -> list[Post]:
        """Story 24 小时过期，因此每轮都全量列出、靠 SQLite 去重。

        优先走登录态 API（yt-dlp 的 story extractor 已失效，实测恒返回 0 条）。
        """
        self._warm_session()   # 幂等；直接调用本方法时也能带上登录态
        entries: list[dict] = []
        has_session = bool(self._session.cookies.get("sessionid")
                           or self.cfg.get("cookies_file")
                           or self.cfg.get("cookies_from_browser"))
        if has_session:
            try:
                entries = self._api_story_entries(account)
            except InstagramSessionRejected as e:
                log.warning(
                    "[instagram] @%s Story 未检查：登录态已失效或被拒绝（%s）。"
                    "请在后台重新导出 cookies。", account, e)
                return []
            except Exception as e:
                log.warning(
                    "[instagram] @%s Story 检查失败：暂时无法判断是否有 Story，"
                    "将按计划重试（%s）", account,
                    str(e).replace("\n", " ")[:160])
                return []
        else:
            self._hint_login_once()
            log.warning("[instagram] @%s Story 未检查：未配置登录态。", account)
            return []
        if not entries:
            log.debug("[instagram] @%s 当前没有 Story（登录态有效，Story API 返回空列表）",
                      account)
            return []

        log.info("[instagram] 📖 @%s 发现 %s 条 Story", account, len(entries))
        ids = [f"instagram_story_{e['id']}" for e in entries]
        # ⚠️ Story **不做** first_run_skip。
        # bootstrap 的做法是「只标记已发送、不下载」，对 Feed 没问题（帖子长期存在，
        # 以后随时能补），但 Story **24 小时就过期**——跳过一次就等于永久丢失，
        # 而这恰恰是监控 Story 的全部意义。当前在线的 Story 至多几条，不会刷屏。
        if self.cfg.get("story_first_run_skip", False):
            if self._bootstrap_guard(account, ids, kind="story"):
                return []

        out: list[Post] = []
        for e in entries:
            item_id = str(e["id"])
            post_id = f"instagram_story_{item_id}"
            if self.is_sent(post_id):
                continue
            self.mark_seen(post_id, account, "story")
            url = (e.get("url") or e.get("webpage_url")
                   or STORY_URL.format(account=account))
            try:
                log.info("[instagram] ⬇️ 开始下载 Story %s", item_id)
                d = self.item_dir(account, item_id, "story")
                # Story 接口已给出直链 → 直接下，省掉 yt-dlp 的重复解析
                files = []
                direct = e.get("media") or []
                if direct:
                    tasks = []
                    for i, m in enumerate(direct, 1):
                        ext = ".mp4" if m["type"] == "video" else ".jpg"
                        tasks.append((m["url"],
                                      os.path.join(d, f"{item_id}_{i}{ext}")))
                    files = self._dl.download_many(
                        tasks, referer="https://www.instagram.com/")
                if not files:
                    files = self._dl.download_via_ytdlp(
                        url, d, platform_cfg=self.cfg,
                        outtmpl=f"{item_id}.%(ext)s",
                    )
                if not files:
                    # Story 必然是图片或视频 —— 没下到就跳过，下轮重试
                    raise RuntimeError("Story 媒体下载失败")
                ts = e.get("timestamp") or 0
                out.append(Post(
                    platform="instagram",
                    post_id=post_id,
                    author=self.display_name(account),
                    text=str(e.get("description") or e.get("title") or "").strip(),
                    media=self.build_media_items(files),
                    timestamp=(datetime.fromtimestamp(float(ts), tz=_JST)
                               .strftime("%Y-%m-%d %H:%M:%S JST") if ts
                               else time.strftime("%Y-%m-%d %H:%M:%S")),
                    extra={"account": account, "kind": "story",
                           "url": STORY_URL.format(account=account),
                           "item_id": item_id},
                ))
            except Exception as ex:
                log.warning("[instagram] Story %s 处理失败: %s", item_id, ex)
            if len(out) >= self.max_items_per_poll:
                break
        return out


def _best_image(node: dict) -> str:
    """取最大尺寸的原图。"""
    cands = ((node.get("image_versions2") or {}).get("candidates") or [])
    best, area = "", -1
    for c in cands:
        a = int(c.get("width") or 0) * int(c.get("height") or 0)
        if c.get("url") and a > area:
            best, area = c["url"], a
    return best


def _best_video(node: dict) -> str:
    """取最高码率/尺寸的视频。"""
    vs = node.get("video_versions") or []
    best, area = "", -1
    for v in vs:
        a = int(v.get("width") or 0) * int(v.get("height") or 0)
        if v.get("url") and a > area:
            best, area = v["url"], a
    return best


def _extract_media(item: dict) -> list[dict]:
    """从 Feed API 的一条 item 里取出全部媒体直链。

    media_type: 1=图片 2=视频/Reel 8=轮播（carousel_media 里逐条再判断）
    """
    out: list[dict] = []

    def _one(node: dict):
        mt = int(node.get("media_type") or 1)
        if mt == 2:
            u = _best_video(node)
            if u:
                out.append({"type": "video", "url": u})
                return
        u = _best_image(node)
        if u:
            out.append({"type": "image", "url": u})

    if int(item.get("media_type") or 1) == 8:
        for sub in (item.get("carousel_media") or []):
            _one(sub)
    else:
        _one(item)
    return out


def _cookies_from_browser(browser: str) -> dict:
    """借用 yt-dlp 的浏览器 cookie 提取能力，取出 instagram.com 的 cookie。

    这样 config 里的 `cookies_from_browser` 一处配置即可同时作用于
    yt-dlp 通道与 requests 通道，用户无需在本程序做任何登录操作。
    """
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except Exception as e:
        log.debug("[instagram] 无法读取浏览器 cookie（yt-dlp 不可用）: %s", e)
        return {}
    try:
        jar = extract_cookies_from_browser(browser.lower())
    except Exception as e:
        log.warning("[instagram] 读取 %s 浏览器 cookie 失败: %s",
                    browser, str(e)[:160])
        return {}
    out = {}
    for c in jar:
        if "instagram.com" in (c.domain or ""):
            out[c.name] = c.value
    return out


def _load_cookie_header(cookies_file: str) -> str:
    """把 Netscape cookies.txt 转成 Cookie 请求头（供 web API 兜底使用）。"""
    if not cookies_file or not os.path.exists(cookies_file):
        return ""
    pairs = []
    try:
        with open(cookies_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7 and "instagram" in parts[0]:
                    pairs.append(f"{parts[5]}={parts[6]}")
    except OSError:
        return ""
    return "; ".join(pairs)
