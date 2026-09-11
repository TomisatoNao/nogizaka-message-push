"""
src/archive_query.py — 归档数据多维检索与日历统计分析引擎

将归档系统只读查询、FTS5 全文索引检索、粉丝信件查询与日历统计算法
从底层消息管道写入模块中抽离，降低单个模块复杂度，提升可测试性。
"""

from datetime import datetime
import json
from pathlib import Path
import sqlite3

from src.logger import log_all

_MEDIA_TYPES = ("picture", "image", "video", "voice", "audio")
_count_cache: dict[str, tuple[float, int]] = {}
_day_cache: dict[str, tuple[float, dict[str, dict[str, int]]]] = {}


def _get_archive_root() -> Path:
    from src.archive import archive_root
    return archive_root()


def _get_init_db() -> sqlite3.Connection | None:
    from src.archive import init_db
    return init_db()


def _get_has_fts5() -> bool:
    from src import archive
    return getattr(archive, "_has_fts5", False)


def get_existing_letter_ids(member_dir: str) -> set[int]:
    """获取指定成员已归档的所有信件 ID 集合。"""
    conn = _get_init_db()
    if conn:
        try:
            rows = conn.execute("SELECT id FROM letters WHERE member_dir = ?;", (member_dir,)).fetchall()
            return {int(r[0]) for r in rows}
        except (sqlite3.Error, ValueError) as ex:
            log_all(f"⚠️ 查询信件 ID 集合异常: {ex}", is_debug=True)
    letters = get_archive_letters(member_dir)
    return {int(item.get("id")) for item in letters if item.get("id")}


def get_archive_letters(member_dir: str) -> list[dict]:
    """从 SQLite 获取指定成员已归档的所有粉丝信件（新在前）。"""
    conn = _get_init_db()
    if conn:
        try:
            rows = conn.execute("""
                SELECT id, group_id, member_name, member_dir, created_at, updated_at, text, file_url, local_file, thumbnail_url, is_favorite, raw_json
                FROM letters
                WHERE member_dir = ?
                ORDER BY created_at DESC;
            """, (member_dir,)).fetchall()
            if rows:
                out = []
                for r in rows:
                    out.append({
                        "id": r[0],
                        "group_id": r[1],
                        "member_name": r[2],
                        "member_dir": r[3],
                        "created_at": r[4],
                        "updated_at": r[5],
                        "text": r[6],
                        "file_url": r[7],
                        "local_file": r[8],
                        "thumbnail_url": r[9],
                        "is_favorite": bool(r[10]),
                    })
                return out
        except Exception as ex:
            log_all(f"⚠️ 从 SQLite 读取信件失败 ({member_dir}): {ex}", is_debug=True)

    # 兜底：从 letters.json 读取
    json_path = _get_archive_root() / member_dir / "letters" / "letters.json"
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError) as ex:
            log_all(f"⚠️ 读取 letters.json 异常 ({member_dir}): {ex}", is_debug=True)
    return []


def get_letters_count(member_dir: str) -> int:
    """获取指定成员已归档的信件数量。"""
    conn = _get_init_db()
    if conn:
        try:
            row = conn.execute("SELECT COUNT(*) FROM letters WHERE member_dir = ?;", (member_dir,)).fetchone()
            if row:
                return row[0]
        except sqlite3.Error as ex:
            log_all(f"⚠️ 查询信件数量异常: {ex}", is_debug=True)
    letters = get_archive_letters(member_dir)
    return len(letters)


def get_letters_counts(member_dirs: list[str] | tuple[str, ...]) -> dict[str, int]:
    """批量返回成员信件归档数量，避免成员选择器逐项查询数据库。"""
    names = [str(name) for name in member_dirs if name]
    if not names:
        return {}
    conn = _get_init_db()
    if conn:
        try:
            placeholders = ",".join("?" for _ in names)
            rows = conn.execute(
                f"SELECT member_dir, COUNT(*) FROM letters WHERE member_dir IN ({placeholders}) GROUP BY member_dir",
                names,
            ).fetchall()
            counts = {str(row[0]): int(row[1] or 0) for row in rows}
            return {name: counts.get(name, 0) for name in names}
        except sqlite3.Error as ex:
            log_all(f"⚠️ 批量查询信件数量异常: {ex}", is_debug=True)
    return {name: get_letters_count(name) for name in names}


def list_members() -> list[str]:
    """返回所有有归档消息的成员列表。优先从 SQLite 获取。"""
    conn = _get_init_db()
    if conn:
        try:
            rows = conn.execute("SELECT DISTINCT member_dir FROM messages ORDER BY member_dir ASC;").fetchall()
            if rows:
                return [r[0] for r in rows]
        except sqlite3.Error as ex:
            log_all(f"⚠️ 查询归档成员列表异常: {ex}", is_debug=True)

    root = _get_archive_root()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())


def _month_count(json_path: Path) -> int:
    try:
        mtime = json_path.stat().st_mtime
    except OSError:
        return 0
    key = str(json_path)
    cached = _count_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            count = len(json.load(f))
    except (OSError, ValueError):
        count = 0
    _count_cache[key] = (mtime, count)
    return count


def list_months(
    member_dir: str,
    type_filter: set[str] | None = None,
    id_filter: set[str] | None = None,
) -> list[dict]:
    """返回 [{year, month, count}]，新的在前。支持 mtime 缓存与类型/收藏复合统计。"""
    root = _get_archive_root() / member_dir
    base_months: list[dict] = []
    if root.is_dir():
        for year_dir in sorted((d for d in root.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True):
            for month_dir in sorted((d for d in year_dir.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True):
                json_path = month_dir / "messages.json"
                if not json_path.is_file():
                    continue
                base_months.append({
                    "year": int(year_dir.name),
                    "month": int(month_dir.name),
                    "count": _month_count(json_path),
                })
    if not base_months:
        conn = _get_init_db()
        if conn:
            try:
                rows = conn.execute("""
                    SELECT year, month, COUNT(*) FROM messages
                    WHERE member_dir = ?
                    GROUP BY year, month
                    ORDER BY year DESC, month DESC;
                """, (member_dir,)).fetchall()
                if rows:
                    base_months = [{"year": r[0], "month": r[1], "count": r[2]} for r in rows]
            except Exception:
                pass

    if not base_months:
        return []

    # 若没有任何筛选条件，直接返回基础月份统计
    if type_filter is None and id_filter is None:
        return base_months

    # 若指定了收藏 ID 过滤且集合为空，各月份计数均为 0
    if id_filter is not None and len(id_filter) == 0:
        return [{"year": m["year"], "month": m["month"], "count": 0} for m in base_months]

    counts_map: dict[tuple[int, int], int] | None = {}
    conn = _get_init_db()
    if conn:
        try:
            if id_filter is not None:
                chunk = list(id_filter)
                for start in range(0, len(chunk), 400):
                    sub_ids = chunk[start:start + 400]
                    placeholders = ",".join("?" for _ in sub_ids)
                    params: list[any] = [member_dir, *sub_ids]
                    sql = f"SELECT year, month, COUNT(*) FROM messages WHERE member_dir = ? AND id IN ({placeholders})"
                    if type_filter:
                        t_placeholders = ",".join("?" for _ in type_filter)
                        sql += f" AND type IN ({t_placeholders})"
                        params.extend(list(type_filter))
                    sql += " GROUP BY year, month;"
                    rows = conn.execute(sql, params).fetchall()
                    for y, mo, c in rows:
                        counts_map[(y, mo)] = counts_map.get((y, mo), 0) + c
            else:
                placeholders = ",".join("?" for _ in type_filter)
                rows = conn.execute(
                    f"SELECT year, month, COUNT(*) FROM messages WHERE member_dir = ? AND type IN ({placeholders}) GROUP BY year, month;",
                    [member_dir, *type_filter],
                ).fetchall()
                for y, mo, c in rows:
                    counts_map[(y, mo)] = c
        except Exception as e:
            log_all(f"⚠️ SQLite 复合月份计数失败，降级扫描 JSON: {e}", is_debug=True)
            counts_map = None

    if counts_map is None:
        counts_map = {}
        for m in base_months:
            json_path = root / str(m["year"]) / f"{m['month']:02d}" / "messages.json"
            if not json_path.is_file():
                json_path = root / str(m["year"]) / str(m["month"]) / "messages.json"
            if not json_path.is_file():
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    msgs = json.load(f)
            except (OSError, ValueError):
                msgs = []
            c = 0
            for msg in msgs:
                if id_filter is not None and str(msg.get("id")) not in id_filter:
                    continue
                if type_filter is not None and msg.get("type") not in type_filter:
                    continue
                c += 1
            counts_map[(m["year"], m["month"])] = c

    return [
        {"year": m["year"], "month": m["month"], "count": counts_map.get((m["year"], m["month"]), 0)}
        for m in base_months
    ]


def _jst_date(utc_str: str) -> str:
    """UTC 时间串 → JST 日期串 YYYY-MM-DD（解析失败返回空串）。"""
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""
    from datetime import timedelta
    return (dt + timedelta(hours=9)).strftime("%Y-%m-%d")


def day_counts(
    member_dir: str,
    type_filter: set[str] | None = None,
    id_filter: set[str] | None = None,
) -> dict[str, int]:
    """全档按 JST 日期统计消息数（日历视图用），支持类型与收藏复合过滤。
    type_filter 为 None 统计全部类型，否则只计入指定类型集合。
    id_filter 为 None 统计全部消息，否则只计入指定 ID 集合（如用户收藏）。"""
    out: dict[str, int] = {}
    if id_filter is not None and len(id_filter) == 0:
        return out

    if id_filter is not None:
        conn = _get_init_db()
        if conn:
            try:
                chunk = list(id_filter)
                for start in range(0, len(chunk), 400):
                    sub_ids = chunk[start:start + 400]
                    placeholders = ",".join("?" for _ in sub_ids)
                    params: list[any] = [member_dir, *sub_ids]
                    sql = f"SELECT published_at, updated_at, type FROM messages WHERE member_dir = ? AND id IN ({placeholders})"
                    if type_filter:
                        t_placeholders = ",".join("?" for _ in type_filter)
                        sql += f" AND type IN ({t_placeholders})"
                        params.extend(list(type_filter))
                    rows = conn.execute(sql, params).fetchall()
                    for r in rows:
                        d = _jst_date(r[0] or r[1] or "")
                        if d:
                            out[d] = out.get(d, 0) + 1
                return out
            except Exception as e:
                log_all(f"⚠️ SQLite 复合日历统计失败，降级扫描 JSON: {e}", is_debug=True)

        root = _get_archive_root() / member_dir
        if not root.is_dir():
            return out
        for json_path in root.glob("[0-9]*/[0-9]*/messages.json"):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    msgs = json.load(f)
            except (OSError, ValueError):
                continue
            for m in msgs:
                if str(m.get("id")) not in id_filter:
                    continue
                if type_filter is not None and m.get("type") not in type_filter:
                    continue
                d = _jst_date(m.get("published_at") or m.get("updated_at", ""))
                if d:
                    out[d] = out.get(d, 0) + 1
        return out

    # id_filter is None: 常规统计模式（逐月 mtime 缓存）
    root = _get_archive_root() / member_dir
    if not root.is_dir():
        return out
    for json_path in root.glob("[0-9]*/[0-9]*/messages.json"):
        try:
            mtime = json_path.stat().st_mtime
        except OSError:
            continue
        key = str(json_path)
        cached = _day_cache.get(key)
        if cached and cached[0] == mtime:
            month_counts = cached[1]
        else:
            month_counts = {}
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    msgs = json.load(f)
            except (OSError, ValueError):
                msgs = []
            for m in msgs:
                d = _jst_date(m.get("published_at") or m.get("updated_at", ""))
                if d:
                    by_type = month_counts.setdefault(d, {})
                    t = m.get("type", "text")
                    by_type[t] = by_type.get(t, 0) + 1
            _day_cache[key] = (mtime, month_counts)
        for d, by_type in month_counts.items():
            n = sum(c for t, c in by_type.items()
                    if type_filter is None or t in type_filter)
            if n:
                out[d] = out.get(d, 0) + n
    return out


def search(member_dir: str, query: str, type_filter: set[str] | None = None,
           limit: int = 500, order: str = "desc") -> list[dict]:
    """跨月搜索：优先使用 SQLite 原生 SQL 子串匹配，无 DB 或被 mock 时降级为 JSON 遍历。
    原文与译文都参与匹配，空格分词取 AND 语义。
    返回附加 _year/_month 字段的消息列表，最多 limit 条。

    ``order`` 仅接受 ``asc`` / ``desc``。默认新消息在前，保持搜索接口
    原有行为；Web UI 会将用户选择的阅读顺序显式传入。
    """
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        return []
    if order not in {"asc", "desc"}:
        order = "desc"

    from src import archive
    is_mocked = getattr(archive, "list_months", None) is not getattr(archive, "_ORIGINAL_LIST_MONTHS", None)
    if not is_mocked:
        conn = _get_init_db()
        if conn:
            try:
                possible_dirs = {
                    member_dir,
                    member_dir.replace("_", ""),
                    member_dir.replace(" ", ""),
                    member_dir.replace("　", ""),
                    member_dir.replace("_", " "),
                    member_dir.replace(" ", "_"),
                }
                try:
                    from src.archive import member_dir_name
                    possible_dirs.add(member_dir_name(member_dir))
                except Exception:
                    pass
                dir_list = [d for d in possible_dirs if d]

                placeholders_dirs = ",".join("?" * len(dir_list))
                where_member = f"(member_dir IN ({placeholders_dirs}) OR member_name IN ({placeholders_dirs}))"
                member_params: list[str | int] = list(dir_list) + list(dir_list)

                has_member = conn.execute(
                    f"SELECT 1 FROM messages WHERE {where_member} LIMIT 1;",
                    member_params
                ).fetchone()

                if has_member:
                    where_clauses = [where_member]
                    params: list[str | int] = list(member_params)

                    if type_filter:
                        placeholders_types = ",".join("?" * len(type_filter))
                        where_clauses.append(f"type IN ({placeholders_types})")
                        params.extend(list(type_filter))

                    for t in terms:
                        escaped_t = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                        pat = f"%{escaped_t}%"
                        where_clauses.append(
                            "(lower(COALESCE(text, '')) LIKE ? ESCAPE '\\' OR "
                            "lower(COALESCE(translation, '')) LIKE ? ESCAPE '\\' OR "
                            "lower(COALESCE(tags, '')) LIKE ? ESCAPE '\\')"
                        )
                        params.extend([pat, pat, pat])

                    direction = "ASC" if order == "asc" else "DESC"
                    sql = f"""
                        SELECT raw_json, year, month
                        FROM messages
                        WHERE {' AND '.join(where_clauses)}
                        ORDER BY updated_at {direction}, id {direction}
                        LIMIT ?
                    """
                    params.append(limit)

                    cursor = conn.cursor()
                    cursor.execute(sql, params)
                    results = []
                    for raw, yr, mo in cursor.fetchall():
                        try:
                            msg = json.loads(raw)
                            results.append({**msg, "_year": yr, "_month": mo})
                        except (ValueError, TypeError):
                            continue
                    return results
            except Exception as e:
                log_all(f"⚠️ SQLite 搜索异常，降级回 JSON 文件匹配: {e}", is_debug=True)

    list_months_fn = getattr(archive, "list_months", list_months)
    load_month_fn = getattr(archive, "load_month", None)
    if load_month_fn is None:
        from src.archive import load_month as load_month_fn
    results: list[dict] = []
    month_list = list(list_months_fn(member_dir))
    if order == "asc":
        month_list.reverse()
    for m in month_list:
        msgs = load_month_fn(member_dir, m["year"], m["month"])
        iterable = msgs if order == "asc" else reversed(msgs)
        for msg in iterable:
            if type_filter and msg.get("type") not in type_filter:
                continue
            haystack = "\n".join([
                msg.get("text") or "",
                msg.get("_translation") or "",
                msg.get("_tags") or "",
                msg.get("_custom_tags") or "",
            ]).lower()
            if all(t in haystack for t in terms):
                results.append({**msg, "_year": m["year"], "_month": m["month"]})
                if len(results) >= limit:
                    return results
    return results


def load_archived_ids(m_name: str) -> tuple[set[str], set[str]]:
    """返回 (已归档 ID, 媒体下载失败的 ID)。回填工具用于跳过与重试。"""
    from src.archive import _member_root
    root = _member_root(m_name)
    ok_ids: set[str] = set()
    fail_ids: set[str] = set()
    if not root.is_dir():
        return ok_ids, fail_ids
    for json_path in root.glob("[0-9]*/[0-9]*/messages.json"):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                msgs = json.load(f)
        except (OSError, ValueError):
            continue
        for m in msgs:
            mid = str(m.get("id", ""))
            if not mid:
                continue
            incomplete = (
                m.get("_download_failed")
                or (m.get("type") in _MEDIA_TYPES and m.get("file") and not m.get("_local_file"))
            )
            (fail_ids if incomplete else ok_ids).add(mid)
    return ok_ids, fail_ids
