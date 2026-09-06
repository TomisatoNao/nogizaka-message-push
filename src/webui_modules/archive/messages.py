"""
src/webui_modules/archive/messages.py — 成员消息归档、检索、标签与头像服务
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote

import config.config as cfg
from src import archive as _archive
from src.audit import record_event
from src.webui_modules.archive.common import (
    ARCHIVE_TYPES,
    _archive_write_lock,
    _send_json_resp,
)
from src.webui_modules.media_service import serve_file_range


def handle_messages(handler, sub: str, guard_fn, read_body_json_fn) -> bool:
    """处理消息归档相关子路由，处理则返回 True，未命中返回 False。"""
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
                    return True
                except Exception:
                    pass
        _send_json_resp(handler, {"ok": False, "errors": ["Avatar not found"]}, 404)
        return True

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
        return True

    # 3. 月份列表
    if sub == "months":
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return True
        _send_json_resp(handler, {"ok": True, "member": member, "months": _archive.list_months(member)})
        return True

    # 4. 消息分页
    if sub == "messages":
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return True
        try:
            year, month = int(qp("year")), int(qp("month"))
            page = max(1, int(qp("page", "1")))
            per_page = min(200, max(1, int(qp("per_page", "50"))))
        except ValueError:
            _send_json_resp(handler, {"ok": False, "errors": ["year/month/page 必须是数字"]}, 400)
            return True

        # 保持未带参数的旧 API 为升序；网页端显式请求 desc 以首屏展示最新消息。
        order = qp("order", "asc").strip().lower()
        if order not in {"asc", "desc"}:
            _send_json_resp(handler, {"ok": False, "errors": ["order 必须是 asc 或 desc"]}, 400)
            return True

        type_filter = qp("type")
        msgs = _archive.load_month(member, year, month)
        if type_filter:
            if type_filter not in ARCHIVE_TYPES:
                _send_json_resp(handler, {"ok": False, "errors": [f"未知类型: {type_filter!r}"]}, 400)
                return True
            wanted = {"picture", "image"} if type_filter in ("picture", "image") else {type_filter}
            msgs = [m for m in msgs if m.get("type") in wanted]

        if order == "desc":
            msgs.reverse()

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
            "total": total, "page": page, "order": order,
            "total_pages": max(1, -(-total // per_page)), "messages": slim,
        })
        return True

    # 5. 日历热力图
    if sub == "calendar":
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return True
        type_filter = qp("type")
        wanted = None
        if type_filter:
            if type_filter not in ARCHIVE_TYPES:
                _send_json_resp(handler, {"ok": False, "errors": [f"未知类型: {type_filter!r}"]}, 400)
                return True
            wanted = {"picture", "image"} if type_filter in ("picture", "image") else {type_filter}
        _send_json_resp(handler, {"ok": True, "member": member, "days": _archive.day_counts(member, type_filter=wanted)})
        return True

    # 6. FTS5 全文搜索
    if sub == "search":
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return True
        query = qp("q").strip()
        if not query:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少搜索关键词 q"]}, 400)
            return True
        if len(query) > 100:
            _send_json_resp(handler, {"ok": False, "errors": ["搜索关键词不能超过 100 个字符"]}, 400)
            return True
        try:
            page = max(1, int(qp("page", "1")))
            per_page = min(200, max(1, int(qp("per_page", "50"))))
        except ValueError:
            _send_json_resp(handler, {"ok": False, "errors": ["page 必须是数字"]}, 400)
            return True

        order = qp("order", "desc").strip().lower()
        if order not in {"asc", "desc"}:
            _send_json_resp(handler, {"ok": False, "errors": ["order 必须是 asc 或 desc"]}, 400)
            return True

        type_filter = qp("type")
        wanted = None
        if type_filter:
            if type_filter not in ARCHIVE_TYPES:
                _send_json_resp(handler, {"ok": False, "errors": [f"未知类型: {type_filter!r}"]}, 400)
                return True
            wanted = {"picture", "image"} if type_filter in ("picture", "image") else {type_filter}

        hits = _archive.search(member, query, type_filter=wanted, order=order)
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
            "page": page, "order": order, "total_pages": max(1, -(-total // per_page)),
            "capped": total >= 500, "messages": slim,
        })
        return True

    # 7. 自定义标签
    if sub == "tags":
        if not guard_fn(need_admin=True):
            return True
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return True
        body = read_body_json_fn()
        if body is None:
            return True
        if not isinstance(body, dict):
            _send_json_resp(handler, {"ok": False, "errors": ["请求体必须是 JSON 对象"]}, 400)
            return True
        msg_id = str(body.get("id") or "")
        raw_tags = body.get("custom_tags", "")
        if not isinstance(raw_tags, str):
            _send_json_resp(handler, {"ok": False, "errors": ["custom_tags 必须是字符串"]}, 400)
            return True
        tags = raw_tags.strip()
        if len(tags) > 500:
            _send_json_resp(handler, {"ok": False, "errors": ["custom_tags 不能超过 500 个字符"]}, 400)
            return True
        try:
            year = int(body.get("year"))
            month = int(body.get("month"))
        except (TypeError, ValueError):
            _send_json_resp(handler, {"ok": False, "errors": ["year/month 必须是数字"]}, 400)
            return True
        if not msg_id or not 1 <= month <= 12 or not 1 <= year <= 9999:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少 id/year/month"]}, 400)
            return True
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
                return True
            json_path = (_archive.archive_root() / member / f"{year:04d}" / f"{month:02d}" / "messages.json")
            if not json_path.is_file():
                _send_json_resp(handler, {"ok": False, "errors": ["归档文件不存在"]}, 404)
                return True
            tmp = json_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(msgs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, json_path)
        try:
            from src.webui_modules.auth_handlers import current_user, get_client_ip
            user = current_user(handler) or {}
            source_ip = get_client_ip(handler)
            record_event("archive.custom_tags", outcome="success", actor=user.get("username"),
                         source_ip=source_ip, target=f"{member}/{year:04d}-{month:02d}/{msg_id}",
                         details={"tag_count": len([tag for tag in tags.split(",") if tag.strip()])})
        except Exception:
            pass
        _send_json_resp(handler, {"ok": True, "id": msg_id, "custom_tags": tags})
        return True

    # 8. 消息媒体流服务
    if sub.startswith("media/"):
        rest = unquote(sub[len("media/"):])
        raw_member, _, rel = rest.partition("/")
        member = _archive.member_dir_name(raw_member) if raw_member else ""
        if not member or member not in _archive.list_members() or not rel:
            _send_json_resp(handler, {"ok": False, "errors": ["媒体不存在"]}, 404)
            return True
        member_root = (_archive.archive_root() / member).resolve()
        full = (member_root / rel).resolve()
        if member_root not in full.parents:
            _send_json_resp(handler, {"ok": False, "errors": ["非法路径"]}, 403)
            return True
        if not full.is_file():
            _send_json_resp(handler, {"ok": False, "errors": ["媒体不存在"]}, 404)
            return True
        serve_file_range(handler, full)
        return True

    # 9. 后台历史消息全量回填
    if sub == "messages/backfill":
        from src.webui_modules.archive.message_backfill import handle_message_backfill
        handle_message_backfill(handler, guard_fn, read_body_json_fn)
        return True

    # 10. 失败媒体重试下载
    if sub == "retry_download":
        if not guard_fn(need_admin=False):
            return True
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member or member not in _archive.list_members():
            _send_json_resp(handler, {"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
            return True
        body = read_body_json_fn()
        if body is None:
            return True
        if not isinstance(body, dict):
            _send_json_resp(handler, {"ok": False, "errors": ["请求体必须是 JSON 对象"]}, 400)
            return True
        msg_id = str(body.get("id") or "")
        try:
            year = int(body.get("year"))
            month = int(body.get("month"))
        except (TypeError, ValueError):
            _send_json_resp(handler, {"ok": False, "errors": ["year/month 必须是数字"]}, 400)
            return True
        if not msg_id or not 1 <= month <= 12 or not 1 <= year <= 9999:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少 id/year/month"]}, 400)
            return True

        with _archive_write_lock:
            msgs = _archive.load_month(member, year, month)
            target_msg = None
            for m in msgs:
                if str(m.get("id", "")) == msg_id:
                    target_msg = m
                    break
            if not target_msg:
                _send_json_resp(handler, {"ok": False, "errors": [f"消息 {msg_id} 不存在"]}, 404)
                return True

            file_url = target_msg.get("file") or target_msg.get("thumbnail") or ""
            if not file_url:
                _send_json_resp(handler, {"ok": False, "errors": ["该消息无媒体下载链接"]}, 400)
                return True

            import urllib.request
            ts_str = target_msg.get("published_at") or target_msg.get("updated_at", "")
            try:
                dt = _archive.parse_jst_datetime(ts_str)
            except Exception:
                from datetime import datetime
                dt = datetime.now()

            dest_dir = _archive._month_dir(member, dt) / _archive._media_subdir(target_msg.get("type", ""))
            dest_dir.mkdir(parents=True, exist_ok=True)
            ts = dt.strftime("%Y%m%d_%H%M%S")
            tmp_path = dest_dir / f"{ts}_{msg_id}.tmp"

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            candidate_urls = [u for u in [target_msg.get("file"), target_msg.get("thumbnail")] if u]
            ok = False
            used_url = file_url
            for u in candidate_urls:
                if ok:
                    break
                try:
                    req = urllib.request.Request(u, headers=headers)
                    with urllib.request.urlopen(req, timeout=30) as resp, open(tmp_path, "wb") as f:
                        if resp.status == 200:
                            f.write(resp.read())
                            if tmp_path.exists() and tmp_path.stat().st_size > 0:
                                ok = True
                                used_url = u
                                break
                except Exception:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

            if not ok:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                _send_json_resp(handler, {"ok": False, "errors": ["下载失败，该媒体资源链接可能已过期，可使用 backfill_archive.py 工具带最新 Token 回填重试"]}, 400)
                return True

            ext = _archive._guess_extension(used_url, _archive._sniff_content_type(tmp_path))
            final_path = dest_dir / f"{ts}_{msg_id}{ext}"
            os.replace(tmp_path, final_path)
            rel = final_path.relative_to(_archive._member_root(member)).as_posix()

            target_msg["_local_file"] = rel
            target_msg.pop("_download_failed", None)

            json_path = (_archive.archive_root() / member / f"{year:04d}" / f"{month:02d}" / "messages.json")
            tmp = json_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(msgs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, json_path)
            _archive._save_msgs_to_sqlite(member, year, month, [target_msg])

            _send_json_resp(handler, {"ok": True, "id": int(msg_id) if msg_id.isdigit() else msg_id, "local_file": rel, "media_url": f"/api/archive/media/{member}/{rel}"})
            return True

    return False
