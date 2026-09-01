"""
src/webui_modules/archive_handlers.py — WebUI 归档数据路由与业务处理服务

提供：
  1. 消息归档：成员列表、月份列表、消息分页、日历热力图、FTS5 全文搜索、手动翻译与打标回填
  2. 官方博客：博客列表、分页、分组统计、按日历筛选、文章详情、单成员全量补抓、日文振假名与手动翻译
  3. 粉丝信件：信件列表、收藏状态切换与批量同步
  4. 首页聚合：全站动态瀑布流、时光隧道、写真画廊与统计指标
  5. 媒体流服务：头像、博客图片与消息图片分发调度
"""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import random
import sqlite3
import threading
from urllib.parse import parse_qs, quote, unquote

import config.config as cfg
from src import archive as _archive
from src.webui_modules.media_service import serve_file_range
from src.webui_modules.static_handler import send_json
from src.audit import record_event

BLOG_IMAGE_DIR = Path("data/blog_images")
_blog_db_local = threading.local()

_home_cache: dict | None = None
_home_cache_key: tuple[float, float, str] | None = None
_home_cache_condition = threading.Condition()
_home_cache_building = False
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


def _blog_calendar_days(db: sqlite3.Connection, group: str, author: str = "") -> dict[str, int]:
    """按博客分组/作者统计有效日期的文章数。

    博客抓取时已将日期规范化为本地时间字符串（``YYYY-MM-DD HH:MM``），
    日历只取前 10 位日期，不做时区转换。格式异常的历史记录会被忽略，
    避免污染前端日历键。
    """
    where = [
        "group_key = ?",
        "date IS NOT NULL",
        "length(substr(date, 1, 10)) = 10",
        "substr(date, 5, 1) = '-'",
        "substr(date, 8, 1) = '-'",
    ]
    params: list[str] = [group]
    if author:
        normalized_author = author.replace(" ", "").replace("　", "").replace("_", "")
        where.append("REPLACE(REPLACE(REPLACE(author, ' ', ''), '　', ''), '_', '') = ?")
        params.append(normalized_author)

    rows = db.execute(
        "SELECT substr(date, 1, 10) AS day, COUNT(*) AS count "
        "FROM blog_posts WHERE " + " AND ".join(where) +
        " GROUP BY substr(date, 1, 10) ORDER BY day",
        params,
    ).fetchall()

    days: dict[str, int] = {}
    for row in rows:
        day = str(row[0] or "")
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            continue
        days[day] = int(row[1] or 0)
    return days


def _send_json_resp(handler, obj: dict, code: int = 200) -> None:
    if hasattr(handler, "_send_json") and callable(getattr(handler, "_send_json")):
        try:
            handler._send_json(obj, code)
        except TypeError:
            handler._send_json(obj)
    else:
        send_json(handler, obj, code)


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


def _load_latest_text_by_member(
    db: sqlite3.Connection,
    member_names: list[str],
    limit: int = 4,
) -> dict[str, list[dict]]:
    """一次查询每个成员的最新文本，避免首页聚合产生成员级 N+1 查询。"""
    latest: dict[str, list[dict]] = {name: [] for name in member_names}
    if not member_names or limit <= 0:
        return latest

    try:
        rows = db.execute(
            """
            SELECT id, member_dir, text, translation, published_at, updated_at
            FROM (
                SELECT id, member_dir, text, translation, published_at, updated_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY member_dir
                           ORDER BY COALESCE(published_at, updated_at) DESC, id DESC
                       ) AS row_num
                FROM messages
                WHERE type = 'text'
                  AND text IS NOT NULL
                  AND trim(text) != ''
            )
            WHERE row_num <= ?
            ORDER BY member_dir, COALESCE(published_at, updated_at) DESC, id DESC
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        # 兼容旧版 SQLite；现代 SQLite 使用上面的窗口查询，旧版退回单次成员查询。
        for name in member_names:
            rows = db.execute(
                """
                SELECT id, text, translation, published_at, updated_at
                FROM messages
                WHERE member_dir = ?
                  AND type = 'text'
                  AND text IS NOT NULL
                  AND trim(text) != ''
                ORDER BY COALESCE(published_at, updated_at) DESC, id DESC
                LIMIT ?
                """,
                (name, limit),
            ).fetchall()
            latest[name] = [
                {
                    "id": row[0],
                    "text": row[1] or "",
                    "translation": row[2] or "",
                    "published_at": row[3] or row[4] or "",
                }
                for row in rows
            ]
        return latest

    for row in rows:
        latest.setdefault(row[1], []).append({
            "id": row[0],
            "text": row[2] or "",
            "translation": row[3] or "",
            "published_at": row[4] or row[5] or "",
        })
    return latest


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
    except Exception as exc:  # 预热失败不能影响主服务启动
        from src.logger import log_all
        log_all(f"⚠️ 首页缓存预热跳过: {type(exc).__name__}: {exc}", is_debug=True)
        return False


def _handle_archive_impl(handler, sub: str, guard_fn, read_body_json_fn) -> None:
    """归档子路由统一派发。"""
    qs = parse_qs(handler.path.partition("?")[2])

    def qp(key: str, default: str = "") -> str:
        return (qs.get(key) or [default])[0]

    # 1. 头像服务
    if sub == "avatar":
        name = qp("name")
        group = qp("group")
        from src import avatar_manager
        rel_path = avatar_manager.get_member_avatar_path(name, group)
        if rel_path:
            full_path = Path("data/avatars") / rel_path
            if full_path.exists():
                ext = full_path.suffix.lower()
                ctype = "image/jpeg"
                if ext == ".png":
                    ctype = "image/png"
                elif ext == ".webp":
                    ctype = "image/webp"
                try:
                    data = full_path.read_bytes()
                    handler.send_response(200)
                    handler.send_header("Content-Type", ctype)
                    handler.send_header("Content-Length", str(len(data)))
                    handler.send_header("Cache-Control", "public, max-age=2592000")
                    handler.end_headers()
                    handler.wfile.write(data)
                    return
                except Exception:
                    pass
        _send_json_resp(handler, {"ok": False, "errors": ["Avatar not found"]}, 404)
        return

    # 2. 成员列表
    if sub == "members":
        members = []
        monitor_map = {}
        for idx, m in enumerate(getattr(cfg, "MONITOR_LIST", [])):
            norm = m.get("m_name", "").replace(" ", "").replace("　", "").replace("_", "")
            monitor_map[norm] = {
                "display": m.get("m_name", ""),
                "group": m.get("group_type", ""),
            }

        from src import avatar_manager
        avatar_map = avatar_manager.get_member_avatar_map()
        from src.sakamichi_roster import get_member_sort_tuple

        raw_members = _archive.list_members()
        for name in raw_members:
            months = _archive.list_months(name)
            norm = name.replace(" ", "").replace("　", "").replace("_", "")
            info = monitor_map.get(norm) or {}
            display = info.get("display") or name.replace("_", " ")
            group = info.get("group") or _archive.infer_member_group(name)
            avatar = avatar_map.get(f"{group}:{norm}") or avatar_map.get(norm) or ""
            members.append({
                "name": name,
                "display": display,
                "group": group,
                "avatar": avatar,
                "months": len(months),
                "total": sum(m["count"] for m in months),
            })

        members.sort(key=lambda x: get_member_sort_tuple(x["group"], x["name"]))
        _send_json_resp(handler, {"ok": True, "members": members})
        return

    # 3. 月份列表
    if sub == "months":
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return
        _send_json_resp(handler, {"ok": True, "member": member, "months": _archive.list_months(member)})
        return

    # 4. 消息分页
    if sub == "messages":
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return
        try:
            year, month = int(qp("year")), int(qp("month"))
            page = max(1, int(qp("page", "1")))
            per_page = min(200, max(1, int(qp("per_page", "50"))))
        except ValueError:
            _send_json_resp(handler, {"ok": False, "errors": ["year/month/page 必须是数字"]}, 400)
            return

        type_filter = qp("type")
        msgs = _archive.load_month(member, year, month)
        if type_filter:
            if type_filter not in ARCHIVE_TYPES:
                _send_json_resp(handler, {"ok": False, "errors": [f"未知类型: {type_filter!r}"]}, 400)
                return
            wanted = {"picture", "image"} if type_filter in ("picture", "image") else {type_filter}
            msgs = [m for m in msgs if m.get("type") in wanted]

        show_auto_tags = bool(getattr(cfg, "ENABLE_IMAGE_TAGGING", False))
        total = len(msgs)
        start = (page - 1) * per_page
        grp = _archive.infer_member_group(member)
        slim = [{
            "id": m.get("id"),
            "type": m.get("type"),
            "text": m.get("text", ""),
            "translation": m.get("_translation", ""),
            "tags": m.get("_tags", "") if show_auto_tags else "",
            "custom_tags": m.get("_custom_tags", ""),
            "published_at": m.get("published_at") or m.get("updated_at", ""),
            "upload_at": _archive.extract_upload_time(m),
            "group": grp,
            "media_url": f"/api/archive/media/{member}/{m['_local_file']}" if m.get("_local_file") else None,
            "download_failed": bool(m.get("_download_failed")),
            "w": m.get("thumbnail_width"),
            "h": m.get("thumbnail_height"),
        } for m in msgs[start:start + per_page]]

        _send_json_resp(handler, {
            "ok": True, "member": member, "group": grp, "year": year, "month": month,
            "total": total, "page": page,
            "total_pages": max(1, -(-total // per_page)), "messages": slim,
        })
        return

    # 5. 日历热力图
    if sub == "calendar":
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return
        type_filter = qp("type")
        wanted = None
        if type_filter:
            if type_filter not in ARCHIVE_TYPES:
                _send_json_resp(handler, {"ok": False, "errors": [f"未知类型: {type_filter!r}"]}, 400)
                return
            wanted = {"picture", "image"} if type_filter in ("picture", "image") else {type_filter}
        _send_json_resp(handler, {"ok": True, "member": member, "days": _archive.day_counts(member, type_filter=wanted)})
        return

    # 6. FTS5 全文搜索
    if sub == "search":
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return
        query = qp("q").strip()
        if not query:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少搜索关键词 q"]}, 400)
            return
        if len(query) > 100:
            _send_json_resp(handler, {"ok": False, "errors": ["搜索关键词不能超过 100 个字符"]}, 400)
            return
        try:
            page = max(1, int(qp("page", "1")))
            per_page = min(200, max(1, int(qp("per_page", "50"))))
        except ValueError:
            _send_json_resp(handler, {"ok": False, "errors": ["page 必须是数字"]}, 400)
            return

        type_filter = qp("type")
        wanted = None
        if type_filter:
            if type_filter not in ARCHIVE_TYPES:
                _send_json_resp(handler, {"ok": False, "errors": [f"未知类型: {type_filter!r}"]}, 400)
                return
            wanted = {"picture", "image"} if type_filter in ("picture", "image") else {type_filter}

        hits = _archive.search(member, query, type_filter=wanted)
        show_auto_tags = bool(getattr(cfg, "ENABLE_IMAGE_TAGGING", False))
        grp = _archive.infer_member_group(member)
        total = len(hits)
        start = (page - 1) * per_page
        slim = [{
            "id": m.get("id"),
            "type": m.get("type"),
            "text": m.get("text", ""),
            "translation": m.get("_translation", ""),
            "tags": m.get("_tags", "") if show_auto_tags else "",
            "custom_tags": m.get("_custom_tags", ""),
            "published_at": m.get("published_at") or m.get("updated_at", ""),
            "upload_at": _archive.extract_upload_time(m),
            "group": grp,
            "media_url": f"/api/archive/media/{member}/{m['_local_file']}" if m.get("_local_file") else None,
            "download_failed": bool(m.get("_download_failed")),
            "w": m.get("thumbnail_width"),
            "h": m.get("thumbnail_height"),
            "year": m.get("_year"),
            "month": m.get("_month"),
        } for m in hits[start:start + per_page]]

        _send_json_resp(handler, {
            "ok": True, "member": member, "group": grp, "q": query, "total": total,
            "page": page, "total_pages": max(1, -(-total // per_page)),
            "capped": total >= 500, "messages": slim,
        })
        return

    # 7. 自定义标签
    if sub == "tags":
        if not guard_fn(need_admin=True):
            return
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return
        body = read_body_json_fn()
        if body is None:
            return
        if not isinstance(body, dict):
            _send_json_resp(handler, {"ok": False, "errors": ["请求体必须是 JSON 对象"]}, 400)
            return
        msg_id = str(body.get("id") or "")
        raw_tags = body.get("custom_tags", "")
        if not isinstance(raw_tags, str):
            _send_json_resp(handler, {"ok": False, "errors": ["custom_tags 必须是字符串"]}, 400)
            return
        tags = raw_tags.strip()
        if len(tags) > 500:
            _send_json_resp(handler, {"ok": False, "errors": ["custom_tags 不能超过 500 个字符"]}, 400)
            return
        try:
            year = int(body.get("year"))
            month = int(body.get("month"))
        except (TypeError, ValueError):
            _send_json_resp(handler, {"ok": False, "errors": ["year/month 必须是数字"]}, 400)
            return
        if not msg_id or not 1 <= month <= 12 or not 1 <= year <= 9999:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少 id/year/month"]}, 400)
            return
        with _archive_write_lock:
            msgs = _archive.load_month(member, year, month)
            found = False
            for m in msgs:
                if str(m.get("id", "")) == msg_id:
                    m["_custom_tags"] = tags
                    found = True
                    break
            if not found:
                _send_json_resp(handler, {"ok": False, "errors": [f"消息 {msg_id} 不存在"]}, 404)
                return
            json_path = (_archive.archive_root() / member / f"{year:04d}" / f"{month:02d}" / "messages.json")
            if not json_path.is_file():
                _send_json_resp(handler, {"ok": False, "errors": ["归档文件不存在"]}, 404)
                return
            tmp = json_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(msgs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, json_path)
        try:
            from src.webui_modules.auth_handlers import current_user
            user = current_user(handler) or {}
            source_ip = handler.client_address[0] if getattr(handler, "client_address", None) else "?"
            record_event("archive.custom_tags", outcome="success", actor=user.get("username"),
                         source_ip=source_ip, target=f"{member}/{year:04d}-{month:02d}/{msg_id}",
                         details={"tag_count": len([tag for tag in tags.split(",") if tag.strip()])})
        except Exception:
            pass
        _send_json_resp(handler, {"ok": True, "id": msg_id, "custom_tags": tags})
        return

    # 8. 粉丝信件
    if sub == "letters":
        if not guard_fn(need_admin=True):
            return
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少成员参数 member"]}, 400)
            return
        letters = _archive.get_archive_letters(member)
        grp = _archive.infer_member_group(member)
        slim = []
        for item in letters:
            loc = item.get("local_file") or ""
            if loc:
                rel_path = loc[len(member) + 1:] if loc.startswith(member + "/") else loc
                media_url = f"/api/archive/media/{member}/{rel_path}"
            else:
                media_url = None

            slim.append({
                "id": item.get("id"),
                "group_id": item.get("group_id"),
                "member_name": item.get("member_name"),
                "member_dir": item.get("member_dir"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "text": item.get("text", ""),
                "file_url": item.get("file_url"),
                "media_url": media_url,
                "thumbnail_url": item.get("thumbnail_url"),
                "is_favorite": bool(item.get("is_favorite")),
            })
        _send_json_resp(handler, {"ok": True, "member": member, "group": grp, "total": len(slim), "letters": slim})
        return

    # 8.1 粉丝信件同步
    if sub == "letters_sync":
        if not guard_fn(need_admin=True):
            return
        raw_m = qp("member")
        if not raw_m:
            body = read_body_json_fn()
            if body and isinstance(body, dict):
                raw_m = body.get("member")
        if not raw_m:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少成员参数 member"]}, 400)
            return
        member = _archive.member_dir_name(raw_m)
        target_mem = None
        norm_raw = raw_m.replace(" ", "").replace("　", "").replace("_", "").lower()
        for m in getattr(cfg, "MONITOR_LIST", []):
            m_name = m.get("m_name") or m.get("name", "")
            norm_m = m_name.replace(" ", "").replace("　", "").replace("_", "").lower()
            if norm_m == norm_raw or _archive.member_dir_name(m_name) == member:
                target_mem = m
                break
        if not target_mem:
            target_mem = {"name": raw_m, "m_name": raw_m}

        import tools.archive_letters as _al
        import httpx
        import asyncio
        import concurrent.futures

        async def _do_sync():
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                _archive.initialize(client)
                return await _al.sync_letters_for_member(target_mem, client)

        try:
            try:
                _loop = asyncio.get_running_loop()
            except RuntimeError:
                _loop = None

            if _loop and _loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                    tot, nw = _pool.submit(asyncio.run, _do_sync()).result(timeout=60)
            else:
                tot, nw = asyncio.run(_do_sync())
            _send_json_resp(handler, {"ok": True, "member": member, "total": tot, "new": nw, "count": tot})
        except Exception as e:
            _send_json_resp(handler, {"ok": False, "errors": [f"同步信件异常: {e}"]}, 500)
        return

    # 8.2 粉丝信件收藏切换
    if sub == "letters/favorite":
        if not guard_fn(need_admin=True):
            return
        body = read_body_json_fn() or {}
        letter_id = body.get("id")
        is_fav = bool(body.get("is_favorite", False))
        if not letter_id:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少 letter id"]}, 400)
            return
        _archive.set_letter_favorite(int(letter_id), is_fav)
        _send_json_resp(handler, {"ok": True, "id": int(letter_id), "is_favorite": is_fav})
        return

    # 9. 博客分组统计
    if sub == "blog_groups":
        try:
            rows = get_blog_db().execute("""
                SELECT group_key, COUNT(*), MIN(date), MAX(date)
                FROM blog_posts
                GROUP BY group_key
                ORDER BY group_key
            """).fetchall()
        except (sqlite3.Error, OSError):
            _send_json_resp(handler, {"ok": False, "errors": ["博客分组暂时不可用"]}, 500)
            return
        groups = [{
            "key": row[0],
            "total": int(row[1] or 0),
            "first_date": row[2] or "",
            "last_date": row[3] or "",
        } for row in rows]
        _send_json_resp(handler, {"ok": True, "groups": groups})
        return

    # 10. 博客日历热力图
    if sub == "blog_calendar":
        group = qp("group", "hinatazaka")
        author = qp("author", "").strip()
        try:
            days = _blog_calendar_days(get_blog_db(), group, author)
        except (sqlite3.Error, OSError):
            _send_json_resp(handler, {"ok": False, "errors": ["博客日历暂时不可用"]}, 500)
            return
        date_keys = sorted(days)
        _send_json_resp(handler, {
            "ok": True,
            "group": group,
            "author": author,
            "days": days,
            "total": sum(days.values()),
            "first_date": date_keys[0] if date_keys else "",
            "last_date": date_keys[-1] if date_keys else "",
        })
        return

    # 11. 博客作者列表
    if sub == "blog_authors":
        group = qp("group", "hinatazaka")
        authors = []
        try:
            db = get_blog_db()
            from src import avatar_manager
            from src.sakamichi_roster import get_author_sort_tuple
            avatar_map = avatar_manager.get_member_avatar_map()
            raw_authors = db.execute("""
                SELECT author, COUNT(*)
                FROM blog_posts WHERE group_key=? AND author != '' AND author IS NOT NULL
                GROUP BY author
            """, (group,)).fetchall()
            for r in raw_authors:
                if r[0] and str(r[0]).strip():
                    a_name = str(r[0]).strip()
                    norm_a = a_name.replace(" ", "").replace("　", "").replace("_", "")
                    avatar = avatar_map.get(f"{group}:{norm_a}") or avatar_map.get(norm_a) or ""
                    sort_key = get_author_sort_tuple(group, a_name)
                    authors.append({"name": a_name, "total": r[1], "avatar": avatar, "_sort": sort_key})
            authors.sort(key=lambda x: x["_sort"])
            for a_item in authors:
                a_item.pop("_sort", None)
        except Exception:
            pass
        _send_json_resp(handler, {"ok": True, "group": group, "authors": authors})
        return

    # 12. 博客列表与详情
    if sub == "blogs":
        blog_id = qp("id")
        if blog_id:
            try:
                db = get_blog_db()
                r = db.execute("SELECT * FROM blog_posts WHERE id=?", (blog_id,)).fetchone()
                if r:
                    d = dict(r)
                    d["images_json"] = d.get("images_json") or "[]"
                    d["image_paths_json"] = d.get("image_paths_json") or "[]"
                    _send_json_resp(handler, {"ok": True, "post": d})
                    return
                _send_json_resp(handler, {"ok": False, "errors": ["博客不存在"]}, 404)
                return
            except Exception as e:
                _send_json_resp(handler, {"ok": False, "errors": [str(e)]}, 500)
                return

        group = qp("group", "hinatazaka")
        author = qp("author", "")
        date_filter = qp("date", "")
        year = int(qp("year", "0") or "0")
        month = int(qp("month", "0") or "0")
        page = max(1, int(qp("page", "1") or "1"))
        per_page = min(100, max(1, int(qp("per_page", "30") or "30")))
        q = qp("q", "")
        posts = []
        total = 0
        try:
            db = get_blog_db()
            where = "WHERE group_key=?"
            params: list = [group]
            if author:
                norm_author = author.replace(" ", "").replace("　", "").replace("_", "")
                where += " AND REPLACE(REPLACE(REPLACE(author, ' ', ''), '　', ''), '_', '') = ?"
                params.append(norm_author)
            if date_filter:
                where += " AND substr(date,1,10)=?"
                params.append(date_filter)
            elif year and month:
                where += " AND substr(date,1,7)=?"
                params.append(f"{year:04d}-{month:02d}")
            if q:
                where += " AND (title LIKE ? OR body_text LIKE ? OR translation LIKE ?)"
                q_like = f"%{q}%"
                params.extend([q_like, q_like, q_like])
            total = db.execute(f"SELECT COUNT(*) FROM blog_posts {where}", params).fetchone()[0]

            has_hero_mode = (not q and not date_filter and not (year and month))
            if has_hero_mode:
                limit = 25 if page == 1 else 24
                offset = 0 if page == 1 else 25 + (page - 2) * 24
                total_pages = 1 if total <= 25 else 1 + (total - 25 + 24 - 1) // 24
            else:
                limit = per_page
                offset = (page - 1) * per_page
                total_pages = max(1, (total + per_page - 1) // per_page)

            sql = f"SELECT * FROM blog_posts {where} ORDER BY date DESC LIMIT ? OFFSET ?"
            rows = db.execute(sql, params + [limit, offset]).fetchall()
            for r in rows:
                d = dict(r)
                d["images_json"] = d.get("images_json") or "[]"
                d["image_paths_json"] = d.get("image_paths_json") or "[]"
                d["content_json"] = d.get("content_json") or "[]"
                d["translation_model"] = d.get("translation_model") or ""
                posts.append(d)
        except Exception:
            pass
        _send_json_resp(handler, {
            "ok": True, "group": group, "posts": posts,
            "total": total, "page": page, "total_pages": total_pages,
        })
        return

    # 13. 博客振假名
    if sub == "blogs/furigana":
        if handler.command != "POST":
            _send_json_resp(handler, {"ok": False, "msg": "Method not allowed"}, 405)
            return
        body_data = read_body_json_fn() or {}
        blog_id = body_data.get("id")
        raw_html = body_data.get("html")
        raw_title = body_data.get("title")

        try:
            from src import furigana
            if blog_id:
                db = get_blog_db()
                row = db.execute("SELECT * FROM blog_posts WHERE id = ?", (int(blog_id),)).fetchone()
                if not row:
                    _send_json_resp(handler, {"ok": False, "msg": "未找到该博客"}, 404)
                    return
                row = dict(row)
                f_title = furigana.add_furigana_to_text(row.get("title") or "")
                f_html = furigana.add_furigana_to_html(row.get("body_html") or "")
                f_content_json = None
                if row.get("content_json") and row["content_json"] != "[]":
                    try:
                        blocks = json.loads(row["content_json"])
                        f_blocks = furigana.add_furigana_to_blocks(blocks)
                        f_content_json = json.dumps(f_blocks, ensure_ascii=False)
                    except Exception:
                        pass

                _send_json_resp(handler, {
                    "ok": True, "id": blog_id, "title": f_title,
                    "furigana_html": f_html, "furigana_content_json": f_content_json,
                })
                return

            if raw_html:
                f_html = furigana.add_furigana_to_html(str(raw_html))
                f_title = furigana.add_furigana_to_text(str(raw_title)) if raw_title else ""
                _send_json_resp(handler, {"ok": True, "title": f_title, "furigana_html": f_html})
                return

            _send_json_resp(handler, {"ok": False, "msg": "缺少 id 或 html 参数"}, 400)
        except Exception as e:
            _send_json_resp(handler, {"ok": False, "msg": f"生成振假名异常: {e}"}, 500)
        return

    # 14. 媒体流服务
    if sub.startswith("blog_media/"):
        rel_str = unquote(sub[len("blog_media/"):].replace("\\", "/"))
        rel = Path(rel_str)
        full = (BLOG_IMAGE_DIR / rel).resolve()
        if BLOG_IMAGE_DIR.resolve() not in full.parents and full != BLOG_IMAGE_DIR.resolve():
            _send_json_resp(handler, {"ok": False, "errors": ["非法路径"]}, 403)
            return
        if not full.is_file():
            _send_json_resp(handler, {"ok": False, "errors": ["媒体不存在"]}, 404)
            return
        serve_file_range(handler, full)
        return

    if sub.startswith("media/"):
        rest = unquote(sub[len("media/"):])
        raw_member, _, rel = rest.partition("/")
        member = _archive.member_dir_name(raw_member) if raw_member else ""
        if not member or member not in _archive.list_members() or not rel:
            _send_json_resp(handler, {"ok": False, "errors": ["媒体不存在"]}, 404)
            return
        member_root = (_archive.archive_root() / member).resolve()
        full = (member_root / rel).resolve()
        if member_root not in full.parents:
            _send_json_resp(handler, {"ok": False, "errors": ["非法路径"]}, 403)
            return
        if not full.is_file():
            _send_json_resp(handler, {"ok": False, "errors": ["媒体不存在"]}, 404)
            return
        serve_file_range(handler, full)
        return

    # 15. 首页仪表盘聚合数据
    if sub == "home":
        global _home_cache, _home_cache_key
        cache_key = _home_cache_key_for_request()
        today_str = cache_key[2]
        if _home_cache is not None and _home_cache_key == cache_key:
            _send_json_resp(handler, _home_cache)
            return

        random.seed(today_str)
        monitor_names = {}
        for m in getattr(cfg, "MONITOR_LIST", []):
            norm = m.get("m_name", "").replace(" ", "").replace("　", "").replace("_", "")
            monitor_names[norm] = m.get("m_name", "")

        now_dt = datetime.now()
        tomorrow_str = (now_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        db = _archive.init_db()
        archive_members = _archive.list_members()
        from src import avatar_manager
        avatar_map = avatar_manager.get_member_avatar_map()

        members = []
        today_msg_cnt = 0
        this_week_msgs = 0
        last_week_msgs = 0
        months_by_member: dict[str, list[dict]] = {}
        types_by_member: dict[str, dict[str, int]] = {}
        latest_msgs_by_member: dict[str, list[dict]] = {}

        if db:
            try:
                r_td = db.execute(
                    """
                    SELECT COUNT(*) FROM messages
                    WHERE (published_at >= ? AND published_at < ?)
                       OR (updated_at >= ? AND updated_at < ?)
                    """,
                    (today_str, tomorrow_str, today_str, tomorrow_str),
                ).fetchone()
                today_msg_cnt = r_td[0] if r_td else 0
                w0 = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")
                w1 = (now_dt - timedelta(days=14)).strftime("%Y-%m-%d")
                r_this = db.execute("SELECT COUNT(*) FROM messages WHERE published_at >= ?", (w0,)).fetchone()
                this_week_msgs = r_this[0] if r_this else 0
                r_last = db.execute("SELECT COUNT(*) FROM messages WHERE published_at >= ? AND published_at < ?", (w1, w0)).fetchone()
                last_week_msgs = r_last[0] if r_last else 0

                for r_m in db.execute("SELECT member_dir, year, month, COUNT(*) FROM messages GROUP BY member_dir, year, month ORDER BY member_dir, year DESC, month DESC").fetchall():
                    md, y, mo, cnt = r_m[0], r_m[1], r_m[2], r_m[3]
                    months_by_member.setdefault(md, []).append({"year": y, "month": mo, "count": cnt})

                for r_tc in db.execute("SELECT member_dir, type, COUNT(*) FROM messages GROUP BY member_dir, type").fetchall():
                    md, mtype, cnt = r_tc[0], r_tc[1] or "text", r_tc[2]
                    types_by_member.setdefault(md, {})[mtype] = cnt

                latest_msgs_by_member = _load_latest_text_by_member(db, archive_members, limit=4)
            except Exception:
                pass

        for name in archive_members:
            months = months_by_member.get(name) or _archive.list_months(name)
            total = sum(m["count"] for m in months)
            type_counts = types_by_member.get(name, {})
            monthly = [{"year": mo["year"], "month": mo["month"], "count": mo["count"]} for mo in months[:24]]
            first_date, last_date = "", ""
            if months:
                last = months[0]
                first = months[-1]
                first_date = f"{first['year']:04d}/{first['month']:02d}"
                last_date = f"{last['year']:04d}/{last['month']:02d}"

            stats = {
                "total": total, "months": len(months),
                "pictures": type_counts.get("picture", 0) + type_counts.get("image", 0),
                "videos": type_counts.get("video", 0), "voices": type_counts.get("voice", 0),
                "texts": type_counts.get("text", 0), "first_date": first_date, "last_date": last_date,
            }
            if months:
                stats["this_month"] = months[0]["count"]

            norm = name.replace(" ", "").replace("　", "").replace("_", "")
            display = monitor_names.get(norm) or name.replace("_", " ")
            group = _archive.infer_member_group(name)
            avatar = avatar_map.get(f"{group}:{norm}") or avatar_map.get(norm) or ""
            members.append({
                "name": name, "display": display, "group": group, "avatar": avatar,
                "stats": stats, "monthly": monthly, "days": {},
            })

        from src.sakamichi_roster import get_member_sort_tuple
        members.sort(key=lambda x: get_member_sort_tuple(x["group"], x["name"]))

        GROUP_INFO = {
            "nogizaka": {"name": "乃木坂46", "icon": "💜", "color": "#8b5cf6"},
            "sakurazaka": {"name": "樱坂46", "icon": "🌸", "color": "#ec4899"},
            "hinatazaka": {"name": "日向坂46", "icon": "🩵", "color": "#06b6d4"},
        }
        blog_groups = []
        total_blogs = 0
        total_blog_authors = 0
        recent_blogs = []
        blog_pics = []
        today_blog_cnt = 0
        blog_this_week = 0

        def _encode_blog_media_url(rel_p: str) -> str:
            if not rel_p:
                return ""
            parts = rel_p.replace("\\", "/").strip("/").split("/")
            encoded_parts = [quote(p) for p in parts]
            return "/api/archive/blog_media/" + "/".join(encoded_parts)

        blog_db = get_blog_db()
        if blog_db:
            try:
                total_blogs = blog_db.execute("SELECT COUNT(*) FROM blog_posts").fetchone()[0]
                total_blog_authors = blog_db.execute("SELECT COUNT(DISTINCT author) FROM blog_posts").fetchone()[0]
                for gkey, gmeta in GROUP_INFO.items():
                    row = blog_db.execute("SELECT COUNT(*), COUNT(DISTINCT author), MIN(date), MAX(date) FROM blog_posts WHERE group_key=?", (gkey,)).fetchone()
                    count = row[0] if row else 0
                    if count > 0:
                        lp_row = blog_db.execute("SELECT id, author, title, date, image_paths_json FROM blog_posts WHERE group_key=? ORDER BY date DESC LIMIT 1", (gkey,)).fetchone()
                        latest_post = None
                        if lp_row:
                            lp = dict(lp_row)
                            imgs = json.loads(lp.get("image_paths_json") or "[]")
                            first_img = imgs[0].replace("\\", "/") if imgs and imgs[0] else ""
                            cover = _encode_blog_media_url(first_img) if first_img else ""
                            latest_post = {"id": lp["id"], "author": lp["author"], "title": lp["title"], "date": lp["date"], "cover": cover}
                        blog_groups.append({
                            "key": gkey, "name": gmeta["name"], "icon": gmeta["icon"], "color": gmeta["color"],
                            "total": count, "author_count": row[1],
                            "first_date": (row[2] or "")[:7].replace("-", "/"),
                            "last_date": (row[3] or "")[:7].replace("-", "/"),
                            "latest_post": latest_post,
                        })

                for r in blog_db.execute("SELECT id, group_key, author, title, date, image_paths_json FROM blog_posts ORDER BY date DESC LIMIT 6").fetchall():
                    bp = dict(r)
                    imgs = json.loads(bp.get("image_paths_json") or "[]")
                    first_img = imgs[0].replace("\\", "/") if imgs and imgs[0] else ""
                    cover = _encode_blog_media_url(first_img) if first_img else ""
                    gname = GROUP_INFO.get(bp["group_key"], {}).get("name", bp["group_key"])
                    gicon = GROUP_INFO.get(bp["group_key"], {}).get("icon", "📝")
                    recent_blogs.append({
                        "type": "blog", "id": bp["id"], "group_key": bp["group_key"],
                        "group_name": gname, "group_icon": gicon, "author": bp["author"],
                        "title": bp["title"], "date": bp["date"], "cover": cover, "has_images": len(imgs) > 0,
                    })
                    if cover:
                        blog_pics.append({
                            "type": "blog", "id": bp["id"], "group_key": bp["group_key"], "member": bp["author"],
                            "member_display": f"{gicon} {gname} · {bp['author']}", "text": bp["title"], "url": cover,
                            "published_at": bp["date"],
                            "year": int(bp["date"][:4]) if len(bp["date"]) >= 4 and bp["date"][:4].isdigit() else 2026,
                            "month": int(bp["date"][5:7]) if len(bp["date"]) >= 7 and bp["date"][5:7].isdigit() else 8,
                        })

                r_b_td = blog_db.execute(
                    "SELECT COUNT(*) FROM blog_posts WHERE date >= ? AND date < ?",
                    (today_str, tomorrow_str),
                ).fetchone()
                today_blog_cnt = r_b_td[0] if r_b_td else 0
                week_ago_str = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")
                r_b_wk = blog_db.execute("SELECT COUNT(*) FROM blog_posts WHERE date >= ?", (week_ago_str,)).fetchone()
                blog_this_week = r_b_wk[0] if r_b_wk else 0
            except Exception:
                pass

        def _ym(utc_str: str) -> tuple[int, int]:
            try:
                return (int(utc_str[:4]), int(utc_str[5:7]))
            except (ValueError, IndexError):
                return (2026, 1)

        msg_pics = []
        if db:
            try:
                for row in db.execute("SELECT id, member_name, text, local_file, published_at, updated_at, raw_json FROM messages WHERE type IN ('picture','image') AND local_file IS NOT NULL AND local_file != '' ORDER BY published_at DESC LIMIT 12").fetchall():
                    rj = json.loads(row[6]) if row[6] else {}
                    pub = row[4] or row[5] or ""
                    norm_m = row[1].replace(" ", "").replace("　", "").replace("_", "")
                    disp = monitor_names.get(norm_m) or row[1].replace("_", " ")
                    canonical_m = _archive.member_dir_name(row[1])
                    msg_pics.append({
                        "type": "msg", "member": canonical_m, "member_display": disp,
                        "id": row[0], "text": row[2] or "",
                        "url": f"/api/archive/media/{canonical_m}/{row[3]}",
                        "w": rj.get("thumbnail_width"), "h": rj.get("thumbnail_height"),
                        "published_at": pub, "year": _ym(pub)[0], "month": _ym(pub)[1],
                    })
            except Exception:
                pass

        agg_pics = sorted(msg_pics + blog_pics[:6], key=lambda x: x.get("published_at", ""), reverse=True)
        agg_msgs = []
        member_display_by_name = {m["name"]: m["display"] for m in members}
        for name in archive_members:
            for msg in latest_msgs_by_member.get(name, []):
                agg_msgs.append({
                    "type": "msg", "member": name, "member_display": member_display_by_name.get(name, name),
                    "id": msg["id"], "text": msg["text"], "translation": msg.get("translation", ""),
                    "published_at": msg.get("published_at", ""),
                    "year": _ym(msg.get("published_at", ""))[0], "month": _ym(msg.get("published_at", ""))[1],
                })

        for b in recent_blogs[:4]:
            agg_msgs.append({
                "type": "blog", "group_key": b["group_key"], "group_name": b["group_name"],
                "group_icon": b["group_icon"], "author": b["author"],
                "member_display": f"{b['group_icon']} {b['group_name']} · {b['author']}",
                "id": b["id"], "text": b["title"], "translation": "", "cover": b.get("cover", ""),
                "published_at": b["date"],
                "year": int(b["date"][:4]) if len(b["date"]) >= 4 and b["date"][:4].isdigit() else 2026,
                "month": int(b["date"][5:7]) if len(b["date"]) >= 7 and b["date"][5:7].isdigit() else 8,
            })

        recent_feed = sorted(agg_msgs, key=lambda x: x.get("published_at", ""), reverse=True)[:8]
        total_messages = sum(m["stats"]["total"] for m in members)
        first_dates = [m["stats"]["first_date"] for m in members if m["stats"]["first_date"]] + [g["first_date"] for g in blog_groups if g["first_date"]]
        last_dates = [m["stats"]["last_date"] for m in members if m["stats"]["last_date"]] + [g["last_date"] for g in blog_groups if g["last_date"]]

        result = {
            "ok": True,
            "summary": {
                "total_messages": total_messages,
                "total_blogs": total_blogs,
                "total_all": total_messages + total_blogs,
                "member_count": len(members),
                "blog_group_count": len(blog_groups),
                "blog_author_count": total_blog_authors,
                "first_date": min(first_dates) if first_dates else "",
                "last_date": max(last_dates) if last_dates else "",
                "last_updated": max((p.get("published_at", "") for p in agg_pics), default=""),
                "today_stats": {"messages": today_msg_cnt, "blogs": today_blog_cnt, "total": today_msg_cnt + today_blog_cnt},
                "week_stats": {"this_week": this_week_msgs + blog_this_week, "last_week": last_week_msgs, "messages_week": this_week_msgs, "blogs_week": blog_this_week},
            },
            "members": members,
            "blog_groups": blog_groups,
            "recent_pics": agg_pics,
            "recent_feed": recent_feed,
            "time_tunnel": recent_feed[:6],
        }
        _home_cache = result
        _home_cache_key = cache_key
        _send_json_resp(handler, result)
        return

    send_json(handler, {"ok": False, "errors": ["未知路径"]}, 404)
