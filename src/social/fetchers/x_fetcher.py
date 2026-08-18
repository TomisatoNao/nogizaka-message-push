"""
fetchers/x_fetcher.py — X（Twitter）监控

支持内容：普通推文 / 图片 / 多图 / 视频 / GIF / 引用推文 / 转推（可配置是否推送）。

X 没有免费的公开时间线 API，因此实现了**多后端链**，按 config 中
`platforms.x.backends` 的顺序依次尝试，第一个成功的即采用：

  1. "syndication" —— syndication.twitter.com 时间线（免 token，默认首选）
  2. "nitter"      —— Nitter 实例的 RSS（免 token，实例可配置）
  3. "apiv2"       —— 官方 API v2（需在 config 里填 bearer_token，可选）

媒体下载策略：
  * 图片 → 改写为 `?name=orig` 原图直链，requests 并发下载
  * 视频 / GIF → 取 video_info.variants 中码率最高的 MP4（保留原始音轨）
  * 拿不到直链时回退 yt-dlp 解析推文页面
"""

import html
import json
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests

from src.social.fetchers.social_base import SocialFetcher
from src.social.models import Post

log = logging.getLogger("collink")

_JST = timezone(timedelta(hours=9))
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SYNDICATION_URL = ("https://syndication.twitter.com/srv/timeline-profile/"
                   "screen-name/{screen_name}")
TWEET_RESULT_URL = "https://cdn.syndication.twimg.com/tweet-result"
APIV2_USER = "https://api.twitter.com/2/users/by/username/{screen_name}"
APIV2_TWEETS = "https://api.twitter.com/2/users/{uid}/tweets"
TWEET_URL = "https://x.com/{screen_name}/status/{tweet_id}"

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def is_card_image(url: str) -> bool:
    """是否为「链接预览缩略图」而非用户发布的媒体。

    card_img 取不到原图（改写成 name=orig 会 404），而且把它当成媒体会让
    纯链接推文因「媒体全部下载失败」被无限重试。各后端都要过滤。
    """
    return "/card_img/" in (url or "")


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
    # .../media/XXXX.jpg → .../media/XXXX?format=jpg&name=orig
    m = re.match(r"(.*/[\w-]+)\.(jpg|jpeg|png|webp)$", base, re.I)
    if m:
        return f"{m.group(1)}?format={m.group(2).lower()}&name=orig"
    # 已带查询串（如 ?format=jpg&name=800x419）→ **替换** name，而不是追加。
    # 追加会得到 name=800x419&name=orig，Twitter 直接返回 404。
    if query:
        parts = [p for p in query.split("&") if p and not p.startswith("name=")]
        parts.append("name=orig")
        return f"{base}?{'&'.join(parts)}"
    return f"{base}?name=orig"


def _syndication_token(tweet_id: str) -> str:
    """计算 cdn.syndication.twimg.com 单推接口所需的 token（免登录）。

    等价于官方嵌入脚本里的
    ``((id / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, "")``。
    """
    try:
        n = (int(tweet_id) / 1e15) * math.pi
    except (TypeError, ValueError):
        return "0"
    # 手写 base36（Python 没有内置的小数进制转换）
    whole = int(n)
    frac = n - whole
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    head = ""
    if whole == 0:
        head = "0"
    while whole:
        whole, r = divmod(whole, 36)
        head = digits[r] + head
    tail = ""
    for _ in range(20):          # JS toString(36) 的小数位精度足够覆盖
        frac *= 36
        d = int(frac)
        tail += digits[d]
        frac -= d
    return re.sub(r"(0+|\.)", "", f"{head}.{tail}") or "0"


def _best_variant(video_info: dict) -> str:
    """从 video_info.variants 中选码率最高的 MP4（视频与 GIF 都走这里）。"""
    best, best_br = "", -1
    for v in (video_info or {}).get("variants", []) or []:
        if v.get("content_type") != "video/mp4":
            continue
        br = int(v.get("bitrate") or 0)
        if br > best_br:
            best_br, best = br, v.get("url", "")
    if best:
        return best
    # 没有 MP4 就退 m3u8（交给 yt-dlp 处理）
    for v in (video_info or {}).get("variants", []) or []:
        if v.get("url"):
            return v["url"]
    return ""


def _strip_html(fragment: str) -> str:
    """HTML 片段 → 纯文本，保留原有换行结构。"""
    if not fragment:
        return ""
    # <br> / </p> 转成换行。注意 Nitter 的源码里每个 <br> 后面**本来就跟着**
    # 一个真实换行，所以要把紧随其后的换行一并吃掉，否则作者打的每个换行都会
    # 变成两个（连续 <br><br> 仍能正确得到空行）。
    s = re.sub(r"<br\s*/?>[ \t]*\n?", "\n", fragment, flags=re.I)
    s = re.sub(r"</p\s*>[ \t]*\n?", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    # 折叠标签留下的多余空行，但保留作者刻意打的空行
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _nitter_quote(tail_html: str) -> tuple:
    """从 Nitter 的 <hr/> 之后解析被引用推文，返回 (正文, 作者)。

    只认 <blockquote>，链接预览卡片没有这个标签，因此不会被误当成引用。
    """
    if not tail_html:
        return "", ""
    m = re.search(r"<blockquote>(.*?)</blockquote>", tail_html, re.S | re.I)
    if not m:
        return "", ""
    block = m.group(1)
    author = ""
    ma = re.search(r"<b>(.*?)</b>", block, re.S | re.I)
    if ma:
        author = _strip_html(ma.group(1))
        block = block.replace(ma.group(0), "", 1)   # 作者名不重复进正文
    # footer 只是引用原推的链接，去掉
    block = re.sub(r"<footer>.*?</footer>", "", block, flags=re.S | re.I)
    return _strip_html(block), author


def _parse_twitter_date(s: str) -> float:
    """解析 "Sat Jul 26 12:00:00 +0000 2026" 或 ISO8601 时间。"""
    if not s:
        return 0.0
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s.replace("Z", "+0000"), fmt).timestamp()
        except ValueError:
            continue
    return 0.0


class _RawTweet:
    """后端无关的推文中间表示。"""

    def __init__(self, *, tweet_id: str, text: str, created_ts: float,
                 author: str, screen_name: str, kind: str = "post",
                 media: list[dict] | None = None,
                 quoted_text: str = "", quoted_author: str = "",
                 is_retweet: bool = False, is_reply: bool = False,
                 url: str = ""):
        self.tweet_id = str(tweet_id)
        self.text = text or ""
        self.created_ts = created_ts
        self.author = author
        self.screen_name = screen_name
        self.kind = kind
        self.media = media or []   # [{type, url, alt}]
        self.quoted_text = quoted_text
        self.quoted_author = quoted_author
        self.is_retweet = is_retweet
        self.is_reply = is_reply
        self.url = url or TWEET_URL.format(screen_name=screen_name, tweet_id=tweet_id)


class XFetcher(SocialFetcher):
    platform_name = "x"
    kinds = ("post", "retweet", "quote")

    def __init__(self, config: dict, store=None, downloader=None):
        super().__init__(config, store, downloader)
        self._session = requests.Session()
        proxy = self.cfg.get("proxy") or config.get("proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or ""
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})
        self._session.headers.update({"User-Agent": _UA,
                                      "Accept-Language": "ja,en;q=0.8"})
        self._uid_cache: dict[str, str] = {}

    # ── 主流程 ───────────────────────────────────────────

    def _fetch_account(self, account: str) -> list[Post]:
        tweets = self._fetch_timeline(account)
        if not tweets:
            return []

        # 按配置过滤形态
        cfg = self.cfg
        filtered: list[_RawTweet] = []
        for t in tweets:
            if t.is_retweet and not cfg.get("include_retweets", False):
                continue
            if t.is_reply and not cfg.get("include_replies", False):
                continue
            if t.kind == "quote" and not cfg.get("include_quotes", True):
                continue
            filtered.append(t)

        # 首次运行只记录不推送
        if self._bootstrap_guard(account, [self._pid(t) for t in filtered]):
            return []

        fresh = [t for t in filtered if not self.is_sent(self._pid(t))]
        # 时间升序，最多处理 N 条
        fresh.sort(key=lambda t: t.created_ts)
        if len(fresh) > self.max_items_per_poll:
            log.info("[x] @%s 新内容 %s 条，本轮先处理最新 %s 条",
                     account, len(fresh), self.max_items_per_poll)
            fresh = fresh[-self.max_items_per_poll:]

        posts: list[Post] = []
        for t in fresh:
            try:
                posts.append(self._build_post(account, t))
            except Exception as e:
                log.warning("[x] 处理推文 %s 失败: %s", t.tweet_id, e)
        return posts

    def _pid(self, t: _RawTweet) -> str:
        return f"x_{t.tweet_id}"

    def _build_post(self, account: str, t: _RawTweet) -> Post:
        post_id = self._pid(t)
        self.mark_seen(post_id, account, t.kind)

        media_items = []
        if t.media:
            log.info("[x] ⬇️ 开始下载 %s 的媒体（%s 个）", t.tweet_id, len(t.media))
            media_items = self._download_media(account, t)
            if not media_items:
                # 该推文确实带媒体但一个都没下到 —— 不要发出「媒体数量：0」的消息，
                # 抛异常让本条跳过（未标记 sent），下轮自动重试
                raise RuntimeError(
                    f"{t.tweet_id} 的 {len(t.media)} 个媒体全部下载失败，本条稍后重试")

        return Post(
            platform="x",
            post_id=post_id,
            author=self.display_name(account) or t.author,
            text=t.text,
            media=media_items,
            timestamp=(datetime.fromtimestamp(t.created_ts, tz=_JST)
                       .strftime("%Y-%m-%d %H:%M:%S JST") if t.created_ts else ""),
            extra={
                "account": account,
                "kind": t.kind,
                "url": t.url,
                "quoted_text": t.quoted_text,
                "quoted_author": t.quoted_author,
                "tweet_id": t.tweet_id,
            },
        )

    def _download_media(self, account: str, t: _RawTweet) -> list:
        """下载推文媒体：图片直下原图，视频/GIF 直下最高码率 MP4，失败回退 yt-dlp。"""
        d = self.item_dir(account, t.tweet_id, t.kind)
        tasks, urls, alts = [], [], []
        for i, m in enumerate(t.media, 1):
            url = m.get("url", "")
            if not url or is_card_image(url):
                continue
            ext = os.path.splitext(url.split("?")[0])[1].lower() or (
                ".jpg" if m.get("type") == "image" else ".mp4")
            if "format=jpg" in url:
                ext = ".jpg"
            elif "format=png" in url:
                ext = ".png"
            dest = os.path.join(d, f"{t.tweet_id}_{i}{ext}")
            tasks.append((url, dest))
            urls.append(url)
            alts.append(m.get("alt", ""))

        files = self._dl.download_many(tasks, referer="https://x.com/") if tasks else []

        # 有媒体但未下载到任何文件（包括 Nitter 的视频占位符没有直链）
        # → 回退 yt-dlp 解析推文页。
        if t.media and not files:
            log.info("[x] 直链下载失败，回退 yt-dlp 解析 %s", t.url)
            files = self._dl.download_via_ytdlp(
                t.url, d, platform_cfg=self.cfg,
                outtmpl=f"{t.tweet_id}_%(playlist_index|1)s.%(ext)s",
            )
            urls, alts = [], []

        # 让 alt 与实际下载成功的文件对齐（download_many 会丢掉失败项）
        if files and len(files) == len(tasks):
            return self.build_media_items(files, urls, alts)
        return self.build_media_items(files)

    # ── 后端链 ───────────────────────────────────────────

    def _fetch_timeline(self, account: str) -> list[_RawTweet]:
        backends = self.cfg.get("backends") or ["syndication", "nitter", "apiv2"]
        for name in backends:
            fn = {
                "syndication": self._backend_syndication,
                "nitter": self._backend_nitter,
                "apiv2": self._backend_apiv2,
            }.get(str(name).lower())
            if fn is None:
                continue
            try:
                got = fn(account)
            except Exception as e:
                log.debug("[x] 后端 %s 异常: %s", name,
                          str(e).replace("\n", " ")[:160])
                continue
            if got:
                log.debug("[x] 后端 %s 取得 %s 条推文", name, len(got))
                self._fill_missing_alts(got)
                return got
            log.debug("[x] 后端 %s 无结果，尝试下一个", name)
        log.debug("[x] @%s 所有后端均无结果（可能被限流或账号无公开推文）", account)
        return []

    # ── 图片 alt（无障碍描述）补齐 ────────────────────────
    #
    # Nitter RSS 的 <img> 不带 alt，官方 API v2 未申请到 media.fields 时也没有。
    # cdn.syndication.twimg.com 的单推接口免登录、带 ext_alt_text，用它补齐。

    def _tweet_alts(self, tweet_id: str) -> list[str]:
        """按媒体顺序返回该推文的 alt 描述；失败返回空列表。"""
        try:
            resp = self._session.get(
                TWEET_RESULT_URL,
                params={"id": tweet_id, "lang": "ja",
                        "token": _syndication_token(tweet_id)},
                timeout=self._dl.timeout,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception as e:
            log.debug("[x] alt 查询失败 %s: %s", tweet_id, str(e)[:120])
            return []
        details = data.get("mediaDetails") or []
        return [(d.get("ext_alt_text") or "") for d in details
                if isinstance(d, dict)]

    def _fill_missing_alts(self, tweets: list) -> None:
        """对「有图但一条 alt 都没有」的推文补查 alt（就地修改）。"""
        if not self.cfg.get("fetch_alt_text", True):
            return
        for t in tweets:
            imgs = [m for m in t.media if m.get("type") == "image"]
            if not imgs or any((m.get("alt") or "").strip() for m in t.media):
                continue
            alts = self._tweet_alts(t.tweet_id)
            if not alts:
                continue
            # mediaDetails 与 t.media 同为「推文媒体顺序」，按位对齐
            for m, a in zip(t.media, alts):
                if a and not (m.get("alt") or "").strip():
                    m["alt"] = a
            if any((m.get("alt") or "").strip() for m in t.media):
                log.debug("[x] 推文 %s 已补齐图片 alt 描述", t.tweet_id)

    # 后端 1：syndication 时间线（免 token）
    def _backend_syndication(self, account: str) -> list[_RawTweet]:
        resp = self._session.get(
            SYNDICATION_URL.format(screen_name=account),
            params={"showReplies": "true"},
            timeout=self._dl.timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        m = _NEXT_DATA_RE.search(resp.text)
        if not m:
            raise RuntimeError("未找到 __NEXT_DATA__")
        data = json.loads(m.group(1))
        entries = (data.get("props", {}).get("pageProps", {})
                   .get("timeline", {}).get("entries", []) or [])

        out: list[_RawTweet] = []
        for entry in entries:
            tw = (entry.get("content") or {}).get("tweet")
            if not isinstance(tw, dict):
                continue
            try:
                out.append(self._from_legacy_tweet(tw, account))
            except Exception:
                continue
        return out

    def _from_legacy_tweet(self, tw: dict, account: str) -> _RawTweet:
        """把 syndication 返回的 legacy 推文对象转成中间表示。"""
        retweeted = tw.get("retweeted_status")
        is_retweet = isinstance(retweeted, dict)
        # 转推时正文与媒体都取被转推的原推
        src = retweeted if is_retweet else tw

        user = src.get("user") or {}
        text = src.get("full_text") or src.get("text") or ""
        # 去掉结尾自动附加的 t.co 媒体短链（媒体已单独提取）
        text = re.sub(r"\s*https://t\.co/\w+\s*$", "", text).strip()

        media: list[dict] = []
        ext_media = ((src.get("extended_entities") or {}).get("media")
                     or (src.get("entities") or {}).get("media") or [])
        for mm in ext_media:
            mtype = mm.get("type", "photo")
            if mtype == "photo":
                raw_url = mm.get("media_url_https", "")
                if is_card_image(raw_url):
                    continue
                media.append({
                    "type": "image",
                    "url": _orig_image(raw_url),
                    "alt": mm.get("ext_alt_text") or "",
                })
            else:  # video / animated_gif
                vurl = _best_variant(mm.get("video_info") or {})
                if vurl:
                    media.append({"type": "video", "url": vurl,
                                  "alt": mm.get("ext_alt_text") or ""})

        quoted = src.get("quoted_status") or tw.get("quoted_status")
        quoted_text, quoted_author = "", ""
        if isinstance(quoted, dict):
            quoted_text = quoted.get("full_text") or quoted.get("text") or ""
            qu = quoted.get("user") or {}
            quoted_author = qu.get("name") or qu.get("screen_name") or ""

        screen_name = user.get("screen_name") or account
        # 双保险：正文作者不是被监控账号本人 → 一律按转推处理
        # （include_retweets=false 时会被过滤，只推送用户自己发的内容）
        if not is_retweet and screen_name.lower() != account.lower():
            is_retweet = True
        kind = "retweet" if is_retweet else ("quote" if quoted_text else "post")

        return _RawTweet(
            tweet_id=tw.get("id_str") or str(tw.get("id") or ""),
            text=(f"RT @{screen_name}: {text}" if is_retweet else text),
            created_ts=_parse_twitter_date(tw.get("created_at", "")),
            author=user.get("name") or screen_name,
            screen_name=account,
            kind=kind,
            media=media,
            quoted_text=quoted_text,
            quoted_author=quoted_author,
            is_retweet=is_retweet,
            is_reply=bool(tw.get("in_reply_to_status_id_str")),
        )

    # 后端 2：Nitter RSS（免 token）
    def _backend_nitter(self, account: str) -> list[_RawTweet]:
        instances = self.cfg.get("nitter_instances") or [
            "https://nitter.perennialte.ch",
            "https://xcancel.com",
        ]
        last_err = None
        for inst in instances:
            base = str(inst).rstrip("/")
            try:
                resp = self._session.get(f"{base}/{account}/rss",
                                         timeout=self._dl.timeout)
                if resp.status_code != 200:
                    last_err = f"HTTP {resp.status_code}"
                    continue
                root = ET.fromstring(resp.text)
            except Exception as e:
                last_err = str(e)
                continue

            out: list[_RawTweet] = []
            for item in root.iterfind(".//item"):
                link = (item.findtext("link") or "")
                mid = re.search(r"/status/(\d+)", link)
                if not mid:
                    continue
                tweet_id = mid.group(1)
                title = html.unescape(item.findtext("title") or "")
                desc = item.findtext("description") or ""
                creator = (item.findtext("{http://purl.org/dc/elements/1.1/}creator")
                           or f"@{account}")
                is_retweet = title.startswith("RT by") or f"@{account}" not in creator

                # Nitter description 的结构：
                #   作者正文 + 作者自己的媒体
                #   <hr/> 之后 → 链接预览卡片（Link / 站点标题 / 域名 / 简介）
                #                和被引用推文（<blockquote>）
                # 卡片文案与引用推文都不是作者写的正文，整段 strip 标签会把
                # 「Link」「TikTok · 鈴木瞳美」「tiktok.com」之类混进推送内容；
                # 卡片缩略图和引用推文里的图也不是作者发的媒体。
                # 因此正文与媒体一律只取第一个 <hr/> 之前的部分。
                body_html, _, tail_html = desc.partition("<hr")

                # 从正文段提取图片；Nitter 的 /pic/ 路径还原为 pbs 原图
                media: list[dict] = []
                for src in re.findall(r'<img[^>]+src="([^"]+)"', body_html):
                    real = _nitter_to_pbs(src)
                    if real and not is_card_image(real):
                        media.append({"type": "image", "url": real, "alt": ""})
                if re.search(r'<video[^>]', body_html):
                    # Nitter 视频需要 yt-dlp 兜底 —— 放一个占位让下游走回退分支
                    media.append({"type": "video", "url": "", "alt": ""})

                text = _strip_html(body_html) or title
                quoted_text, quoted_author = _nitter_quote(tail_html)

                out.append(_RawTweet(
                    tweet_id=tweet_id,
                    text=text,
                    created_ts=_parse_rss_date(item.findtext("pubDate") or ""),
                    author=self.display_name(account),
                    screen_name=account,
                    kind=("retweet" if is_retweet
                          else ("quote" if quoted_text else "post")),
                    media=[m for m in media if m.get("url")] or media,
                    quoted_text=quoted_text,
                    quoted_author=quoted_author,
                    is_retweet=is_retweet,
                ))
            if out:
                return out
        if last_err:
            raise RuntimeError(f"所有 Nitter 实例均失败（最后错误: {last_err}）")
        return []

    # 后端 3：官方 API v2（需 bearer_token）
    def _backend_apiv2(self, account: str) -> list[_RawTweet]:
        token = (self.cfg.get("bearer_token") or "").strip()
        if not token:
            return []
        hdrs = {"Authorization": f"Bearer {token}", "User-Agent": _UA}

        uid = self._uid_cache.get(account)
        if not uid:
            r = self._session.get(APIV2_USER.format(screen_name=account),
                                  headers=hdrs, timeout=self._dl.timeout)
            r.raise_for_status()
            uid = (r.json().get("data") or {}).get("id")
            if not uid:
                raise RuntimeError("无法解析用户 ID")
            self._uid_cache[account] = uid

        r = self._session.get(
            APIV2_TWEETS.format(uid=uid), headers=hdrs,
            params={
                "max_results": 20,
                "tweet.fields": "created_at,text,referenced_tweets,attachments",
                "expansions": "attachments.media_keys,referenced_tweets.id,"
                              "referenced_tweets.id.author_id",
                "media.fields": "url,variants,type,alt_text,preview_image_url",
            },
            timeout=self._dl.timeout,
        )
        r.raise_for_status()
        body = r.json()
        media_map = {m["media_key"]: m
                     for m in (body.get("includes") or {}).get("media", [])}
        ref_map = {t["id"]: t
                   for t in (body.get("includes") or {}).get("tweets", [])}

        out: list[_RawTweet] = []
        for tw in body.get("data") or []:
            refs = tw.get("referenced_tweets") or []
            rtypes = {r_.get("type") for r_ in refs}
            is_retweet = "retweeted" in rtypes
            is_reply = "replied_to" in rtypes

            quoted_text, quoted_author = "", ""
            for r_ in refs:
                if r_.get("type") == "quoted":
                    q = ref_map.get(r_.get("id")) or {}
                    quoted_text = q.get("text", "")

            media: list[dict] = []
            for key in (tw.get("attachments") or {}).get("media_keys", []) or []:
                mm = media_map.get(key) or {}
                if mm.get("type") == "photo":
                    if is_card_image(mm.get("url", "")):
                        continue
                    media.append({"type": "image",
                                  "url": _orig_image(mm.get("url", "")),
                                  "alt": mm.get("alt_text") or ""})
                else:
                    vurl = _best_variant({"variants": mm.get("variants") or []})
                    if vurl:
                        media.append({"type": "video", "url": vurl,
                                      "alt": mm.get("alt_text") or ""})

            out.append(_RawTweet(
                tweet_id=tw["id"],
                text=tw.get("text", ""),
                created_ts=_parse_twitter_date(tw.get("created_at", "")),
                author=self.display_name(account),
                screen_name=account,
                kind="retweet" if is_retweet else ("quote" if quoted_text else "post"),
                media=media,
                quoted_text=quoted_text,
                quoted_author=quoted_author,
                is_retweet=is_retweet,
                is_reply=is_reply,
            ))
        return out


def _nitter_to_pbs(src: str) -> str:
    """Nitter 的 /pic/media%2FXXX.jpg 或 /pic/amplify_video_thumb%2F... 还原成 pbs.twimg.com 图片。"""
    from urllib.parse import unquote
    m = re.search(r"/pic/(.+)$", src)
    if not m:
        return src if src.startswith("http") else ""
    path = unquote(m.group(1)).lstrip("/")
    if any(path.startswith(prefix) for prefix in ("media/", "orig/", "amplify_video_thumb/", "tweet_video_thumb/", "ext_tw_video_thumb/", "card_img/")):
        return _orig_image(f"https://pbs.twimg.com/{path}")
    return _orig_image(f"https://pbs.twimg.com/media/{path}")



def _parse_rss_date(s: str) -> float:
    """RFC822 → 时间戳。"""
    if not s:
        return 0.0
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return 0.0
