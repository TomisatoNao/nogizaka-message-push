"""
src/webui_modules/archive/home.py — 首页聚合、统计指标与缓存服务
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import random
import sqlite3
import sys
import threading
from urllib.parse import quote

import config.config as cfg
from src import archive as _archive
from src.webui_modules.archive.common import (
    _send_json_resp,
    get_blog_db,
)

_home_cache: dict | None = None
_home_cache_key: tuple[float, float, str] | None = None
_home_cache_condition = threading.Condition()
_home_cache_building = False


def _get_facade():
    return sys.modules.get("src.webui_modules.archive_handlers")


def _get_db() -> sqlite3.Connection:
    facade = _get_facade()
    if facade and hasattr(facade, "get_blog_db"):
        return facade.get_blog_db()
    return get_blog_db()


def _message_media_totals(members: list[dict]) -> dict[str, int]:
    """汇总消息媒体数量；博客数量不应混入媒体指标。"""
    pictures = sum(int((member.get("stats") or {}).get("pictures", 0) or 0) for member in members)
    videos = sum(int((member.get("stats") or {}).get("videos", 0) or 0) for member in members)
    voices = sum(int((member.get("stats") or {}).get("voices", 0) or 0) for member in members)
    return {
        "pictures": pictures,
        "videos": videos,
        "voices": voices,
        "total": pictures + videos + voices,
    }


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


def handle_home(handler, sub: str, guard_fn, read_body_json_fn) -> None:
    """处理首页聚合请求。"""
    facade = _get_facade()
    cache_key_fn = getattr(facade, "_home_cache_key_for_request", _home_cache_key_for_request)
    cache_key = cache_key_fn()
    today_str = cache_key[2]

    # 检查门面上的 _home_cache
    facade_cache = getattr(facade, "_home_cache", None)
    facade_cache_key = getattr(facade, "_home_cache_key", None)
    if facade_cache is not None and facade_cache_key == cache_key:
        _send_json_resp(handler, facade_cache)
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

    load_text_fn = getattr(facade, "_load_latest_text_by_member", _load_latest_text_by_member)

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

            latest_msgs_by_member = load_text_fn(db, archive_members, limit=4)
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

    blog_db = _get_db()
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
            "year": int(b["date"][:4]) if len(b["date"]) >= 4 and bp["date"][:4].isdigit() else 2026,
            "month": int(b["date"][5:7]) if len(b["date"]) >= 7 and bp["date"][5:7].isdigit() else 8,
        })

    recent_feed = sorted(agg_msgs, key=lambda x: x.get("published_at", ""), reverse=True)[:8]
    total_messages = sum(m["stats"]["total"] for m in members)
    totals_fn = getattr(facade, "_message_media_totals", _message_media_totals)
    media_totals = totals_fn(members)
    first_dates = [m["stats"]["first_date"] for m in members if m["stats"]["first_date"]] + [g["first_date"] for g in blog_groups if g["first_date"]]
    last_dates = [m["stats"]["last_date"] for m in members if m["stats"]["last_date"]] + [g["last_date"] for g in blog_groups if g["last_date"]]

    result = {
        "ok": True,
        "summary": {
            "total_messages": total_messages,
            "total_blogs": total_blogs,
            "total_all": total_messages + total_blogs,
            # 明确区分消息媒体与消息/博客总量，避免首页把 total_all 当作媒体数。
            "total_pictures": media_totals["pictures"],
            "total_videos": media_totals["videos"],
            "total_voices": media_totals["voices"],
            "message_media_total": media_totals["total"],
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
    }
    if facade is not None:
        facade._home_cache = result
        facade._home_cache_key = cache_key
    _send_json_resp(handler, result)


def warm_home_cache(handle_archive_fn=None) -> bool:
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
        if handle_archive_fn is not None:
            handle_archive_fn(handler, "home", lambda **_: True, lambda: {})
        else:
            handle_home(handler, "home", lambda **_: True, lambda: {})
        return bool(getattr(handler, "payload", None))
    except Exception as exc:
        from src.logger import log_all
        log_all(f"⚠️ 首页缓存预热跳过: {type(exc).__name__}: {exc}", is_debug=True)
        return False
