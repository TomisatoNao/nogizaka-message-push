"""
src/webui_modules/archive/gallery.py — 纯享美图画廊服务
"""

from __future__ import annotations

import json
import time as _time
from urllib.parse import parse_qs

from src import archive as _archive
from src.webui_modules.archive.common import _blog_media_url, _send_json_resp


def handle_gallery(handler, sub: str, guard_fn, read_body_json_fn) -> bool:
    """处理相册画廊子路由，命中返回 True，未命中返回 False。"""
    if sub not in {"gallery", "gallery_members"}:
        return False

    if not guard_fn(need_admin=False):
        return True

    if sub == "gallery_members":
        data = get_gallery_members()
        _send_json_resp(handler, data)
        return True

    qs = parse_qs(handler.path.partition("?")[2])

    def qp(key: str, default: str = "") -> str:
        return (qs.get(key) or [default])[0]

    raw_m = qp("member")
    member = _archive.member_dir_name(raw_m) if raw_m else ""
    source = qp("source", "all").lower().strip()
    if source not in {"all", "message", "blog"}:
        source = "all"

    try:
        page = max(1, int(qp("page", "1")))
    except (ValueError, TypeError):
        page = 1

    try:
        per_page = max(1, min(100, int(qp("per_page", "40"))))
    except (ValueError, TypeError):
        per_page = 40

    year = None
    if qp("year"):
        try:
            year = int(qp("year"))
        except (ValueError, TypeError):
            pass

    month = None
    if qp("month"):
        try:
            month = int(qp("month"))
        except (ValueError, TypeError):
            pass

    # 如果只查博客图片
    if source == "blog":
        data = _get_blog_gallery(member=member, page=page, per_page=per_page, year=year, month=month)
        _send_json_resp(handler, data)
        return True

    # 查 Message 图片
    res = _archive.get_gallery_photos(
        member_dir=member,
        source=source,
        page=page,
        per_page=per_page,
        year=year,
        month=month,
    )

    # 若为 all 模式，结合博客美图按时间倒序混编呈现
    if source == "all" and res.get("ok"):
        blog_data = _get_blog_gallery(member=member, page=page, per_page=per_page, year=year, month=month)
        blog_total = blog_data.get("total", 0)
        blog_photos = blog_data.get("photos", [])

        if blog_photos:
            combined = sorted(
                res.get("photos", []) + blog_photos,
                key=lambda x: str(x.get("published_at") or ""),
                reverse=True,
            )
            res["photos"] = combined[:per_page]

        res["total"] = res.get("total", 0) + blog_total
        res["total_pages"] = (res["total"] + per_page - 1) // per_page if res["total"] > 0 else 1
        res["has_more"] = page < res["total_pages"]

    _send_json_resp(handler, res)
    return True


_blog_count_cache: dict[tuple, tuple[float, int]] = {}
_BLOG_COUNT_CACHE_TTL = 120.0


def _get_blog_gallery(
    member: str = "",
    page: int = 1,
    per_page: int = 40,
    year: int | None = None,
    month: int | None = None,
) -> dict:
    """从 blog.db 检索博客配图（利用 SQLite json_each 引擎级分页，极速毫秒响应）。"""
    try:
        from src.webui_modules.archive_handlers import get_blog_db
        blog_db = get_blog_db()
        if not blog_db:
            return {"ok": True, "total": 0, "photos": [], "page": page, "per_page": per_page, "has_more": False}

        where = ["p.image_paths_json IS NOT NULL", "p.image_paths_json != '[]'", "p.image_paths_json != ''"]
        params: list[object] = []

        if member:
            norm_m = member.replace(" ", "").replace("　", "").replace("_", "")
            where.append("REPLACE(REPLACE(REPLACE(p.author, ' ', ''), '　', ''), '_', '') = ?")
            params.append(norm_m)
        if year:
            y_str = f"{year:04d}"
            where.append("substr(p.date, 1, 4) = ?")
            params.append(y_str)
        if month and year:
            ym_str = f"{year:04d}-{month:02d}"
            where.append("substr(p.date, 1, 7) = ?")
            params.append(ym_str)

        where_str = " AND ".join(where)

        # 1. 尝试使用 SQLite json_each 引擎级极速分页
        try:
            cache_key = (member, year, month)
            now = _time.monotonic()
            cached = _blog_count_cache.get(cache_key)
            if cached and (now - cached[0]) < _BLOG_COUNT_CACHE_TTL:
                total_photos = cached[1]
            else:
                count_sql = f"""
                    SELECT COUNT(*)
                    FROM blog_posts p, json_each(p.image_paths_json) j
                    WHERE {where_str};
                """
                total_photos = blog_db.execute(count_sql, params).fetchone()[0]
                _blog_count_cache[cache_key] = (now, total_photos)

            offset = (page - 1) * per_page
            photos_sql = f"""
                SELECT p.id, p.group_key, p.author, p.title, p.date, j.key, j.value
                FROM blog_posts p, json_each(p.image_paths_json) j
                WHERE {where_str}
                ORDER BY p.date DESC, p.id DESC, CAST(j.key AS INTEGER) ASC
                LIMIT ? OFFSET ?;
            """
            rows = blog_db.execute(photos_sql, params + [per_page, offset]).fetchall()

            photos = []
            for r in rows:
                post_id = r[0]
                g_key = r[1]
                author = r[2]
                title = r[3]
                dt = r[4] or ""
                idx = r[5]
                img_rel = r[6]
                if not img_rel:
                    continue
                clean_path = str(img_rel).replace("\\", "/")
                url = _blog_media_url(clean_path)
                if not url:
                    continue
                photos.append({
                    "id": f"blog_{post_id}_{idx}",
                    "blog_id": str(post_id),
                    "source": "blog",
                    "group_key": g_key,
                    "member_name": author,
                    "member_dir": _archive.member_dir_name(author),
                    "published_at": dt,
                    "text": title,
                    "url": url,
                    "local_file": clean_path,
                    "year": int(dt[:4]) if len(dt) >= 4 and dt[:4].isdigit() else None,
                    "month": int(dt[5:7]) if len(dt) >= 7 and dt[5:7].isdigit() else None,
                })

            total_pages = (total_photos + per_page - 1) // per_page if total_photos > 0 else 1
            return {
                "ok": True,
                "source": "blog",
                "member": member,
                "page": page,
                "per_page": per_page,
                "total": total_photos,
                "total_pages": total_pages,
                "has_more": page < total_pages,
                "photos": photos,
            }
        except Exception:
            # 降级方案：针对不支持 json_each 的旧 SQLite 环境
            posts_sql = f"""
                SELECT p.id, p.group_key, p.author, p.title, p.date, p.image_paths_json
                FROM blog_posts p
                WHERE {where_str}
                ORDER BY p.date DESC, p.id DESC;
            """
            rows = blog_db.execute(posts_sql, params).fetchall()
            all_photos = []
            for r in rows:
                post_id = r[0]
                g_key = r[1]
                author = r[2]
                title = r[3]
                dt = r[4] or ""
                imgs_raw = r[5]
                imgs = []
                if imgs_raw:
                    try:
                        imgs = json.loads(imgs_raw)
                    except (ValueError, TypeError):
                        imgs = []
                for idx, img_rel in enumerate(imgs):
                    if not img_rel:
                        continue
                    clean_path = img_rel.replace("\\", "/")
                    url = _blog_media_url(clean_path)
                    if not url:
                        continue
                    all_photos.append({
                        "id": f"blog_{post_id}_{idx}",
                        "blog_id": str(post_id),
                        "source": "blog",
                        "group_key": g_key,
                        "member_name": author,
                        "member_dir": _archive.member_dir_name(author),
                        "published_at": dt,
                        "text": title,
                        "url": url,
                        "local_file": clean_path,
                        "year": int(dt[:4]) if len(dt) >= 4 and dt[:4].isdigit() else None,
                        "month": int(dt[5:7]) if len(dt) >= 7 and dt[5:7].isdigit() else None,
                    })

            total_photos = len(all_photos)
            offset = (page - 1) * per_page
            photos = all_photos[offset:offset + per_page]
            total_pages = (total_photos + per_page - 1) // per_page if total_photos > 0 else 1

            return {
                "ok": True,
                "source": "blog",
                "member": member,
                "page": page,
                "per_page": per_page,
                "total": total_photos,
                "total_pages": total_pages,
                "has_more": page < total_pages,
                "photos": photos,
            }
    except Exception as ex:
        return {"ok": False, "errors": [f"博客图片查询异常: {ex}"], "total": 0, "photos": []}



_gallery_members_cache: tuple[float, list[dict]] | None = None
_GALLERY_MEMBERS_CACHE_TTL = 300.0


def get_gallery_members() -> dict:
    """获取所有相册成员（合并含照片的消息成员与含配图的博客作者），返回归一化名册与各源照片数。"""
    global _gallery_members_cache
    now = _time.monotonic()
    if _gallery_members_cache and (now - _gallery_members_cache[0]) < _GALLERY_MEMBERS_CACHE_TTL:
        return {"ok": True, "members": _gallery_members_cache[1]}

    try:
        from src import avatar_manager
        from src.sakamichi_roster import get_member_sort_tuple
        from src.webui_modules.archive_handlers import get_blog_db

        avatar_map = avatar_manager.get_member_avatar_map()
        members_by_norm: dict[str, dict] = {}

        # 1. 查询 messages 归档表中含有本地有效图片的成员与数量
        conn = _archive.init_db()
        if conn:
            try:
                m_rows = conn.execute("""
                    SELECT member_dir, member_name, COUNT(*)
                    FROM messages
                    WHERE type IN ('picture', 'image') AND local_file IS NOT NULL AND local_file != ''
                    GROUP BY member_dir;
                """).fetchall()
                for m_dir, m_name, cnt in m_rows:
                    if not m_dir:
                        continue
                    norm = m_dir.replace(" ", "").replace("　", "").replace("_", "")
                    grp = _archive.infer_member_group(m_dir)
                    display = m_name.replace("_", " ") if m_name else m_dir.replace("_", " ")
                    avatar = avatar_map.get(f"{grp}:{norm}") or avatar_map.get(norm) or ""
                    members_by_norm[norm] = {
                        "name": m_dir,
                        "display": display,
                        "group": grp,
                        "avatar": avatar,
                        "msg_photos": cnt,
                        "blog_photos": 0,
                        "total_photos": cnt,
                    }
            except Exception:
                pass

        # 2. 查询 blogs.db 中含有有效配图的作者与数量（支持即使无 message 归档的成员）
        try:
            blog_db = get_blog_db()
        except Exception:
            blog_db = None

        if blog_db:
            try:
                try:
                    b_rows = blog_db.execute("""
                        SELECT p.group_key, p.author, COUNT(*)
                        FROM blog_posts p, json_each(p.image_paths_json) j
                        WHERE p.image_paths_json IS NOT NULL AND p.image_paths_json != '' AND p.image_paths_json != '[]'
                        GROUP BY p.group_key, p.author;
                    """).fetchall()
                except Exception:
                    b_posts = blog_db.execute("""
                        SELECT group_key, author, image_paths_json
                        FROM blog_posts
                        WHERE image_paths_json IS NOT NULL AND image_paths_json != '' AND image_paths_json != '[]';
                    """).fetchall()
                    counts_dict = {}
                    for g_k, auth, j_raw in b_posts:
                        if not auth:
                            continue
                        try:
                            imgs = json.loads(j_raw)
                            c = len([img for img in imgs if img])
                        except Exception:
                            c = 0
                        counts_dict[(g_k, auth)] = counts_dict.get((g_k, auth), 0) + c
                    b_rows = [(k[0], k[1], v) for k, v in counts_dict.items()]

                for grp, author, cnt in b_rows:
                    if not author or not author.strip() or cnt <= 0:
                        continue
                    a_clean = author.strip()
                    norm = a_clean.replace(" ", "").replace("　", "").replace("_", "")
                    if norm in members_by_norm:
                        members_by_norm[norm]["blog_photos"] += cnt
                        members_by_norm[norm]["total_photos"] += cnt
                        if not members_by_norm[norm]["avatar"]:
                            members_by_norm[norm]["avatar"] = avatar_map.get(f"{grp}:{norm}") or avatar_map.get(norm) or ""
                    else:
                        m_dir = _archive.member_dir_name(a_clean)
                        resolved_grp = grp or _archive.infer_member_group(a_clean)
                        avatar = avatar_map.get(f"{resolved_grp}:{norm}") or avatar_map.get(norm) or ""
                        members_by_norm[norm] = {
                            "name": m_dir,
                            "display": a_clean,
                            "group": resolved_grp,
                            "avatar": avatar,
                            "msg_photos": 0,
                            "blog_photos": cnt,
                            "total_photos": cnt,
                        }
            except Exception:
                pass

        # 3. 按团队与坂道名册自然顺序排序
        res_list = list(members_by_norm.values())
        res_list.sort(key=lambda x: get_member_sort_tuple(x["group"], x["name"]))

        _gallery_members_cache = (now, res_list)
        return {"ok": True, "members": res_list}
    except Exception as ex:
        return {"ok": False, "errors": [f"获取相册成员异常: {ex}"], "members": []}
