"""
src/webui_modules/archive/letters.py — 粉丝信件查询、同步与收藏服务
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from urllib.parse import parse_qs

import config.config as cfg
import httpx
from src import archive as _archive
from src.webui_modules.archive.common import _send_json_resp


def handle_letters(handler, sub: str, guard_fn, read_body_json_fn) -> bool:
    """处理粉丝信件子路由，处理则返回 True，未命中返回 False。"""
    qs = parse_qs(handler.path.partition("?")[2])

    def qp(key: str, default: str = "") -> str:
        return (qs.get(key) or [default])[0]

    # 1. 粉丝信件列表
    if sub == "letters":
        if not guard_fn(need_admin=True):
            return True
        raw_m = qp("member")
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        if not member:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少成员参数 member"]}, 400)
            return True
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
        return True

    # 2. 粉丝信件同步
    if sub == "letters_sync":
        if not guard_fn(need_admin=True):
            return True
        raw_m = qp("member")
        if not raw_m:
            body = read_body_json_fn()
            if body and isinstance(body, dict):
                raw_m = body.get("member")
        if not raw_m:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少成员参数 member"]}, 400)
            return True
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
        return True

    # 3. 粉丝信件收藏切换
    if sub == "letters/favorite":
        if not guard_fn(need_admin=True):
            return True
        body = read_body_json_fn() or {}
        letter_id = body.get("id")
        is_fav = bool(body.get("is_favorite", False))
        if not letter_id:
            _send_json_resp(handler, {"ok": False, "errors": ["缺少 letter id"]}, 400)
            return True
        _archive.set_letter_favorite(int(letter_id), is_fav)
        _send_json_resp(handler, {"ok": True, "id": int(letter_id), "is_favorite": is_fav})
        return True

    return False
