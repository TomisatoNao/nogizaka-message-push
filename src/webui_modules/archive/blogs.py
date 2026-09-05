"""
src/webui_modules/archive/blogs.py — 官方博客阅读、日历、振假名与异步翻译服务
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import parse_qs, unquote
import uuid

import httpx
from src.webui_modules.archive import common as _archive_common
from src.webui_modules.archive.common import (
    BLOG_IMAGE_DIR,
    _blog_media_url,
    _blog_table_columns,
    _send_json_resp,
)
from src.audit import record_event
from src.webui_modules.media_service import serve_file_range

_blog_translation_locks: dict[int, threading.Lock] = {}
_blog_translation_locks_guard = threading.Lock()


def _get_db() -> sqlite3.Connection:
    return _archive_common.get_blog_db()


def _get_translation_lock(blog_id: int) -> threading.Lock:
    return _get_blog_translation_lock(blog_id)


def _set_state(
    db: sqlite3.Connection,
    blog_id: int,
    *,
    status: str,
    error: str = "",
    request_id: str = "",
) -> None:
    _set_blog_translation_state(db, blog_id, status=status, error=error, request_id=request_id)


def _cal_days(db: sqlite3.Connection, group: str, author: str = "") -> dict[str, int]:
    return _blog_calendar_days(db, group, author)


def _get_blog_translation_lock(blog_id: int) -> threading.Lock:
    """返回博客级翻译锁，避免重复点击/多标签页并发覆盖译文。"""
    with _blog_translation_locks_guard:
        lock = _blog_translation_locks.get(blog_id)
        if lock is None:
            lock = threading.Lock()
            _blog_translation_locks[blog_id] = lock
        return lock


def _set_blog_translation_state(
    db: sqlite3.Connection,
    blog_id: int,
    *,
    status: str,
    error: str = "",
    request_id: str = "",
) -> None:
    """尽力写入翻译状态；旧版数据库缺列时不影响主业务。"""
    columns = _blog_table_columns(db)
    updates: list[str] = []
    values: list[str] = []
    if "translation_status" in columns:
        updates.append("translation_status = ?")
        values.append(status)
    if "translation_error" in columns:
        updates.append("translation_error = ?")
        values.append(error)
    if "translation_request_id" in columns:
        updates.append("translation_request_id = ?")
        values.append(request_id)
    if "translation_updated_at" in columns:
        updates.append("translation_updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if not updates:
        return
    try:
        db.execute(
            f"UPDATE blog_posts SET {', '.join(updates)} WHERE id = ?",  # nosec B608
            (*values, blog_id),
        )
        db.commit()
    except (sqlite3.Error, OSError):
        # 状态是辅助诊断信息，不能让翻译/删除结果变成失败。
        return


def _blog_calendar_days(db: sqlite3.Connection, group: str, author: str = "") -> dict[str, int]:
    """按博客分组/作者统计有效日期的文章数。"""
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


def _blog_list_excerpt(body_text: str, translation: str, query: str, limit: int = 260) -> str:
    """生成博客列表摘要，不把正文/译文全文下发到列表接口。"""
    text = " ".join(" ".join(re.sub(r"<[^>]+>", " ", str(value or "")).replace("\n", " ").split())
                    for value in (body_text, translation)).strip()
    if not text:
        return ""
    if query:
        lower_text = text.lower()
        idx = lower_text.find(str(query).lower())
        if idx >= 0:
            start = max(0, idx - limit // 3)
            end = min(len(text), start + limit)
            prefix = "…" if start else ""
            suffix = "…" if end < len(text) else ""
            return prefix + text[start:end].strip() + suffix
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def handle_blogs(handler, sub: str, guard_fn, read_body_json_fn) -> bool:
    """处理官方博客子路由，处理则返回 True，未命中返回 False。"""
    qs = parse_qs(handler.path.partition("?")[2])

    def qp(key: str, default: str = "") -> str:
        return (qs.get(key) or [default])[0]

    # 1. 博客分组统计
    if sub == "blog_groups":
        try:
            rows = _get_db().execute("""
                SELECT group_key, COUNT(*), MIN(date), MAX(date)
                FROM blog_posts
                GROUP BY group_key
                ORDER BY group_key
            """).fetchall()
        except (sqlite3.Error, OSError):
            _send_json_resp(handler, {"ok": False, "errors": ["博客分组暂时不可用"]}, 500)
            return True
        groups = [{
            "key": row[0],
            "total": int(row[1] or 0),
            "first_date": row[2] or "",
            "last_date": row[3] or "",
        } for row in rows]
        _send_json_resp(handler, {"ok": True, "groups": groups})
        return True

    # 2. 博客日历热力图
    if sub == "blog_calendar":
        group = qp("group", "hinatazaka")
        author = qp("author", "").strip()
        try:
            days = _cal_days(_get_db(), group, author)
        except (sqlite3.Error, OSError):
            _send_json_resp(handler, {"ok": False, "errors": ["博客日历暂时不可用"]}, 500)
            return True
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
        return True

    # 3. 博客作者列表
    if sub == "blog_authors":
        group = qp("group", "hinatazaka")
        authors = []
        try:
            db = _get_db()
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
        except (sqlite3.Error, OSError, ImportError, ValueError) as exc:
            _send_json_resp(handler, {"ok": False, "errors": [f"博客作者暂时不可用: {exc}"]}, 500)
            return True
        _send_json_resp(handler, {"ok": True, "group": group, "authors": authors})
        return True

    # 4. 博客列表与详情
    if sub == "blogs":
        blog_id = qp("id")
        if blog_id:
            try:
                db = _get_db()
                r = db.execute("SELECT * FROM blog_posts WHERE id=?", (blog_id,)).fetchone()
                if r:
                    d = dict(r)
                    d["images_json"] = d.get("images_json") or "[]"
                    d["image_paths_json"] = d.get("image_paths_json") or "[]"
                    _send_json_resp(handler, {"ok": True, "post": d})
                    return True
                _send_json_resp(handler, {"ok": False, "errors": ["博客不存在"]}, 404)
                return True
            except Exception as e:
                _send_json_resp(handler, {"ok": False, "errors": [str(e)]}, 500)
                return True

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
            db = _get_db()
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

            sql = f"""
                SELECT id, group_key, author, title, url, date,
                       body_text, translation, content_json,
                       images_json, image_paths_json
                FROM blog_posts {where}
                ORDER BY date DESC LIMIT ? OFFSET ?
            """
            rows = db.execute(sql, params + [limit, offset]).fetchall()
            for r in rows:
                d = dict(r)
                images_raw = d.pop("images_json", "") or "[]"
                paths_raw = d.pop("image_paths_json", "") or "[]"
                content_raw = d.pop("content_json", "") or "[]"
                try:
                    images = json.loads(images_raw) if isinstance(images_raw, str) else images_raw
                except (TypeError, ValueError):
                    images = []
                try:
                    paths = json.loads(paths_raw) if isinstance(paths_raw, str) else paths_raw
                except (TypeError, ValueError):
                    paths = []
                images = images if isinstance(images, list) else []
                paths = paths if isinstance(paths, list) else []
                first_path = next((str(item) for item in paths if item), "")
                first_image = next((str(item) for item in images if item), "")
                d["cover"] = _blog_media_url(first_path) if first_path else first_image
                d["cover_original"] = first_image if first_path and first_image else ""
                d["image_count"] = max(len(images), len(paths))
                translation = d.pop("translation", "") or ""
                d["has_translation"] = bool(
                    str(translation).strip() or
                    (content_raw not in ("", "[]", "{}"))
                )
                body_text = d.pop("body_text", "") or ""
                d["excerpt"] = _blog_list_excerpt(body_text, translation, q)
                posts.append(d)
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            _send_json_resp(handler, {"ok": False, "errors": [f"博客列表暂时不可用: {exc}"]}, 500)
            return True

        _send_json_resp(handler, {
            "ok": True,
            "group": group,
            "posts": posts,
            "total": total,
            "page": page,
            "total_pages": total_pages,
        })
        return True

    # 5. 手动翻译博客
    if sub == "blogs/translate":
        if not guard_fn(need_admin=True):
            return True
        if handler.command != "POST":
            _send_json_resp(handler, {"ok": False, "msg": "Method not allowed"}, 405)
            return True

        body = read_body_json_fn()
        if body is None:
            return True
        if not isinstance(body, dict):
            _send_json_resp(handler, {"ok": False, "msg": "请求体必须是 JSON 对象"}, 400)
            return True

        raw_id = body.get("id")
        try:
            if isinstance(raw_id, bool):
                raise ValueError
            blog_id = int(raw_id)
        except (TypeError, ValueError):
            _send_json_resp(handler, {"ok": False, "msg": "无效参数：id 必须是正整数"}, 400)
            return True
        if blog_id <= 0:
            _send_json_resp(handler, {"ok": False, "msg": "无效参数：id 必须是正整数"}, 400)
            return True

        request_id = f"manual-{uuid.uuid4().hex[:12]}"
        translation_lock = _get_translation_lock(blog_id)
        if not translation_lock.acquire(blocking=False):
            _send_json_resp(handler, {
                "ok": False,
                "id": blog_id,
                "status": "running",
                "request_id": request_id,
                "msg": "该博客正在翻译，请稍后重试",
            }, 409)
            return True

        from src import translator
        from src.logger import log_all

        db = None
        try:
            db = _get_db()
            row = db.execute("SELECT * FROM blog_posts WHERE id = ?", (blog_id,)).fetchone()
            if row is None:
                _send_json_resp(handler, {"ok": False, "msg": "未找到该博客"}, 404)
                return True
            row = dict(row)

            # 结构化译文已存在时直接返回，避免重复调用模型。
            cached_translation = str(row.get("translation") or "")
            cached_blocks = str(row.get("content_json") or "")
            cached_status = str(row.get("translation_status") or "")
            if (
                cached_translation.strip()
                and cached_blocks not in ("", "[]", "{}")
                and cached_status not in {"partial", "failed", "running"}
            ):
                _send_json_resp(handler, {
                    "ok": True,
                    "id": blog_id,
                    "html": cached_translation,
                    "content_json": cached_blocks,
                    "translation_model": row.get("translation_model") or "",
                    "translation_status": row.get("translation_status") or "succeeded",
                    "request_id": request_id,
                })
                return True

            body_html = str(row.get("body_html") or "")
            if not body_html.strip():
                _set_state(
                    db, blog_id, status="skipped", error="empty_body", request_id=request_id,
                )
                _send_json_resp(handler, {"ok": False, "msg": "该博客没有可翻译的正文", "request_id": request_id}, 422)
                return True

            _set_state(db, blog_id, status="running", request_id=request_id)
            log_all(
                f"🔄 网页端博客翻译开始 | trace={request_id} | id={blog_id} | "
                f"author={row.get('author', '')} | chars={len(body_html)}",
                is_debug=True,
            )

            async def _do_translate():
                async with translator.create_client() as temp_client:
                    return await translator.translate_blog_structured(
                        body_html,
                        row.get("author", ""),
                        row.get("group_key", ""),
                        custom_client=temp_client,
                        request_id=request_id,
                        source="archive_manual",
                    )

            structured, model_name = asyncio.run(_do_translate())
            if not structured:
                error_code = "no_model_result"
                _set_state(
                    db, blog_id, status="failed", error=error_code, request_id=request_id,
                )
                log_all(
                    f"⚠️ 网页端博客翻译无结果 | trace={request_id} | id={blog_id} | "
                    f"error={error_code}",
                    is_error=True,
                )
                _send_json_resp(handler, {
                    "ok": False,
                    "id": blog_id,
                    "status": "failed",
                    "request_id": request_id,
                    "msg": "翻译失败，请检查 API Key、代理与网络连接",
                }, 502)
                return True

            translated = translator.blocks_to_html(structured)
            content_json = json.dumps(structured, ensure_ascii=False)
            translation_model = model_name or ""
            translated_count, total_count, complete = translator.blog_translation_stats(structured)
            translation_status = "succeeded" if complete else "partial"
            db.execute(
                "UPDATE blog_posts SET translation = ?, content_json = ?, translation_model = ? WHERE id = ?",
                (translated, content_json, translation_model, blog_id),
            )
            db.commit()
            _set_state(
                db, blog_id, status=translation_status, request_id=request_id,
            )
            log_all(
                f"✅ 网页端博客翻译完成 | trace={request_id} | id={blog_id} | "
                f"model={translation_model or 'unknown'} | translated={translated_count}/{total_count} | "
                f"complete={'yes' if complete else 'no'}",
                is_debug=True,
            )
            _send_json_resp(handler, {
                "ok": True,
                "id": blog_id,
                "html": translated,
                "content_json": content_json,
                "translation_model": translation_model,
                "translation_status": translation_status,
                "translation_complete": complete,
                "request_id": request_id,
            })
        except asyncio.TimeoutError:
            error_code = "timeout"
            if db is not None:
                _set_state(db, blog_id, status="failed", error=error_code, request_id=request_id)
            log_all(
                f"⏱️ 网页端博客翻译超时 | trace={request_id} | id={blog_id} | error={error_code}",
                is_error=True,
            )
            _send_json_resp(handler, {
                "ok": False, "id": blog_id, "status": "failed", "request_id": request_id,
                "msg": "翻译超时，请稍后重试（可在日志中按请求 ID 定位）",
            }, 504)
        except httpx.TimeoutException:
            error_code = "timeout"
            if db is not None:
                _set_state(db, blog_id, status="failed", error=error_code, request_id=request_id)
            log_all(
                f"⏱️ 网页端博客翻译网络超时 | trace={request_id} | id={blog_id} | error={error_code}",
                is_error=True,
            )
            _send_json_resp(handler, {
                "ok": False, "id": blog_id, "status": "failed", "request_id": request_id,
                "msg": "翻译网络超时，请检查代理后重试",
            }, 504)
        except httpx.HTTPError as exc:
            from src import translator
            error_code = translator.describe_exception(exc)
            if db is not None:
                _set_state(db, blog_id, status="failed", error=error_code, request_id=request_id)
            log_all(
                f"⚠️ 网页端博客翻译网络失败 | trace={request_id} | id={blog_id} | error={error_code}",
                is_error=True,
            )
            _send_json_resp(handler, {
                "ok": False, "id": blog_id, "status": "failed", "request_id": request_id,
                "msg": "翻译网络请求失败，请检查代理与 API 配置",
            }, 502)
        except (sqlite3.Error, OSError, ValueError, TypeError, RuntimeError) as exc:
            from src import translator
            error_code = translator.describe_exception(exc)
            if db is not None:
                _set_state(db, blog_id, status="failed", error=error_code, request_id=request_id)
            log_all(
                f"⚠️ 网页端博客翻译失败 | trace={request_id} | id={blog_id} | error={error_code}",
                is_error=True,
            )
            _send_json_resp(handler, {
                "ok": False, "id": blog_id, "status": "failed", "request_id": request_id,
                "msg": "翻译失败，请稍后重试",
            }, 500)
        except Exception as exc:
            from src import translator
            error_code = translator.describe_exception(exc)
            if db is not None:
                _set_state(db, blog_id, status="failed", error=error_code, request_id=request_id)
            log_all(
                f"⚠️ 网页端博客翻译未预期异常 | trace={request_id} | id={blog_id} | error={error_code}",
                is_error=True,
            )
            _send_json_resp(handler, {
                "ok": False, "id": blog_id, "status": "failed", "request_id": request_id,
                "msg": "翻译失败，请稍后重试",
            }, 500)
        finally:
            translation_lock.release()
        return True

    # 6. 删除博客译文
    if sub == "blogs/delete_translation":
        if not guard_fn(need_admin=True):
            return True
        if handler.command != "POST":
            _send_json_resp(handler, {"ok": False, "msg": "Method not allowed"}, 405)
            return True

        body = read_body_json_fn()
        if body is None:
            return True
        if not isinstance(body, dict):
            _send_json_resp(handler, {"ok": False, "msg": "请求体必须是 JSON 对象"}, 400)
            return True

        raw_id = body.get("id")
        try:
            if isinstance(raw_id, bool):
                raise ValueError
            blog_id = int(raw_id)
        except (TypeError, ValueError):
            _send_json_resp(handler, {"ok": False, "msg": "无效参数：id 必须是正整数"}, 400)
            return True
        if blog_id <= 0:
            _send_json_resp(handler, {"ok": False, "msg": "无效参数：id 必须是正整数"}, 400)
            return True

        translation_lock = _get_translation_lock(blog_id)
        if not translation_lock.acquire(blocking=False):
            _send_json_resp(handler, {
                "ok": False,
                "id": blog_id,
                "status": "running",
                "msg": "该博客正在翻译，暂时不能删除译文",
            }, 409)
            return True

        try:
            db = _get_db()
            row = db.execute(
                "SELECT author, title FROM blog_posts WHERE id = ?",
                (blog_id,),
            ).fetchone()
            if row is None:
                _send_json_resp(handler, {"ok": False, "msg": "未找到该博客"}, 404)
                return True

            body_html = ""
            try:
                body_row = db.execute(
                    "SELECT body_html FROM blog_posts WHERE id = ?", (blog_id,)
                ).fetchone()
                body_html = str(body_row[0] or "") if body_row else ""
            except (sqlite3.Error, OSError):
                body_html = ""

            db.execute(
                "UPDATE blog_posts "
                "SET translation = NULL, content_json = NULL, translation_model = NULL "
                "WHERE id = ?",
                (blog_id,),
            )
            db.commit()
            _set_state(db, blog_id, status="pending", request_id="")

            from src import translator
            translator.invalidate_blog_cache(row[0] or "", body_html)
        except (sqlite3.Error, OSError) as exc:
            _send_json_resp(handler, {"ok": False, "msg": f"删除翻译失败：{exc}"}, 500)
            return True
        finally:
            translation_lock.release()

        from src.logger import log_all
        log_all(f"🗑️ 管理员删除博客翻译缓存 | id={blog_id}")
        try:
            current_user_fn = getattr(handler, "_current_user", None)
            user = current_user_fn() if callable(current_user_fn) else {}
            from src.webui_modules.auth_handlers import get_client_ip
            source_ip = get_client_ip(handler)
            record_event(
                "archive.blog_translation.delete",
                outcome="success",
                actor=user.get("username"),
                source_ip=source_ip,
                target=str(blog_id),
                details={"author": row[0] or "", "title": row[1] or ""},
            )
        except Exception:
            pass
        _send_json_resp(handler, {"ok": True, "id": blog_id, "msg": "已清除该博客的翻译"})
        return True

    # 7. 博客振假名
    if sub == "blogs/furigana":
        if handler.command != "POST":
            _send_json_resp(handler, {"ok": False, "msg": "Method not allowed"}, 405)
            return True
        body_data = read_body_json_fn() or {}
        blog_id = body_data.get("id")
        raw_html = body_data.get("html")
        raw_title = body_data.get("title")

        try:
            from src import furigana
            if blog_id:
                db = _get_db()
                row = db.execute("SELECT * FROM blog_posts WHERE id = ?", (int(blog_id),)).fetchone()
                if not row:
                    _send_json_resp(handler, {"ok": False, "msg": "未找到该博客"}, 404)
                    return True
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
                return True

            if raw_html:
                f_html = furigana.add_furigana_to_html(str(raw_html))
                f_title = furigana.add_furigana_to_text(str(raw_title)) if raw_title else ""
                _send_json_resp(handler, {"ok": True, "title": f_title, "furigana_html": f_html})
                return True

            _send_json_resp(handler, {"ok": False, "msg": "缺少 id 或 html 参数"}, 400)
        except Exception as e:
            _send_json_resp(handler, {"ok": False, "msg": f"生成振假名异常: {e}"}, 500)
        return True

    # 8. 博客静态媒体流服务
    if sub.startswith("blog_media/"):
        rel_str = unquote(sub[len("blog_media/"):].replace("\\", "/"))
        rel = Path(rel_str)
        full = (BLOG_IMAGE_DIR / rel).resolve()
        if BLOG_IMAGE_DIR.resolve() not in full.parents and full != BLOG_IMAGE_DIR.resolve():
            _send_json_resp(handler, {"ok": False, "errors": ["非法路径"]}, 403)
            return True
        if not full.is_file():
            _send_json_resp(handler, {"ok": False, "errors": ["媒体不存在"]}, 404)
            return True
        serve_file_range(handler, full)
        return True

    return False
