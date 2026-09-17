"""
src/webui_modules/archive/gallery.py — 纯享美图画廊服务
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

from src import archive as _archive
from src.webui_modules.archive.common import _blog_media_url, _send_json_resp


def handle_gallery(handler, sub: str, guard_fn, read_body_json_fn) -> bool:
    """处理相册画廊子路由，命中返回 True，未命中返回 False。"""
    if sub != "gallery":
        return False

    if not guard_fn(need_admin=False):
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

    # 查 Message 图片（或 all 模式下的主要图片）
    res = _archive.get_gallery_photos(
        member_dir=member,
        source=source,
        page=page,
        per_page=per_page,
        year=year,
        month=month,
    )

    # 若为 all 模式且为第 1 页，附带部分博客美图作为多样化展示
    if source == "all" and res.get("ok"):
        blog_data = _get_blog_gallery(member=member, page=1, per_page=12, year=year, month=month)
        if blog_data.get("photos"):
            # 将博客图与消息图按发布时间倒序归并
            combined = sorted(
                res.get("photos", []) + blog_data.get("photos", []),
                key=lambda x: str(x.get("published_at") or ""),
                reverse=True,
            )
            res["photos"] = combined[:per_page]
            res["total"] = res.get("total", 0) + blog_data.get("total", 0)
            res["total_pages"] = (res["total"] + per_page - 1) // per_page if res["total"] > 0 else 1
            res["has_more"] = page < res["total_pages"]

    _send_json_resp(handler, res)
    return True


def _get_blog_gallery(
    member: str = "",
    page: int = 1,
    per_page: int = 40,
    year: int | None = None,
    month: int | None = None,
) -> dict:
    """从 blog.db 检索博客配图。"""
    try:
        from src.webui_modules.archive_handlers import get_blog_db
        blog_db = get_blog_db()
        if not blog_db:
            return {"ok": True, "total": 0, "photos": [], "page": page, "per_page": per_page, "has_more": False}

        where = ["image_paths_json IS NOT NULL", "image_paths_json != '[]'", "image_paths_json != ''"]
        params: list[object] = []

        if member:
            norm_m = member.replace(" ", "").replace("　", "").replace("_", "")
            where.append("REPLACE(REPLACE(REPLACE(author, ' ', ''), '　', ''), '_', '') = ?")
            params.append(norm_m)
        if year:
            y_str = f"{year:04d}"
            where.append("substr(date, 1, 4) = ?")
            params.append(y_str)
        if month and year:
            ym_str = f"{year:04d}-{month:02d}"
            where.append("substr(date, 1, 7) = ?")
            params.append(ym_str)

        where_str = " AND ".join(where)

        posts_sql = f"""
            SELECT id, group_key, author, title, date, image_paths_json
            FROM blog_posts
            WHERE {where_str}
            ORDER BY date DESC, id DESC;
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
