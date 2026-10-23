"""
src/webui_modules/archive_handlers.py — WebUI 归档数据路由与业务处理门面服务

提供：
  1. 消息归档：成员列表、月份列表、消息分页、日历热力图、FTS5 全文搜索、手动翻译与打标回填
  2. 官方博客：博客列表、分页、分组统计、按日历筛选、文章详情、三团/单成员全量补抓、日文振假名与手动翻译
  3. 粉丝信件：信件列表、收藏状态切换与批量同步
  4. 首页聚合：全站动态瀑布流、时光隧道、写真画廊与统计指标
  5. 媒体流服务：头像、博客图片与消息图片分发调度
"""

from __future__ import annotations

import sqlite3

from src.audit import record_event
from src.webui_modules.archive import common as _common
from src.webui_modules.archive import home as _home
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
)
from src.webui_modules.archive.home import (
    _acquire_home_cache,
    _home_cache,
    _home_cache_building,
    _home_cache_condition,
    _home_cache_key,
    _home_cache_key_for_request,
    _load_latest_text_by_member,
    _blog_media_totals,
    _message_media_totals,
    _release_home_cache,
    handle_home,
)
from src.webui_modules.archive.gallery import handle_gallery
from src.webui_modules.archive.letters import handle_letters
from src.webui_modules.archive.messages import handle_messages
from src.webui_modules.static_handler import send_json


def get_blog_db() -> sqlite3.Connection:
    return _common.get_blog_db()


# All HTTP methods share this allowlist. Subhandlers retain their resource and
# account-specific checks; a new route must declare its contract here first.
_READ_ROUTES = frozenset({
    "home", "members", "months", "messages", "calendar", "search",
    "messages/interactions", "messages/starred", "blog_groups", "blog_calendar",
    "blog_authors", "blogs", "blogs/card_status", "blogs/card_file",
    "gallery", "gallery_members", "gallery_years",
})
_ADMIN_POST_ROUTES = frozenset({
    "tags", "retry_download", "messages/backfill", "letters_sync", "letters/favorite",
    "blogs/archive_groups", "blogs/archive_member", "blogs/translate", "blogs/delete_translation",
})
_READER_POST_ROUTES = frozenset({
    "message/interaction", "message/star", "blogs/card", "blogs/furigana",
})


def _authorize_archive(handler, sub: str, guard_fn) -> bool:
    if sub == "avatar":
        # Public roster artwork is also used by the login/admin pages.
        method = "GET"
        need_admin = None if handler.path.startswith("/avatar/") else False
    elif sub == "letters":
        method, need_admin = "GET", True
    elif sub in _ADMIN_POST_ROUTES:
        method, need_admin = "POST", True
    elif sub in _READER_POST_ROUTES:
        method, need_admin = "POST", False
    elif sub in _READ_ROUTES or sub.startswith(("media/", "blog_media/")):
        method, need_admin = "GET", False
    else:
        _send_json_resp(handler, {"ok": False, "errors": ["未知路径"]}, 404)
        return False
    if need_admin is not None and not guard_fn(need_admin=need_admin):
        return False
    if handler.command != method:
        handler._pending_headers = [*getattr(handler, "_pending_headers", []), ("Allow", method)]
        _send_json_resp(handler, {"ok": False, "errors": [f"请使用 {method} 请求"]}, 405)
        return False
    return True


def handle_archive(handler, sub: str, guard_fn, read_body_json_fn) -> None:
    """归档路由入口；首页聚合请求使用单飞缓存避免并发重复计算。"""
    if not _authorize_archive(handler, sub, guard_fn):
        return
    if sub == "home":
        cache_key = _home._home_cache_key_for_request()
        cached = _home._acquire_home_cache(cache_key)
        if cached is not None:
            _send_json_resp(handler, cached)
            return
        try:
            _handle_archive_impl(handler, sub, guard_fn, read_body_json_fn)
        finally:
            _home._release_home_cache()
        return
    _handle_archive_impl(handler, sub, guard_fn, read_body_json_fn)


def warm_home_cache() -> bool:
    """后台预热首页聚合缓存；失败不影响 WebUI 启动。"""
    class _WarmupHandler:
        path = "/api/archive/home"
        command = "GET"
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

    # 4. 美图画廊
    if handle_gallery(handler, sub, guard_fn, read_body_json_fn):
        return

    # 5. 首页聚合
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
    "_blog_media_totals",
    "_message_media_totals",
    "_send_json_resp",
    "_set_blog_translation_state",
    "get_blog_db",
    "handle_archive",
    "_handle_archive_impl",
    "record_event",
    "warm_home_cache",
]
