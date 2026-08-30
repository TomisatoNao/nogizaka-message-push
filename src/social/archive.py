"""
web/archive.py — 内容归档

**为什么需要它**：原程序抓到内容后直接推送 QQ，只在 sync_state.json 里记一个
post_id，正文 / 作者 / 时间 / 译文 / 媒体清单全部没有落地，磁盘上只剩媒体文件。
前台要展示「已抓取的内容」就必须先把这些留下来。

因此新增一张 posts 表（data/archive.db，与业务库分开，坏了也不影响抓取），
在**成功推送之后**写入。归档失败只记一条 warning，绝不影响推送流程。

另外提供 rebuild_from_disk()：把本次改动之前已经下载的媒体目录反向扫描成归档记录，
让历史内容也能在前台看到（X / TikTok 的 item id 内含时间戳，可还原发布时间）。
"""

import json
import logging
import os
import sqlite3
import threading
import time

from src.social.sqlite_utils import connect as sqlite_connect

log = logging.getLogger("collink")

DB_PATH = os.path.join("data", "archive.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id    TEXT PRIMARY KEY,
    platform   TEXT NOT NULL,
    account    TEXT DEFAULT '',
    author     TEXT DEFAULT '',
    kind       TEXT DEFAULT '',
    text       TEXT DEFAULT '',
    translated TEXT DEFAULT '',
    url        TEXT DEFAULT '',
    timestamp  TEXT DEFAULT '',
    ts         REAL DEFAULT 0,       -- 可排序的发布时间（秒）
    media      TEXT DEFAULT '[]',    -- [{type, path, name}]
    extra      TEXT DEFAULT '{}',
    archived_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_posts_ts ON posts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform, ts DESC);
"""

# 平台展示名（前台用）
PLATFORM_LABELS = {
    "melink": "≠ME LINK", "joylink": "≈JOY LINK", "showroom": "SHOWROOM", "youtube": "YouTube",
    "x": "X", "instagram": "Instagram", "tiktok": "TikTok",
    "tiktok_live": "TikTok 直播",
}

# 不需要翻译的平台：
#   showroom    —— 正文固定是「▶️ 直播中！」，本来就是中文
#   tiktok_live —— 正文是程序生成的【TikTok 开播提醒】通知块，同样已是中文
# 这类内容送去翻译纯属浪费 Gemini 配额（实测把「▶️ 直播中！」翻成了「▶️ 直播中！」）
NO_TRANSLATE_PLATFORMS = ("showroom", "tiktok_live")


def account_of(post) -> str:
    """取出一条 Post 的「账号标识」，用于人物归类。

    各平台的 extra 结构不同，社交平台有 account，而 melink / showroom /
    youtube 没有 —— 分别用房间名 / slug / 作者名代替，否则这些平台的内容
    会因为 account 为空而无法归到任何人物名下。
    """
    e = post.extra or {}
    for key in ("account", "room_name", "slug", "channel", "channel_id"):
        v = e.get(key)
        if v:
            return str(v)
    return post.author or ""


def _rel(path: str) -> str:
    """绝对路径 → 相对项目根的路径（前台通过 /api/files/download 取文件）。"""
    if not path:
        return ""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.getcwd())
    except ValueError:
        return ""
    return rel.replace("\\", "/")


class PostArchive:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite_connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(_SCHEMA)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            self._conn.commit()

    # ── 写入 ────────────────────────────────────────────

    def add_post(self, post) -> bool:
        """归档一条已成功推送的 Post。失败返回 False，不抛异常。"""
        try:
            # 图片 alt 的译文由 forwarder 存在 extra 里，键为「第几张图片」
            alt_zh = post.extra.get("_alt_translated") or {}
            media = []
            img_idx = 0
            for m in post.media:
                if m.type == "image":
                    img_idx += 1
                p = _rel(m.local_path)
                if p:
                    item = {"type": m.type, "path": p,
                            "name": os.path.basename(p),
                            "alt": m.alt_text or ""}
                    if m.type == "image" and alt_zh.get(str(img_idx)):
                        item["alt_zh"] = str(alt_zh[str(img_idx)])
                    media.append(item)
            extra = {k: v for k, v in (post.extra or {}).items()
                     if k not in ("_translated",) and isinstance(
                         v, (str, int, float, bool, type(None)))}
            # 开播提醒类内容本身就是中文，不保存译文
            translated = ("" if post.platform in NO_TRANSLATE_PLATFORMS
                          else str(post.extra.get("_translated", "") or ""))
            with self._lock:
                self._conn.execute(
                    "INSERT INTO posts (post_id, platform, account, author, kind,"
                    " text, translated, url, timestamp, ts, media, extra, archived_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(post_id) DO UPDATE SET"
                    "  text=excluded.text, translated=excluded.translated,"
                    "  media=excluded.media, archived_at=excluded.archived_at",
                    (post.post_id, post.platform,
                     account_of(post), post.author,
                     str(post.extra.get("kind", "")), post.text or "",
                     translated,
                     str(post.extra.get("url", "")), post.timestamp or "",
                     _parse_ts(post), json.dumps(media, ensure_ascii=False),
                     json.dumps(extra, ensure_ascii=False), time.time()))
                self._conn.commit()
            return True
        except Exception as e:
            log.warning("[archive] 归档 %s 失败: %s", getattr(post, "post_id", "?"), e)
            return False

    # ── 查询 ────────────────────────────────────────────

    def query(self, *, platform: str = "", account: str = "", keyword: str = "",
              limit: int = 30, offset: int = 0, has_media: bool = False,
              pairs: list | None = None) -> dict:
        """:param pairs: [(platform, account), ...] —— 按人物筛选时传入其全部账号"""
        where, vals = [], []
        if pairs is not None:
            if not pairs:
                return {"total": 0, "items": []}
            ors = " OR ".join(["(platform=? AND account=?)"] * len(pairs))
            where.append(f"({ors})")
            for plat, acc in pairs:
                vals += [plat, acc]
        if platform:
            where.append("platform=?")
            vals.append(platform)
        if account:
            where.append("account=?")
            vals.append(account)
        if keyword:
            where.append("(text LIKE ? OR translated LIKE ? OR author LIKE ?)")
            vals += [f"%{keyword}%"] * 3
        if has_media:
            where.append("media != '[]'")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) c FROM posts{clause}", vals).fetchone()["c"]  # nosec B608
            rows = self._conn.execute(
                f"SELECT * FROM posts{clause} ORDER BY ts DESC, archived_at DESC"
                f" LIMIT ? OFFSET ?", vals + [limit, offset]).fetchall()  # nosec B608
        return {"total": total, "items": [_row_to_dict(r) for r in rows]}

    def get(self, post_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM posts WHERE post_id=?",
                                   (post_id,)).fetchone()
        return _row_to_dict(r) if r else None

    def missing_text(self, limit: int = 500) -> list[dict]:
        """列出正文为空的归档（磁盘重建出来的历史内容）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT post_id, platform, account, kind FROM posts"
                " WHERE text = '' ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def missing_translation(self, limit: int = 500) -> list[dict]:
        """列出有正文但没有译文、且**需要**翻译的归档。

        开播提醒类内容（showroom / tiktok_live）本身就是中文，直接排除。
        """
        holes = ",".join("?" * len(NO_TRANSLATE_PLATFORMS))
        sql = f"SELECT post_id, platform, account, text FROM posts WHERE text != '' AND translated = '' AND platform NOT IN ({holes}) ORDER BY ts DESC LIMIT ?"  # nosec B608
        with self._lock:
            rows = self._conn.execute(sql, (*NO_TRANSLATE_PLATFORMS, limit)).fetchall()  # nosec B608
        return [dict(r) for r in rows]

    def clear_needless_translations(self) -> int:
        """清掉开播提醒类内容上多余的译文（早期版本会把它们也翻一遍）。"""
        holes = ",".join("?" * len(NO_TRANSLATE_PLATFORMS))
        with self._lock:
            n = self._conn.execute(
                f"UPDATE posts SET translated='' WHERE translated != ''"
                f" AND platform IN ({holes})", NO_TRANSLATE_PLATFORMS).rowcount  # nosec B608
            self._conn.commit()
        if n:
            log.info("[archive] 已清除 %s 条开播提醒的多余译文", n)
        return n

    def update_text(self, post_id: str, text: str = None,
                    translated: str = None, author: str = None,
                    url: str = None) -> bool:
        ALLOWED_COLUMNS = {"text", "translated", "author", "url"}
        sets, vals = [], []
        for col, v in (("text", text), ("translated", translated),
                       ("author", author), ("url", url)):
            if v is not None and col in ALLOWED_COLUMNS:
                sets.append(f"{col}=?")
                vals.append(v)
        if not sets:
            return False
        vals.append(post_id)
        with self._lock:
            n = self._conn.execute(
                f"UPDATE posts SET {', '.join(sets)} WHERE post_id=?", vals).rowcount  # nosec B608
            self._conn.commit()
        return n > 0

    def delete(self, post_ids: list) -> int:
        n = 0
        with self._lock:
            for pid in post_ids:
                n += self._conn.execute("DELETE FROM posts WHERE post_id=?",
                                        (pid,)).rowcount
            self._conn.commit()
        return n

    def stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT platform, COUNT(*) c, MAX(ts) latest FROM posts"
                " GROUP BY platform").fetchall()
            total = self._conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        return {
            "total": total,
            "platforms": [{"platform": r["platform"],
                           "label": PLATFORM_LABELS.get(r["platform"], r["platform"]),
                           "count": r["c"], "latest": r["latest"]} for r in rows],
        }

    def accounts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT platform, account, author, COUNT(*) c FROM posts"
                " WHERE account != '' GROUP BY platform, account"
                " ORDER BY c DESC").fetchall()
        return [dict(r) for r in rows]

    def repair_accounts(self) -> int:
        """回填 account 为空的旧归档。

        早期版本只认 extra["account"]，而 melink / showroom / youtube 的 Post
        里没有这个键，导致这些内容 account 为空、无法归到任何人物名下。
        这里从 extra 的 room_name / slug / channel 或 author 补回。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT post_id, author, extra FROM posts WHERE account = ''"
            ).fetchall()
        n = 0
        for r in rows:
            try:
                ex = json.loads(r["extra"] or "{}")
            except ValueError:
                ex = {}
            acc = ""
            for key in ("room_name", "slug", "channel", "channel_id"):
                if ex.get(key):
                    acc = str(ex[key])
                    break
            acc = acc or (r["author"] or "")
            if not acc:
                continue
            with self._lock:
                self._conn.execute("UPDATE posts SET account=? WHERE post_id=?",
                                   (acc, r["post_id"]))
            n += 1
        if n:
            with self._lock:
                self._conn.commit()
            log.info("[archive] 已回填 %s 条归档的账号归属", n)
        return n

    # ── 从磁盘重建历史归档 ──────────────────────────────

    def rebuild_from_disk(self, config: dict) -> dict:
        """扫描已下载的媒体目录，为改动之前抓取的内容补建归档记录。

        目录约定：messages/{platform}_media/{account}/{item_id}/*
        X 与 TikTok 的 item id 内含发布时间戳，可还原准确时间；
        其余平台用文件修改时间兜底。正文无法还原（原程序没保存），留空。
        """
        repaired = self.repair_accounts()   # 顺带修好旧记录的账号归属
        self.clear_needless_translations()  # 清掉开播提醒上多余的译文
        added = 0
        scanned = 0
        for platform in ("x", "instagram", "tiktok"):
            pcfg = (config.get("platforms") or {}).get(platform) or {}
            root = pcfg.get("download_dir") or f"data/social_media/{platform}"
            if not os.path.isdir(root) and os.path.isdir(f"messages/{platform}_media"):
                root = f"messages/{platform}_media"
            if not os.path.isdir(root):
                continue
            names = pcfg.get("display_names") or {}
            for account in sorted(os.listdir(root)):
                adir = os.path.join(root, account)
                if not os.path.isdir(adir):
                    continue
                for item in sorted(os.listdir(adir)):
                    idir = os.path.join(adir, item)
                    if not os.path.isdir(idir):
                        continue
                    scanned += 1
                    media = []
                    newest = 0.0
                    for fn in sorted(os.listdir(idir)):
                        fp = os.path.join(idir, fn)
                        if not os.path.isfile(fp):
                            continue
                        from src.social.downloader import classify_media
                        media.append({"type": classify_media(fp),
                                      "path": _rel(fp), "name": fn, "alt": ""})
                        try:
                            newest = max(newest, os.path.getmtime(fp))
                        except OSError:
                            pass
                    if not media:
                        continue
                    kind, item_id = _split_kind(item)
                    post_id = _guess_post_id(platform, kind, item_id)
                    with self._lock:
                        exists = self._conn.execute(
                            "SELECT 1 FROM posts WHERE post_id=?",
                            (post_id,)).fetchone()
                    if exists:
                        continue
                    ts = _ts_from_id(platform, item_id) or newest
                    with self._lock:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO posts (post_id, platform, account,"
                            " author, kind, text, translated, url, timestamp, ts,"
                            " media, extra, archived_at)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (post_id, platform, account,
                             names.get(account, account), kind, "", "",
                             _guess_url(platform, account, item_id, kind),
                             time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(ts)) if ts else "",
                             ts, json.dumps(media, ensure_ascii=False),
                             json.dumps({"rebuilt": True}), time.time()))
                        self._conn.commit()
                    added += 1
        log.info("[archive] 从磁盘重建：扫描 %s 个内容目录，新增 %s 条归档",
                 scanned, added)
        return {"scanned": scanned, "added": added, "repaired": repaired}


def _row_to_dict(r) -> dict:
    d = dict(r)
    try:
        d["media"] = json.loads(d.get("media") or "[]")
    except ValueError:
        d["media"] = []
    try:
        d["extra"] = json.loads(d.get("extra") or "{}")
    except ValueError:
        d["extra"] = {}
    d["platform_label"] = PLATFORM_LABELS.get(d["platform"], d["platform"])
    return d


def _parse_ts(post) -> float:
    """从 Post 的时间字符串还原时间戳，失败则用当前时间。"""
    s = (post.timestamp or "").replace(" JST", "").replace(" CST", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except (ValueError, OverflowError):
            continue
    return time.time()


def _split_kind(dirname: str) -> tuple:
    """目录名 story_123 / 123 → (kind, item_id)"""
    for k in ("story", "photo", "reel", "carousel", "retweet", "quote"):
        if dirname.startswith(k + "_"):
            return k, dirname[len(k) + 1:]
    return "post", dirname


def _guess_post_id(platform: str, kind: str, item_id: str) -> str:
    if kind == "story":
        return f"{platform}_story_{item_id}"
    return f"{platform}_{item_id}"


def _ts_from_id(platform: str, item_id: str) -> float:
    """从雪花式 ID 还原发布时间（X 与 TikTok 都可以）。"""
    try:
        n = int(item_id)
    except (TypeError, ValueError):
        return 0.0
    if platform == "tiktok":
        ts = n >> 32
    elif platform == "x":
        # Twitter snowflake：毫秒 = (id >> 22) + 1288834974657
        ts = ((n >> 22) + 1288834974657) / 1000.0
    else:
        return 0.0
    return ts if 1451606400 < ts < 4102444800 else 0.0


def _guess_url(platform: str, account: str, item_id: str, kind: str) -> str:
    if platform == "x":
        return f"https://x.com/{account}/status/{item_id}"
    if platform == "tiktok":
        seg = "photo" if kind == "photo" else "video"
        return f"https://www.tiktok.com/@{account}/{seg}/{item_id}"
    if platform == "instagram":
        return f"https://www.instagram.com/p/{item_id}/"
    return ""


_archive: PostArchive | None = None
_lock = threading.Lock()


def get_archive() -> PostArchive:
    global _archive
    with _lock:
        if _archive is None:
            _archive = PostArchive()
    return _archive
