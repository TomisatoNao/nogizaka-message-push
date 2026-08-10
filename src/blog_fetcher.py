"""博客监控：三个坂道团体的官方博客拉取、水印对比、SQLite 归档。"""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

import httpx
from src.logger import log_all
from src.sources import hinatazaka, nogizaka, sakurazaka
from pathlib import Path as _Path
import re as _re
import os as _os

BLOG_IMAGE_DIR = _Path("data/blog_images")


def _normalize_date(raw: str) -> str:
    """把各 source 的日期字符串归一化为 ISO 8601 'YYYY-MM-DD HH:MM' 格式。"""
    if not raw:
        return ""
    raw = raw.strip()
    # 2026.8.10 21:05 (hinatazaka)
    m = _re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:{m.group(5)}"
    # 2026/08/09 21:19 (sakurazaka)
    m2 = _re.match(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", raw)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)} {m2.group(4)}:{m2.group(5)}"
    # 2026/08/08 11:13:25 (nogizaka — 含秒)
    m3 = _re.match(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", raw)
    if m3:
        return f"{m3.group(1)}-{m3.group(2)}-{m3.group(3)} {m3.group(4)}:{m3.group(5)}"
    # Already ISO or unrecognized — return as-is
    return raw


async def _download_images(http_client: httpx.AsyncClient, image_urls: list[str],
                           group_key: str, author: str, title: str,
                           timestamp: str = "") -> list[str]:
    """下载博客图片到本地，目录结构：{group}/{author}/{title}-{ts}/01.jpg"""
    safe_title = _re.sub(r'[\\/:*?"<>|]', '', title)[:50].strip()
    safe_author = _re.sub(r'[\\/:*?"<>|]', '', author)[:20].strip()
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ts = _re.sub(r'[^0-9_]', '', ts)[:15]
    dest_dir = BLOG_IMAGE_DIR / group_key / safe_author / f"{safe_title}-{safe_ts}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, url in enumerate(image_urls):
        try:
            r = await http_client.get(url, timeout=30)
            ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
            if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
                ext = "jpg"
            fname = f"{i+1:02d}.{ext}"
            fpath = dest_dir / fname
            fpath.write_bytes(r.content)
            paths.append(str(fpath.relative_to(BLOG_IMAGE_DIR)))
        except Exception:
            paths.append("")
    return paths

# ── 博客任务表 ──
# (显示名, fetch_posts, fetch_images, record_key, need_detail)
TASKS = [
    ("日向坂46", hinatazaka.fetch_posts, hinatazaka.fetch_images, "hinatazaka", False),
    ("乃木坂46", nogizaka.fetch_posts, None, "nogizaka", False),
    ("樱坂46", sakurazaka.fetch_posts, sakurazaka.fetch_images, "sakurazaka", True),
]

JST = timezone(timedelta(hours=9))
BLOG_DB_PATH = Path("data/archive/blogs.db")

# ── SQLite ──


def init_blog_db() -> sqlite3.Connection:
    """初始化博客数据库，返回共享连接。"""
    BLOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BLOG_DB_PATH), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blog_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key TEXT NOT NULL,
            author TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            date TEXT,
            body_html TEXT,
            body_text TEXT,
            translation TEXT,
            images_json TEXT,
            image_paths_json TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blog_group_date ON blog_posts(group_key, date DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blog_url ON blog_posts(url);")
    # 增量升级：旧表可能没有这些列
    for col in ["body_html", "body_text", "translation", "image_paths_json"]:
        try:
            conn.execute(f"ALTER TABLE blog_posts ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()
    return conn


def _ensure_records(raw_config: dict) -> dict[str, str]:
    """确保 blog_records 字段存在，返回其引用（修改会反映回 raw_config）。"""
    if not raw_config.get("blog_records"):
        raw_config["blog_records"] = {"hinatazaka": "", "nogizaka": "", "sakurazaka": ""}
    return raw_config["blog_records"]


# ── 主循环 ──


async def run_blog_cycle(client: httpx.AsyncClient, db: sqlite3.Connection,
                         raw_config: dict) -> list[dict]:
    """执行一次博客巡查，返回本轮新发现的博客列表。

    返回的每个 post 额外包含 group_key 和 group_name。
    """

    blog_cfg = raw_config.get("blog_monitor") or {}
    if not blog_cfg.get("enabled", True):
        return []

    records = _ensure_records(raw_config)  # 直接引用 raw_config 内部字典
    new_posts = []

    for group_name, fetch_fn, fetch_img_fn, key, need_detail in TASKS:
        if not blog_cfg.get(key, True):
            continue  # 该团体被禁用

        try:
            posts = await fetch_fn(client)
        except Exception:
            continue

        if not posts:
            continue

        last_url = records.get(key, "")
        # 水印 diff：posts 按 newest→oldest，找 last_url 之前的所有新帖
        unseen = []
        for p in posts:
            if p["url"] == last_url:
                break
            unseen.append(p)

        # 首轮不做推送，只记水印
        if not last_url:
            records[key] = posts[0]["url"]
            log_all(f"📝 [{group_name}] 首轮初始化，水印: {posts[0]['url'][:60]}…")
            continue

        # 离线恢复：水印 URL 不在扫描窗口里
        if last_url and len(unseen) == len(posts):
            records[key] = posts[0]["url"]

        if not unseen:
            continue

        log_all(f"📝 [{group_name}] 发现 {len(unseen)} 篇新博客")

        # 按 oldest→newest 顺序处理
        raw_config["_blog_records_dirty"] = True
        for post in reversed(unseen):
            post["group_key"] = key
            post["group_name"] = group_name

            # 获取图片/正文/HTML
            if not post.get("images") and fetch_img_fn:
                if need_detail:
                    imgs, detail_date, body = await sakurazaka.fetch_detail(client, post["url"])
                    post["images"] = imgs
                    if detail_date:
                        post["date"] = detail_date
                    post["body"] = body
                    try:
                        post["body_html"] = await sakurazaka.fetch_html(client, post["url"])
                    except Exception:
                        post["body_html"] = body
                else:
                    post["images"] = await fetch_img_fn(client, post["url"])
                    post["body"] = await hinatazaka.fetch_body(client, post["url"])
                    post["body_html"] = await hinatazaka.fetch_html(client, post["url"])

            # 下载图片到本地
            image_paths = []
            if post.get("images"):
                image_paths = await _download_images(
                    client, post["images"], key,
                    post.get("author", ""), post.get("title", ""),
                    timestamp=post.get("date", "").replace("/", "").replace(" ", "_").replace(":", ""))

            # 存档到 SQLite
            try:
                db.execute("""
                    INSERT OR IGNORE INTO blog_posts
                    (group_key, author, title, url, date, body_html, body_text, images_json, image_paths_json, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    key, post.get("author", ""), post.get("title", ""), post["url"],
                    _normalize_date(post.get("date", "")),
                    post.get("body_html", ""), post.get("body", ""),
                    json.dumps(post.get("images") or [], ensure_ascii=False),
                    json.dumps(image_paths, ensure_ascii=False),
                    json.dumps(post, ensure_ascii=False, default=str),
                ))
                db.commit()
            except Exception:
                pass

            # 推进水印
            records[key] = post["url"]
            new_posts.append(post)

    # 写回水印到 config
    if raw_config.get("_blog_records_dirty"):
        raw_config["blog_records"] = records
        raw_config.pop("_blog_records_dirty", None)

    return new_posts


def _is_night_jst() -> bool:
    """JST 深夜时段（0-8 点）返回 True，适用于低频轮询。"""
    hour = datetime.now(JST).hour
    return 0 <= hour < 8
