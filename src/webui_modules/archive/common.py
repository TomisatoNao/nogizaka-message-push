"""
src/webui_modules/archive/common.py — 归档模块公共基础定义与数据库连接
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
from urllib.parse import quote

from src.webui_modules.static_handler import send_json

BLOG_IMAGE_DIR = Path("data/blog_images")
_blog_db_local = threading.local()
_archive_write_lock = threading.Lock()
ARCHIVE_TYPES = frozenset({"text", "picture", "image", "video", "voice"})


def get_blog_db() -> sqlite3.Connection:
    """获取线程本地的博客 DB 连接（WAL 模式 + 并发隔离）。"""
    from src.blog_fetcher import init_blog_db
    conn = getattr(_blog_db_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1;")
            return conn
        except (sqlite3.Error, OSError):
            try:
                conn.close()
            except (sqlite3.Error, OSError):
                pass
            _blog_db_local.conn = None
    conn = init_blog_db()
    _blog_db_local.conn = conn
    return conn


def _blog_table_columns(db: sqlite3.Connection) -> set[str]:
    """读取博客表列名，用于兼容尚未完成迁移的旧测试/旧数据库。"""
    try:
        return {str(row[1]) for row in db.execute("PRAGMA table_info(blog_posts)").fetchall()}
    except (sqlite3.Error, OSError):
        return set()


def _send_json_resp(handler, obj: dict, code: int = 200) -> None:
    if hasattr(handler, "_send_json") and callable(getattr(handler, "_send_json")):
        try:
            handler._send_json(obj, code)
        except TypeError:
            handler._send_json(obj)
    else:
        send_json(handler, obj, code)


def _blog_media_url(relative_path: str) -> str:
    """把博客本地媒体路径转换为同源代理 URL。"""
    if not relative_path:
        return ""
    parts = str(relative_path).replace("\\", "/").strip("/").split("/")
    return "/api/archive/blog_media/" + "/".join(quote(part) for part in parts if part)
