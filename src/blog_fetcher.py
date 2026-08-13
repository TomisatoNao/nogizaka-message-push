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
    """并发下载博客图片到本地，目录结构：{group}/{author}/{title}-{ts}/01.jpg"""
    import asyncio
    safe_title = _re.sub(r'[\\/:*?"<>|]', '', title)[:50].strip()
    safe_author = _re.sub(r'[\\/:*?"<>|]', '', author)[:20].strip()
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ts = _re.sub(r'[^0-9_]', '', ts)[:15]
    dest_dir = BLOG_IMAGE_DIR / group_key / safe_author / f"{safe_title}-{safe_ts}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    sem = asyncio.Semaphore(5)

    async def _fetch_one(i: int, url: str) -> tuple[int, str]:
        async with sem:
            try:
                r = await http_client.get(url, timeout=20)
                ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
                if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
                    ext = "jpg"
                fname = f"{i+1:02d}.{ext}"
                fpath = dest_dir / fname
                fpath.write_bytes(r.content)
                return (i, str(fpath.relative_to(BLOG_IMAGE_DIR)))
            except Exception:
                return (i, "")

    tasks = [_fetch_one(i, url) for i, url in enumerate(image_urls)]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda x: x[0])
    return [path for _, path in results]

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
    conn = sqlite3.connect(str(BLOG_DB_PATH), timeout=60.0, check_same_thread=False)
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
            content_json TEXT,
            translation_model TEXT,
            images_json TEXT,
            image_paths_json TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blog_group_date ON blog_posts(group_key, date DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blog_url ON blog_posts(url);")
    # 增量升级：旧表可能没有这些列
    for col in ["body_html", "body_text", "translation", "content_json", "translation_model", "image_paths_json"]:
        try:
            conn.execute(f"ALTER TABLE blog_posts ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()
    return conn

_RECORDS_PATH = Path(__file__).resolve().parent.parent / "state" / "blog_records.json"

def _load_records() -> dict[str, str]:
    if _RECORDS_PATH.exists():
        try:
            return json.loads(_RECORDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"hinatazaka": "", "nogizaka": "", "sakurazaka": ""}

def _save_records(records: dict[str, str]) -> None:
    try:
        _RECORDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _RECORDS_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log_all(f"⚠️ 保存博客水印失败: {e}", is_error=True)

# ── 主循环 ──


async def run_blog_cycle(client: httpx.AsyncClient, db: sqlite3.Connection,
                         raw_config: dict) -> list[dict]:
    """执行一次博客巡查，返回本轮新发现的博客列表。

    返回的每个 post 额外包含 group_key 和 group_name。
    """

    blog_cfg = raw_config.get("blog_monitor") or {}
    if not blog_cfg.get("enabled", True):
        return []

    records = _load_records()
    records_dirty = False
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

        # 首轮初始化：只记最新水印，不做推送
        if not last_url:
            records[key] = posts[0]["url"]
            records_dirty = True
            log_all(f"📝 [{group_name}] 首轮初始化，记录水印: {posts[0]['url'][:60]}...")
            continue
        # 离线恢复：持久化记录之后很久没抓取（水印脱节），直接重置水印，不推送积压
        elif len(unseen) == len(posts):
            records[key] = posts[0]["url"]
            records_dirty = True
            log_all(f"📝 [{group_name}] 水印已脱节，重置水印到最新，忽略中间积压的推送...")
            continue

        if not unseen:
            continue

        log_all(f"📝 [{group_name}] 发现 {len(unseen)} 篇新博客")

        # 按 oldest→newest 顺序处理
        records_dirty = True
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

            # 自动调用 Gemini 翻译（结构化解耦：jp/zh 分离存储，绝不硬拼接单一文本）
            translated_html = ""
            content_json = ""
            translation_model = ""
            if post.get("body_html"):
                try:
                    from src import translator
                    if getattr(translator, "_http_client", None) is None:
                        translator.initialize(client)
                    log_all(f"🔄 正在后台翻译博客: {post.get('author', '')} - {post.get('title', '')}")
                    structured, model_name = await translator.translate_blog_structured(
                        post["body_html"],
                        post.get("author", ""),
                        key
                    )
                    if structured:
                        content_json = json.dumps(structured, ensure_ascii=False)
                        translated_html = translator.blocks_to_html(structured)
                        translation_model = model_name or ""
                        log_all(f"✅ 后台博客翻译完成（模型: {translation_model}）")
                except Exception as e:
                    log_all(f"⚠️ 自动翻译博客失败: {e}", is_debug=True)

            post["translation"] = translated_html
            post["content_json"] = content_json
            post["translation_model"] = translation_model

            # 存档到 SQLite
            try:
                db.execute("""
                    INSERT OR IGNORE INTO blog_posts
                    (group_key, author, title, url, date, body_html, body_text, translation, content_json, translation_model, images_json, image_paths_json, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    key, post.get("author", ""), post.get("title", ""), post["url"],
                    _normalize_date(post.get("date", "")),
                    post.get("body_html", ""), post.get("body", ""),
                    translated_html,
                    content_json,
                    translation_model,
                    json.dumps(post.get("images") or [], ensure_ascii=False),
                    json.dumps(image_paths, ensure_ascii=False),
                    json.dumps(post, ensure_ascii=False, default=str),
                ))
                db.commit()
            except Exception:
                pass

            # 推进水印（每个 post 独立推进，容错）
            records[key] = post["url"]
            new_posts.append(post)

    # 写回水印到单独文件
    if records_dirty:
        _save_records(records)

    return new_posts


def _is_night_jst() -> bool:
    """JST 深夜时段（0-8 点）返回 True，适用于低频轮询。"""
    hour = datetime.now(JST).hour
    return 0 <= hour < 8
