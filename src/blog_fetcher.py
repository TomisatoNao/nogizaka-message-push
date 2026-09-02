"""博客监控：三个坂道团体的官方博客拉取、水印对比、SQLite 归档。"""
import json
import sqlite3
import time
import uuid
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
    safe_title = _re.sub(r'[\\/:*?"<>|#%&]', '', title)[:50].strip()
    safe_author = _re.sub(r'[\\/:*?"<>|#%&]', '', author)[:20].strip()
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ts = _re.sub(r'[^0-9_]', '', ts)[:15]
    dest_dir = BLOG_IMAGE_DIR / group_key / safe_author / f"{safe_title}-{safe_ts}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    sem = asyncio.Semaphore(5)

    async def _fetch_one(i: int, url: str) -> tuple[int, str]:
        async with sem:
            ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
            if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
                ext = "jpg"
            fname = f"{i+1:02d}.{ext}"
            fpath = dest_dir / fname
            if fpath.exists() and fpath.stat().st_size > 0:
                return (i, str(fpath.relative_to(BLOG_IMAGE_DIR)))
            try:
                r = await http_client.get(url, timeout=20)
                r.raise_for_status()
                fpath.write_bytes(r.content)
                return (i, str(fpath.relative_to(BLOG_IMAGE_DIR)))
            except Exception:
                if fpath.exists() and fpath.stat().st_size > 0:
                    return (i, str(fpath.relative_to(BLOG_IMAGE_DIR)))
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
_in_flight_blogs: set[str] = set()

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
            translation_status TEXT NOT NULL DEFAULT 'pending',
            translation_error TEXT,
            translation_request_id TEXT,
            translation_updated_at TEXT,
            images_json TEXT,
            image_paths_json TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blog_watermarks (
            group_key TEXT PRIMARY KEY,
            last_url TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blog_group_date ON blog_posts(group_key, date DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blog_date ON blog_posts(date DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_blog_url ON blog_posts(url);")
    # 增量升级：旧表可能没有这些列
    for col in [
        "body_html", "body_text", "translation", "content_json", "translation_model", "image_paths_json",
        "translation_status", "translation_error", "translation_request_id", "translation_updated_at",
    ]:
        try:
            conn.execute(f"ALTER TABLE blog_posts ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.commit()
    return conn

_BASE_DIR = Path(__file__).resolve().parent.parent
_RECORDS_PATH = _BASE_DIR / "data" / "blog_records.json"
_LEGACY_RECORDS_PATH = _BASE_DIR / "state" / "blog_records.json"

def _load_records(db: sqlite3.Connection | None = None) -> dict[str, str]:
    """从 SQLite 数据库加载博客水印，若有历史 JSON 文件则自动无缝导入并清理。"""
    conn = db or init_blog_db()
    records = {"hinatazaka": "", "nogizaka": "", "sakurazaka": ""}
    try:
        cur = conn.execute("SELECT group_key, last_url FROM blog_watermarks;")
        for row in cur.fetchall():
            records[row[0]] = row[1]
    except Exception:  # nosec B110
        pass

    # 若数据库中暂无记录，尝试从旧版 JSON 文件自动无缝迁移
    if not any(records.values()):
        for p in [_RECORDS_PATH, _LEGACY_RECORDS_PATH]:
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        records.update(data)
                        _save_records(records, conn)
                        try:
                            p.unlink()
                        except OSError:
                            pass
                        break
                except Exception:  # nosec B110
                    pass
    return records


def _save_records(records: dict[str, str], db: sqlite3.Connection | None = None) -> None:
    """持久化博客水印至 SQLite 数据库。"""
    conn = db or init_blog_db()
    import time
    try:
        with conn:
            for k, url in records.items():
                if url:
                    conn.execute(
                        "INSERT INTO blog_watermarks (group_key, last_url, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(group_key) DO UPDATE SET last_url = excluded.last_url, updated_at = excluded.updated_at;",
                        (k, url, time.time())
                    )
    except Exception as e:
        log_all(f"⚠️ 保存博客水印至 SQLite 失败: {e}", is_error=True)

# ── 主循环 ──


async def _process_single_post(post: dict, key: str, group_name: str,
                                client: httpx.AsyncClient, need_detail: bool,
                                fetch_img_fn) -> dict:
    """并发抓取单篇博客的详情、图片与 AI 双语翻译。"""
    import asyncio
    import json
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

    # 下载图片到本地（并发下载）
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
    translation_status = "skipped"
    translation_error = ""
    translation_request_id = f"auto-{uuid.uuid4().hex[:12]}"
    translation_started = time.monotonic()
    if post.get("body_html"):
        translation_status = "running"
        try:
            from src import translator
            if getattr(translator, "_http_client", None) is None:
                translator.initialize(client)
            log_all(
                f"🔄 后台博客翻译开始 | trace={translation_request_id} | "
                f"author={post.get('author', '')} | title={post.get('title', '')}",
            )
            structured, model_name = await translator.translate_blog_structured(
                post["body_html"],
                post.get("author", ""),
                key,
                custom_client=client,
                request_id=translation_request_id,
                source="blog_auto",
            )
            if structured:
                content_json = json.dumps(structured, ensure_ascii=False)
                translated_html = translator.blocks_to_html(structured)
                translation_model = model_name or ""
                _, _, complete = translator.blog_translation_stats(structured)
                translation_status = "succeeded" if complete else "partial"
                log_all(
                    f"✅ 后台博客翻译完成 | trace={translation_request_id} | "
                    f"model={translation_model or 'unknown'} | complete={'yes' if complete else 'no'} | "
                    f"elapsed={int((time.monotonic() - translation_started) * 1000)}ms",
                )
            else:
                translation_status = "failed"
                translation_error = "no_model_result"
                log_all(
                    f"⚠️ 后台博客翻译无结果 | trace={translation_request_id} | "
                    f"error={translation_error} | elapsed={int((time.monotonic() - translation_started) * 1000)}ms",
                    is_error=True,
                )
        except asyncio.TimeoutError:
            translation_status = "failed"
            translation_error = "timeout"
            log_all(
                f"⏱️ 后台博客翻译超时 | trace={translation_request_id} | "
                f"error={translation_error} | elapsed={int((time.monotonic() - translation_started) * 1000)}ms",
                is_error=True,
            )
        except httpx.TimeoutException:
            translation_status = "failed"
            translation_error = "timeout"
            log_all(
                f"⏱️ 后台博客翻译网络超时 | trace={translation_request_id} | "
                f"error={translation_error} | elapsed={int((time.monotonic() - translation_started) * 1000)}ms",
                is_error=True,
            )
        except httpx.HTTPError as exc:
            from src import translator
            translation_status = "failed"
            translation_error = translator.describe_exception(exc)
            log_all(
                f"⚠️ 后台博客翻译网络失败 | trace={translation_request_id} | "
                f"error={translation_error} | elapsed={int((time.monotonic() - translation_started) * 1000)}ms",
                is_error=True,
            )
        except (ValueError, TypeError) as exc:
            from src import translator
            translation_status = "failed"
            translation_error = translator.describe_exception(exc)
            log_all(
                f"⚠️ 后台博客翻译响应无效 | trace={translation_request_id} | "
                f"error={translation_error} | elapsed={int((time.monotonic() - translation_started) * 1000)}ms",
                is_error=True,
            )
        except Exception as exc:
            from src import translator
            translation_status = "failed"
            translation_error = translator.describe_exception(exc)
            log_all(
                f"⚠️ 后台博客翻译异常 | trace={translation_request_id} | "
                f"error={translation_error} | elapsed={int((time.monotonic() - translation_started) * 1000)}ms",
                is_error=True,
            )

    post["image_paths"] = image_paths
    post["translation"] = translated_html
    post["content_json"] = content_json
    post["translation_model"] = translation_model
    post["translation_status"] = translation_status
    post["translation_error"] = translation_error
    post["translation_request_id"] = translation_request_id
    post["translation_updated_at"] = datetime.now(timezone.utc).isoformat()
    return post


async def run_blog_cycle(client: httpx.AsyncClient, db: sqlite3.Connection,
                         raw_config: dict) -> list[dict]:
    """并发执行博客巡查（乃木坂46 / 樱坂46 / 日向坂46 多协程并发拉取与解析）。

    返回的每个 post 额外包含 group_key 和 group_name。
    """
    import asyncio

    blog_cfg = raw_config.get("blog_monitor") or {}
    if not blog_cfg.get("enabled", True):
        return []

    records = _load_records(db)
    records_dirty = False
    new_posts = []

    async def _check_group(group_name: str, fetch_fn, fetch_img_fn, key: str, need_detail: bool):
        nonlocal records_dirty
        if not blog_cfg.get(key, True):
            return []  # 该团体被禁用

        try:
            posts = await fetch_fn(client)
        except Exception:
            return []

        if not posts:
            return []

        last_url = records.get(key, "")
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
            return []
        # 离线恢复：持久化记录之后很久没抓取（水印脱节），直接重置水印，不推送积压
        elif len(unseen) == len(posts):
            records[key] = posts[0]["url"]
            records_dirty = True
            log_all(f"📝 [{group_name}] 水印已脱节，重置水印到最新，忽略中间积压的推送...")
            return []

        # 双重防御：过滤掉数据库中已经存在（已归档过）以及正在处理中的博客
        real_unseen = []
        for p in unseen:
            url = p["url"]
            if url in _in_flight_blogs:
                continue
            try:
                cur = db.execute("SELECT 1 FROM blog_posts WHERE url = ? LIMIT 1;", (url,))
                if not cur.fetchone():
                    real_unseen.append(p)
                    _in_flight_blogs.add(url)
            except Exception:
                real_unseen.append(p)
                _in_flight_blogs.add(url)

        # 推进最新水印
        if posts[0]["url"] != records.get(key):
            records[key] = posts[0]["url"]
            records_dirty = True

        if not real_unseen:
            return []

        log_all(f"📝 [{group_name}] 发现 {len(real_unseen)} 篇新博客，并发解析中...")

        # 并发解析新博客
        tasks = [
            _process_single_post(post, key, group_name, client, need_detail, fetch_img_fn)
            for post in reversed(real_unseen)
        ]
        return await asyncio.gather(*tasks)

    # 3 大团博客并行并发拉取
    group_tasks = [
        _check_group(group_name, fetch_fn, fetch_img_fn, key, need_detail)
        for group_name, fetch_fn, fetch_img_fn, key, need_detail in TASKS
    ]
    results = await asyncio.gather(*group_tasks)

    # 汇总并安全写入 SQLite 归档
    for group_posts in results:
        for post in group_posts:
            persisted = False
            try:
                db.execute("""
                    INSERT OR IGNORE INTO blog_posts
                    (group_key, author, title, url, date, body_html, body_text, translation, content_json, translation_model,
                     translation_status, translation_error, translation_request_id, translation_updated_at,
                     images_json, image_paths_json, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    post.get("group_key", ""), post.get("author", ""), post.get("title", ""), post["url"],
                    _normalize_date(post.get("date", "")),
                    post.get("body_html", ""), post.get("body", ""),
                    post.get("translation", ""),
                    post.get("content_json", ""),
                    post.get("translation_model", ""),
                    post.get("translation_status", "skipped"),
                    post.get("translation_error", ""),
                    post.get("translation_request_id", ""),
                    post.get("translation_updated_at", ""),
                    json.dumps(post.get("images") or [], ensure_ascii=False),
                    json.dumps(post.get("image_paths") or [], ensure_ascii=False),
                    json.dumps(post, ensure_ascii=False, default=str),
                ))
                db.commit()
                persisted = True
            except Exception:  # nosec B110
                log_all(
                    f"⚠️ 博客归档落库失败 | trace={post.get('translation_request_id', '') or '-'} | "
                    f"url={post.get('url', '')[:80]}",
                    is_error=True,
                )
            finally:
                # 只在当前巡查批次内去重；落库后允许后续巡查正常处理新的 URL，
                # 也避免一次 SQLite 异常把 URL 永久卡在内存集合中。
                _in_flight_blogs.discard(post.get("url", ""))

            if not persisted:
                # 落库失败时不推进水印，下一轮才能自动重试，而不是静默丢失博客。
                continue
            records[post.get("group_key", "")] = post["url"]
            new_posts.append(post)

    # 写回水印到 SQLite 数据库
    if records_dirty:
        _save_records(records, db)

    return new_posts


def _is_night_jst() -> bool:
    """JST 深夜时段（0-8 点）返回 True，适用于低频轮询。"""
    hour = datetime.now(JST).hour
    return 0 <= hour < 8
