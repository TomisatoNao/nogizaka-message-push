"""
src/webui_modules/archive_handlers.py — WebUI 归档数据路由与业务处理门面服务

提供：
  1. 消息归档：成员列表、月份列表、消息分页、日历热力图、FTS5 全文搜索、手动翻译与打标回填
  2. 官方博客：博客列表、分页、分组统计、按日历筛选、文章详情、单成员全量补抓、日文振假名与手动翻译
  3. 粉丝信件：信件列表、收藏状态切换与批量同步
  4. 首页聚合：全站动态瀑布流、时光隧道、写真画廊与统计指标
  5. 媒体流服务：头像、博客图片与消息图片分发调度
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading

from src import archive as _archive
from src.audit import record_event
from src.webui_modules.archive.blogs import (
    _blog_calendar_days,
    _blog_list_excerpt,
    _blog_translation_locks,
    _blog_translation_locks_guard,
    _get_blog_translation_lock,
    _set_blog_translation_state,
    handle_blogs,
)
from src.webui_modules.archive.common import (
    ARCHIVE_TYPES,
    BLOG_IMAGE_DIR,
    _archive_write_lock,
    _blog_db_local,
    _blog_media_url,
    _blog_table_columns,
    _send_json_resp,
    get_blog_db,
)
from src.webui_modules.archive.home import (
    _load_latest_text_by_member,
    _message_media_totals,
    handle_home,
)
from src.webui_modules.archive.letters import handle_letters
from src.webui_modules.archive.messages import handle_messages
from src.webui_modules.static_handler import send_json

# 首页聚合缓存管理状态（re-exported 且供 handle_archive 及单测 monkeypatch）
_home_cache: dict | None = None
_home_cache_key: tuple[float, float, str] | None = None
_home_cache_condition = threading.Condition()
_home_cache_building = False


def _home_cache_key_for_request() -> tuple[float, float, str]:
    """返回首页聚合缓存键；数据文件更新时间变化时自动触发重建。"""
    try:
        db_mtime = _archive.get_db_path().stat().st_mtime
    except OSError:
        db_mtime = 0
    try:
        blog_mtime = Path("data/archive/blogs.db").stat().st_mtime
    except OSError:
        blog_mtime = 0
    return db_mtime, blog_mtime, datetime.now().strftime("%Y-%m-%d")


def _acquire_home_cache(cache_key: tuple[float, float, str]) -> dict | None:
    """命中缓存则直接返回，否则确保只有一个请求执行昂贵的首页聚合。"""
    global _home_cache_building
    with _home_cache_condition:
        while True:
            if _home_cache is not None and _home_cache_key == cache_key:
                return _home_cache
            if not _home_cache_building:
                _home_cache_building = True
                return None
            _home_cache_condition.wait()


def _release_home_cache() -> None:
    """释放首页聚合占用并唤醒等待的请求。"""
    global _home_cache_building
    with _home_cache_condition:
        _home_cache_building = False
        _home_cache_condition.notify_all()


def handle_archive(handler, sub: str, guard_fn, read_body_json_fn) -> None:
    """归档路由入口；首页聚合请求使用单飞缓存避免并发重复计算。"""
    if sub == "home":
        cache_key = _home_cache_key_for_request()
        cached = _acquire_home_cache(cache_key)
        if cached is not None:
            _send_json_resp(handler, cached)
            return
        try:
            _handle_archive_impl(handler, sub, guard_fn, read_body_json_fn)
        finally:
            _release_home_cache()
        return
    _handle_archive_impl(handler, sub, guard_fn, read_body_json_fn)


def warm_home_cache() -> bool:
    """后台预热首页聚合缓存；失败不影响 WebUI 启动。"""
    class _WarmupHandler:
        path = "/api/archive/home"
        headers = {}
        _pending_headers = []
        _pending_set_cookies = []

        def _send_json(self, payload, _code=200):
            self.payload = payload

    handler = _WarmupHandler()
    try:
        handle_archive(handler, "home", lambda **_: True, lambda: {})
        return bool(getattr(handler, "payload", None))
    except Exception as exc:
        from src.logger import log_all
        log_all(f"⚠️ 首页缓存预热跳过: {type(exc).__name__}: {exc}", is_debug=True)
        return False


def _handle_archive_impl(handler, sub: str, guard_fn, read_body_json_fn) -> None:
    """归档子路由统一派发。"""
    # 1. 消息归档与媒体
    if handle_messages(handler, sub, guard_fn, read_body_json_fn):
        return

    # 2. 粉丝信件
    if handle_letters(handler, sub, guard_fn, read_body_json_fn):
        return

    # 3. 官方博客
    if handle_blogs(handler, sub, guard_fn, read_body_json_fn):
        return

    # 4. 首页聚合
    if sub == "home":
        handle_home(handler, sub, guard_fn, read_body_json_fn)
        return

    send_json(handler, {"ok": False, "errors": ["未知路径"]}, 404)


__all__ = [
    "ARCHIVE_TYPES",
    "BLOG_IMAGE_DIR",
    "_archive_write_lock",
    "_blog_calendar_days",
    "_blog_db_local",
    "_blog_list_excerpt",
    "_blog_media_url",
    "_blog_table_columns",
    "_blog_translation_locks",
    "_blog_translation_locks_guard",
    "_get_blog_translation_lock",
    "_home_cache",
    "_home_cache_building",
    "_home_cache_condition",
    "_home_cache_key",
    "_home_cache_key_for_request",
    "_acquire_home_cache",
    "_release_home_cache",
    "_load_latest_text_by_member",
    "_message_media_totals",
    "_send_json_resp",
    "_set_blog_translation_state",
    "get_blog_db",
    "handle_archive",
    "_handle_archive_impl",
    "record_event",
    "warm_home_cache",
]
