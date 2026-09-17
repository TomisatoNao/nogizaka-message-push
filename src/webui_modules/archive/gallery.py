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
    if sub not in {"gallery", "gallery_members", "gallery_years"}:
        return False

    if not guard_fn(need_admin=False):
        return True

    if sub == "gallery_members":
        data = get_gallery_members()
        _send_json_resp(handler, data)
        return True

    if sub == "gallery_years":
        qs = parse_qs(handler.path.partition("?")[2])
        raw_m = (qs.get("member") or [""])[0]
        member = _archive.member_dir_name(raw_m) if raw_m else ""
        source = (qs.get("source") or ["all"])[0].lower().strip()
        if source not in {"all", "message", "blog"}:
            source = "all"
        data = get_gallery_years(member=member, source=source)
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

    order = qp("order", "desc").lower().strip()
    if order not in {"asc", "desc"}:
        order = "desc"

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
        data = _get_blog_gallery(member=member, page=page, per_page=per_page, year=year, month=month, order=order)
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
        order=order,
    )

    # 若为 all 模式，结合博客美图按时间排序混编呈现
    if source == "all" and res.get("ok"):
        blog_data = _get_blog_gallery(member=member, page=page, per_page=per_page, year=year, month=month, order=order)
        blog_total = blog_data.get("total", 0)
        blog_photos = blog_data.get("photos", [])

        if blog_photos:
            combined = sorted(
                res.get("photos", []) + blog_photos,
                key=lambda x: str(x.get("published_at") or ""),
                reverse=(order == "desc"),
            )
            res["photos"] = combined[:per_page]

        res["total"] = res.get("total", 0) + blog_total
        res["total_pages"] = (res["total"] + per_page - 1) // per_page if res["total"] > 0 else 1
        res["has_more"] = page < res["total_pages"]

    _send_json_resp(handler, res)
    return True


_blog_count_cache: dict[tuple, tuple[float, int]] = {}
_BLOG_COUNT_CACHE_TTL = 120.0


def _get_blog_image_expr(cols: set[str]) -> tuple[str, str]:
    """返回用于匹配包含图片的 WHERE 子句与 json_each 解构表达式。"""
    has_images = "images_json" in cols
    has_paths = "image_paths_json" in cols

    if has_images and has_paths:
        where_expr = (
            "((p.images_json IS NOT NULL AND p.images_json != '[]' AND p.images_json != '') OR "
            "(p.image_paths_json IS NOT NULL AND p.image_paths_json != '[]' AND p.image_paths_json != ''))"
        )
        json_target = (
            "CASE WHEN (p.images_json IS NOT NULL AND p.images_json != '[]' AND p.images_json != '') "
            "THEN p.images_json ELSE p.image_paths_json END"
        )
    elif has_images:
        where_expr = "p.images_json IS NOT NULL AND p.images_json != '[]' AND p.images_json != ''"
        json_target = "p.images_json"
    else:
        where_expr = "p.image_paths_json IS NOT NULL AND p.image_paths_json != '[]' AND p.image_paths_json != ''"
        json_target = "p.image_paths_json"

    return where_expr, json_target


def _get_blog_gallery(
    member: str = "",
    page: int = 1,
    per_page: int = 40,
    year: int | None = None,
    month: int | None = None,
    order: str = "desc",
) -> dict:
    """从 blog.db 检索博客配图（利用 SQLite json_each 引擎级分页，极速毫秒响应）。"""
    try:
        from src.webui_modules.archive.common import _blog_table_columns
        from src.webui_modules.archive_handlers import get_blog_db
        blog_db = get_blog_db()
        if not blog_db:
            return {"ok": True, "total": 0, "photos": [], "page": page, "per_page": per_page, "has_more": False}

        cols = _blog_table_columns(blog_db)
        img_where, json_target = _get_blog_image_expr(cols)
        where = [img_where]
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
        order_dir = "ASC" if str(order).lower() == "asc" else "DESC"

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
                    FROM blog_posts p, json_each({json_target}) j
                    WHERE {where_str} AND j.value IS NOT NULL AND j.value != '';
                """
                total_photos = blog_db.execute(count_sql, params).fetchone()[0]
                _blog_count_cache[cache_key] = (now, total_photos)

            offset = (page - 1) * per_page
            paths_col = "p.image_paths_json" if "image_paths_json" in cols else "NULL"
            photos_sql = f"""
                SELECT p.id, p.group_key, p.author, p.title, p.date, j.key, j.value, {paths_col}
                FROM blog_posts p, json_each({json_target}) j
                WHERE {where_str} AND j.value IS NOT NULL AND j.value != ''
                ORDER BY p.date {order_dir}, p.id {order_dir}, CAST(j.key AS INTEGER) ASC
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
                img_val = r[6]
                paths_raw = r[7]
                if not img_val:
                    continue

                idx_int = int(idx) if str(idx).isdigit() else 0
                clean_local = ""
                if paths_raw:
                    try:
                        paths = json.loads(paths_raw)
                        if isinstance(paths, list) and 0 <= idx_int < len(paths) and paths[idx_int]:
                            clean_local = str(paths[idx_int]).replace("\\", "/")
                    except Exception:
                        pass

                url = ""
                if clean_local:
                    url = _blog_media_url(clean_local)

                if not url:
                    val_str = str(img_val).strip()
                    if val_str.startswith("http://") or val_str.startswith("https://"):
                        url = val_str
                    elif val_str.startswith("/"):
                        base = (
                            "https://www.nogizaka46.com"
                            if g_key == "nogizaka"
                            else "https://sakurazaka46.com"
                            if g_key == "sakurazaka"
                            else "https://www.hinatazaka46.com"
                        )
                        url = base + val_str
                    elif val_str:
                        url = _blog_media_url(val_str.replace("\\", "/"))

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
                    "local_file": clean_local or (str(img_val) if not str(img_val).startswith("http") else ""),
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
            select_cols = ["p.id", "p.group_key", "p.author", "p.title", "p.date"]
            if "images_json" in cols:
                select_cols.append("p.images_json")
            if "image_paths_json" in cols:
                select_cols.append("p.image_paths_json")
            posts_sql = f"""
                SELECT {', '.join(select_cols)}
                FROM blog_posts p
                WHERE {where_str}
                ORDER BY p.date {order_dir}, p.id {order_dir};
            """
            rows = blog_db.execute(posts_sql, params).fetchall()
            all_photos = []
            for r in rows:
                post_id = r[0]
                g_key = r[1]
                author = r[2]
                title = r[3]
                dt = r[4] or ""
                extra = r[5:]
                imgs = []
                paths = []
                if "images_json" in cols and extra:
                    try:
                        imgs = json.loads(extra[0]) if extra[0] else []
                    except Exception:
                        pass
                if "image_paths_json" in cols and extra:
                    p_idx = 1 if "images_json" in cols else 0
                    if p_idx < len(extra):
                        try:
                            paths = json.loads(extra[p_idx]) if extra[p_idx] else []
                        except Exception:
                            pass
                source_list = imgs if (isinstance(imgs, list) and imgs) else paths if isinstance(paths, list) else []
                for idx, img_val in enumerate(source_list):
                    if not img_val:
                        continue
                    clean_local = ""
                    if isinstance(paths, list) and idx < len(paths) and paths[idx]:
                        clean_local = str(paths[idx]).replace("\\", "/")
                    url = ""
                    if clean_local:
                        url = _blog_media_url(clean_local)
                    if not url:
                        val_str = str(img_val).strip()
                        if val_str.startswith("http://") or val_str.startswith("https://"):
                            url = val_str
                        elif val_str.startswith("/"):
                            base = (
                                "https://www.nogizaka46.com"
                                if g_key == "nogizaka"
                                else "https://sakurazaka46.com"
                                if g_key == "sakurazaka"
                                else "https://www.hinatazaka46.com"
                            )
                            url = base + val_str
                        elif val_str:
                            url = _blog_media_url(val_str.replace("\\", "/"))
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
                        "local_file": clean_local or (str(img_val) if not str(img_val).startswith("http") else ""),
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


_gallery_years_cache: dict[tuple, tuple[float, dict]] = {}
_GALLERY_YEARS_CACHE_TTL = 120.0


def get_gallery_years(member: str = "", source: str = "all") -> dict:
    """获取指定成员及来源下的年份分布列表及照片统计。"""
    cache_key = (member, source)
    now = _time.monotonic()
    cached = _gallery_years_cache.get(cache_key)
    if cached and (now - cached[0]) < _GALLERY_YEARS_CACHE_TTL:
        return cached[1]

    counts_by_year: dict[int, int] = {}

    # 1. 检索归档消息配图年份
    if source in {"all", "message"}:
        try:
            m_years = _archive.get_gallery_message_years(member_dir=member if member else None)
            for y, c in m_years.items():
                if 2010 <= y <= 2035:
                    counts_by_year[y] = counts_by_year.get(y, 0) + c
        except Exception:
            pass

    # 2. 检索博客配图年份
    if source in {"all", "blog"}:
        try:
            from src.webui_modules.archive.common import _blog_table_columns
            from src.webui_modules.archive_handlers import get_blog_db
            blog_db = get_blog_db()
            if blog_db:
                cols = _blog_table_columns(blog_db)
                img_where, json_target = _get_blog_image_expr(cols)
                where = [img_where, "p.date IS NOT NULL", "LENGTH(p.date) >= 4"]
                params: list[object] = []
                if member:
                    norm_m = member.replace(" ", "").replace("　", "").replace("_", "")
                    where.append("REPLACE(REPLACE(REPLACE(p.author, ' ', ''), '　', ''), '_', '') = ?")
                    params.append(norm_m)
                where_str = " AND ".join(where)

                try:
                    sql = f"""
                        SELECT CAST(substr(p.date, 1, 4) AS INTEGER) AS y, COUNT(*)
                        FROM blog_posts p, json_each({json_target}) j
                        WHERE {where_str} AND j.value IS NOT NULL AND j.value != ''
                        GROUP BY y
                        ORDER BY y DESC;
                    """
                    b_rows = blog_db.execute(sql, params).fetchall()
                    for r in b_rows:
                        if r[0]:
                            y = int(r[0])
                            if 2010 <= y <= 2035:
                                counts_by_year[y] = counts_by_year.get(y, 0) + int(r[1])
                except Exception:
                    # 降级：若不支持 json_each
                    select_cols = ["p.date"]
                    if "images_json" in cols:
                        select_cols.append("p.images_json")
                    if "image_paths_json" in cols:
                        select_cols.append("p.image_paths_json")
                    b_posts = blog_db.execute(f"SELECT {', '.join(select_cols)} FROM blog_posts p WHERE {where_str};", params).fetchall()
                    for row in b_posts:
                        dt = row[0] or ""
                        if len(dt) >= 4 and dt[:4].isdigit():
                            y = int(dt[:4])
                            if not (2010 <= y <= 2035):
                                continue
                            c = 0
                            for j_raw in row[1:]:
                                if j_raw:
                                    try:
                                        imgs = json.loads(j_raw)
                                        if isinstance(imgs, list) and imgs:
                                            c = len([img for img in imgs if img])
                                            break
                                    except Exception:
                                        pass
                            if c > 0:
                                counts_by_year[y] = counts_by_year.get(y, 0) + c
        except Exception:
            pass

    sorted_years = sorted(counts_by_year.keys(), reverse=True)
    years_data = [{"year": y, "count": counts_by_year[y]} for y in sorted_years]
    total = sum(counts_by_year.values())

    res = {
        "ok": True,
        "member": member,
        "source": source,
        "years": years_data,
        "total": total,
    }
    _gallery_years_cache[cache_key] = (now, res)
    return res


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
                from src.webui_modules.archive.common import _blog_table_columns
                b_cols = _blog_table_columns(blog_db)
                where_expr, json_target = _get_blog_image_expr(b_cols)
                try:
                    b_rows = blog_db.execute(f"""
                        SELECT p.group_key, p.author, COUNT(*)
                        FROM blog_posts p, json_each({json_target}) j
                        WHERE {where_expr} AND j.value IS NOT NULL AND j.value != ''
                        GROUP BY p.group_key, p.author;
                    """).fetchall()
                except Exception:
                    select_cols = ["group_key", "author"]
                    if "images_json" in b_cols:
                        select_cols.append("images_json")
                    if "image_paths_json" in b_cols:
                        select_cols.append("image_paths_json")
                    b_posts = blog_db.execute(f"""
                        SELECT {', '.join(select_cols)}
                        FROM blog_posts
                        WHERE {where_expr};
                    """).fetchall()
                    counts_dict = {}
                    for row in b_posts:
                        g_k = row[0]
                        auth = row[1]
                        if not auth:
                            continue
                        c = 0
                        for j_raw in row[2:]:
                            if j_raw:
                                try:
                                    imgs = json.loads(j_raw)
                                    if isinstance(imgs, list) and imgs:
                                        c = len([img for img in imgs if img])
                                        break
                                except Exception:
                                    pass
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
