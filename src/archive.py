# ============================================================
# archive.py — 消息归档：按 成员/年/月 落地 messages.json 与媒体文件
# ============================================================
# 目录布局（借鉴 nogizaka-message-archive，修正其反斜杠路径问题）：
#   {ARCHIVE_DIR}/{成员名}/{YYYY}/{MM}/
#       messages.json     # 该月全部消息（含 _translation / _local_file 附加字段）
#       images/ videos/ audio/ others/
# 媒体文件名: {YYYYMMDD_HHMMSS}_{消息id}{ext}（UTC updated_at）
#
# 写入语义：
#   - 幂等合并：按消息 id 合并进当月 JSON，按 updated_at 排序，原子写
#   - 两段式：先落 JSON（保住消息本体），再下载媒体回填 _local_file
#     —— 进程中途退出最多丢媒体文件，不丢消息；失败标记 _download_failed
#     供回填工具重试
#   - 所有相对路径使用正斜杠（跨平台，网页端直接可用）
# ============================================================
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

import config.config as cfg
from src.logger import log_all

_BASE_DIR = Path(__file__).resolve().parent.parent
_FILENAME_ILLEGAL = re.compile(r'[<>:"/\\|?*]')

_EXT_MAP = {

    "image/jpeg":      ".jpg",
    "image/png":       ".png",
    "image/gif":       ".gif",
    "image/webp":      ".webp",
    "video/mp4":       ".mp4",
    "video/quicktime": ".mov",
    "audio/mp4":       ".m4a",
    "audio/mpeg":      ".mp3",
    "audio/ogg":       ".ogg",
    "audio/webm":      ".webm",
    "audio/x-m4a":     ".m4a",
    "audio/aac":       ".aac",
}

_MEDIA_TYPES = {"image", "picture", "video", "voice"}

# ---- 模块级状态 ----
_media_client: httpx.AsyncClient | None = None
_media_sem: asyncio.Semaphore | None = None
_write_locks: dict[str, asyncio.Lock] = {}
_bg_tasks: set = set()

_sqlite_conn: sqlite3.Connection | None = None
_has_fts5: bool = False


def get_db_path() -> Path:
    return archive_root() / "archive.db"


def init_db() -> sqlite3.Connection | None:
    """初始化 SQLite 归档数据库与 FTS5 全文索引表。"""
    global _sqlite_conn, _has_fts5
    if _sqlite_conn is not None:
        return _sqlite_conn

    db_file = get_db_path()
    try:
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file), timeout=10.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                member_name TEXT NOT NULL,
                member_dir TEXT NOT NULL,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                type TEXT,
                published_at TEXT,
                updated_at TEXT,
                text TEXT,
                translation TEXT,
                tags TEXT,
                local_file TEXT,
                raw_json TEXT NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_member_year_month ON messages(member_dir, year, month);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_updated_at ON messages(updated_at DESC);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_ids (
                group_type TEXT NOT NULL,
                m_id TEXT NOT NULL,
                msg_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (group_type, m_id, msg_id)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timeline_watermarks (
                group_type TEXT NOT NULL,
                m_id TEXT NOT NULL,
                last_time TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (group_type, m_id)
            );
        """)
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    id UNINDEXED,
                    member_dir UNINDEXED,
                    member_name,
                    text,
                    translation,
                    tags,
                    tokenize='unicode61'
                );
            """)
            _has_fts5 = True
        except sqlite3.OperationalError:
            _has_fts5 = False

        conn.commit()

        # 自动无缝平滑迁移旧版 data/time_records/*.txt 到 timeline_watermarks
        time_dir = _BASE_DIR / "data" / "time_records"
        if time_dir.exists() and time_dir.is_dir():
            try:
                import time as _t
                for f in list(time_dir.glob("time_*.txt")):
                    parts = f.stem.split("_")
                    if len(parts) >= 3:
                        g_type = parts[1]
                        m_id = "_".join(parts[2:])
                        with open(f, "r", encoding="utf-8") as tf:
                            val = tf.read().strip()
                        if val:
                            conn.execute(
                                "INSERT INTO timeline_watermarks (group_type, m_id, last_time, updated_at) VALUES (?, ?, ?, ?) "
                                "ON CONFLICT(group_type, m_id) DO UPDATE SET last_time = excluded.last_time, updated_at = excluded.updated_at;",
                                (g_type, m_id, val, _t.time())
                            )
                        try:
                            f.unlink()
                        except OSError:
                            pass
                conn.commit()
                try:
                    if not any(time_dir.iterdir()):
                        time_dir.rmdir()
                except OSError:
                    pass
            except Exception:
                pass

        _sqlite_conn = conn
        return _sqlite_conn
    except Exception as e:
        log_all(f"⚠️ SQLite 数据库初始化失败: {e}", is_error=True)
        return None


def get_timeline_watermark(group_type: str, m_id: str) -> str | None:
    """从数据库读取成员上次成功抓取的最后时间戳水位线。"""
    conn = init_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_time FROM timeline_watermarks WHERE group_type = ? AND m_id = ?",
            (group_type, m_id)
        )
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else None
    except Exception:
        return None


def set_timeline_watermark(group_type: str, m_id: str, last_time: str) -> None:
    """持久化成员最新抓取时间戳水位线到 SQLite 数据库。"""
    conn = init_db()
    if not conn or not last_time:
        return
    import time
    try:
        with conn:
            conn.execute(
                "INSERT INTO timeline_watermarks (group_type, m_id, last_time, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(group_type, m_id) DO UPDATE SET last_time = excluded.last_time, updated_at = excluded.updated_at;",
                (group_type, m_id, str(last_time), time.time())
            )
    except Exception as e:
        log_all(f"⚠️ 保存时间戳水位线失败 ({group_type}_{m_id}): {e}", is_debug=True)


def _save_msgs_to_sqlite(member_name: str, year: int, month: int, msgs: list[dict]) -> None:
    """同步一组消息至 SQLite 数据库与 FTS 索引。"""
    conn = init_db()
    if not conn or not msgs:
        return
    m_dir = member_dir_name(member_name)
    rows = []
    fts_rows = []
    for msg in msgs:
        rid = str(msg.get("id", ""))
        if not rid:
            continue
        txt = msg.get("text") or ""
        trans = msg.get("_translation") or ""
        tags = (msg.get("_tags") or "") + " " + (msg.get("_custom_tags") or "")
        loc_file = msg.get("_local_file") or ""
        pub_at = msg.get("published_at") or msg.get("updated_at") or ""
        upd_at = msg.get("updated_at") or pub_at
        msg_type = msg.get("type") or "text"
        raw = json.dumps(msg, ensure_ascii=False)

        rows.append((rid, member_name, m_dir, year, month, msg_type, pub_at, upd_at, txt, trans, tags, loc_file, raw))
        if _has_fts5:
            fts_rows.append((rid, m_dir, member_name, txt, trans, tags))

    try:
        with conn:
            conn.executemany("""
                INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, updated_at, text, translation, tags, local_file, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    member_name=excluded.member_name,
                    translation=excluded.translation,
                    tags=excluded.tags,
                    local_file=excluded.local_file,
                    raw_json=excluded.raw_json;
            """, rows)
            if _has_fts5 and fts_rows:
                conn.executemany("DELETE FROM messages_fts WHERE id = ?;", [(r[0],) for r in fts_rows])
                conn.executemany("""
                    INSERT INTO messages_fts (id, member_dir, member_name, text, translation, tags)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, fts_rows)
    except Exception as e:
        log_all(f"⚠️ SQLite 保存消息数据失败: {e}", is_error=True)


def sync_all_to_sqlite() -> int:
    """自动扫描磁盘下所有历史归档 JSON，高性能单事务批量全量同步导入 SQLite 数据库。"""
    root = archive_root()
    if not root.is_dir():
        return 0

    conn = init_db()
    if not conn:
        return 0

    all_rows = []
    fts_rows = []
    for member_dir in root.iterdir():
        if not member_dir.is_dir():
            continue
        m_name = member_dir.name
        m_dirname = member_dir_name(m_name)
        for year_dir in member_dir.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            year = int(year_dir.name)
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir() or not month_dir.name.isdigit():
                    continue
                month = int(month_dir.name)
                json_path = month_dir / "messages.json"
                if not json_path.is_file():
                    continue

                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        msgs = json.load(f)
                    for msg in msgs:
                        rid = str(msg.get("id", ""))
                        if not rid:
                            continue
                        txt = msg.get("text") or ""
                        trans = msg.get("_translation") or ""
                        tags = (msg.get("_tags") or "") + " " + (msg.get("_custom_tags") or "")
                        loc_file = msg.get("_local_file") or ""
                        pub_at = msg.get("published_at") or msg.get("updated_at") or ""
                        upd_at = msg.get("updated_at") or pub_at
                        msg_type = msg.get("type") or "text"
                        raw = json.dumps(msg, ensure_ascii=False)

                        all_rows.append((rid, m_name, m_dirname, year, month, msg_type, pub_at, upd_at, txt, trans, tags, loc_file, raw))
                        if _has_fts5:
                            fts_rows.append((rid, m_dirname, m_name, txt, trans, tags))
                except Exception as e:
                    log_all(f"⚠️ 无法同步归档 {json_path}: {e}", is_error=True)

    if all_rows:
        try:
            with conn:
                conn.executemany("""
                    INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, updated_at, text, translation, tags, local_file, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        member_name=excluded.member_name,
                        translation=excluded.translation,
                        tags=excluded.tags,
                        local_file=excluded.local_file,
                        raw_json=excluded.raw_json;
                """, all_rows)
                if _has_fts5 and fts_rows:
                    conn.executemany("DELETE FROM messages_fts WHERE id = ?;", [(r[0],) for r in fts_rows])
                    conn.executemany("""
                        INSERT INTO messages_fts (id, member_dir, member_name, text, translation, tags)
                        VALUES (?, ?, ?, ?, ?, ?);
                    """, fts_rows)
        except Exception as e:
            log_all(f"⚠️ SQLite 批量全量保存失败: {e}", is_error=True)

    log_all(f"💾 SQLite 归档全量同步完成，共计同步 {len(all_rows)} 条记录")
    return len(all_rows)


def initialize(client: httpx.AsyncClient) -> None:
    """注入共享 HTTP 客户端（媒体下载用）并初始化 SQLite 数据库同步。"""
    global _media_client, _media_sem
    _media_client = client
    _media_sem = asyncio.Semaphore(3)
    init_db()
    sync_all_to_sqlite()



# ──────────────────────────────────────────────
# 路径工具
# ──────────────────────────────────────────────
def member_dir_name(m_name: str) -> str:
    """返回成员在归档根目录下的子文件夹名。

    1. 智能复用：优先匹配磁盘上已存在的归档目录（忽略全半角空格与下划线差异），
       防止修改成员显示名（如 '冨里奈央' 变为 '冨里 奈央'）时导致归档数据分叉。
    2. 新建目录：默认去除空格生成紧凑规范的目录名。
    """
    root = archive_root()
    norm = _FILENAME_ILLEGAL.sub("", m_name.replace(" ", "").replace("　", "").replace("_", ""))
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir():
                d_norm = _FILENAME_ILLEGAL.sub("", d.name.replace(" ", "").replace("　", "").replace("_", ""))
                if d_norm == norm:
                    return d.name
    return _FILENAME_ILLEGAL.sub("_", m_name.replace(" ", "_").replace("　", "_"))


def archive_root() -> Path:
    return Path(cfg.ARCHIVE_DIR)


def _member_root(m_name: str) -> Path:
    return archive_root() / member_dir_name(m_name)


def _month_dir(m_name: str, dt: datetime) -> Path:
    return _member_root(m_name) / f"{dt.year:04d}" / f"{dt.month:02d}"


JST_TZ = timezone(timedelta(hours=9))


def parse_jst_datetime(ts_str: str) -> datetime:
    """将时间戳统一解析为日本标准时间 (JST, UTC+9) 的 datetime 对象。"""
    if not ts_str:
        raise ValueError("Empty timestamp string")
    s = str(ts_str).strip()
    if s.endswith("Z"):
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(JST_TZ)
    if "+" in s or (s.count("-") >= 3 and "T" in s):
        dt = datetime.fromisoformat(s)
        return dt.astimezone(JST_TZ)
    s_iso = s.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s_iso).replace(tzinfo=timezone.utc)
        return dt.astimezone(JST_TZ)
    except Exception:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(JST_TZ)


def _parse_utc(utc_str: str) -> datetime:
    """兼容旧接口：返回 JST 归档时间的 datetime。"""
    return parse_jst_datetime(utc_str)


def _media_subdir(msg_type: str) -> str:
    if msg_type in ("image", "picture"):
        return "images"
    if msg_type == "video":
        return "videos"
    if msg_type == "voice":
        return "audio"
    return "others"


def _guess_extension(url: str, content_type: str | None) -> str:
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in _EXT_MAP:
            return _EXT_MAP[ct]
    url_path = url.split("?")[0]
    ext = os.path.splitext(url_path)[1]
    if ext and len(ext) <= 5 and ext.isascii():
        return ext.lower()
    return ".bin"


def _sniff_content_type(path: Path) -> str | None:
    """魔数嗅探：voice/video 的 URL 后缀经常是骗人的。"""
    try:
        header = path.open("rb").read(12)
    except OSError:
        return None
    if header[4:8] == b"ftyp":
        return "video/mp4"
    if header[:3] == b"ID3" or header[:2] == b"\xff\xfb":
        return "audio/mpeg"
    if header[:4] == b"RIFF":
        return "audio/wav"
    if header[:4] == b"OggS":
        return "audio/ogg"
    return None


# ──────────────────────────────────────────────
# 月度 JSON 读写（幂等合并 + 原子写）
# ──────────────────────────────────────────────
def load_month(m_name: str, year: int, month: int) -> list[dict]:
    json_path = _member_root(m_name) / f"{year:04d}" / f"{month:02d}" / "messages.json"
    if not json_path.exists():
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except ValueError as e:
        # 文件损坏：改名保留现场再返回空，后续写入重建新文件——绝不静默丢弃旧数据
        rescue = json_path.with_name(f"messages.corrupt-{datetime.now():%Y%m%d%H%M%S}.json")
        try:
            os.replace(json_path, rescue)
            log_all(f"⚠️ 归档文件损坏，已改名保留: {rescue}（{e}）", is_error=True)
        except OSError:
            log_all(f"⚠️ 归档读取失败 {json_path}: {e}", is_error=True)
        return []
    except OSError as e:
        log_all(f"⚠️ 归档读取失败 {json_path}: {e}", is_error=True)
        return []


def _sync_write_json(tmp_path: Path, json_path: Path, data: list[dict]) -> None:
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, json_path)


async def _merge_write(m_name: str, dt: datetime, record: dict) -> None:
    """把单条记录按 id 合并进当月 messages.json（新字段覆盖旧字段）。"""
    key = f"{member_dir_name(m_name)}/{dt.year:04d}/{dt.month:02d}"
    lock = _write_locks.setdefault(key, asyncio.Lock())
    async with lock:
        month_path = _month_dir(m_name, dt)
        month_path.mkdir(parents=True, exist_ok=True)
        msgs = load_month(m_name, dt.year, dt.month)
        by_id = {str(m.get("id", "")): m for m in msgs}
        rid = str(record.get("id", ""))
        merged = {**by_id.get(rid, {}), **record}
        merged.pop("_download_failed", None)
        if record.get("_download_failed"):
            merged["_download_failed"] = True
        by_id[rid] = merged
        out = sorted(by_id.values(), key=lambda m: m.get("updated_at", ""))
        json_path = month_path / "messages.json"
        tmp = json_path.with_suffix(".json.tmp")
        await asyncio.to_thread(_sync_write_json, tmp, json_path, out)
        _save_msgs_to_sqlite(m_name, dt.year, dt.month, [merged])




# ──────────────────────────────────────────────
# 媒体下载
# ──────────────────────────────────────────────
async def _download_media(m_name: str, dt: datetime, msg: dict) -> dict:
    """下载消息媒体，返回带 _local_file / _download_failed 字段的增量记录。"""
    file_url = msg.get("file", "")
    msg_id = str(msg.get("id", "0"))
    ts = dt.strftime("%Y%m%d_%H%M%S")
    dest_dir = _month_dir(m_name, dt) / _media_subdir(msg.get("type", ""))
    dest_dir.mkdir(parents=True, exist_ok=True)
    member_root = _member_root(m_name)

    # 已有本地文件（按 时间戳_id 前缀匹配，扩展名无关）→ 直接复用
    for existing in os.listdir(dest_dir):
        if existing.startswith(f"{ts}_{msg_id}") and not existing.endswith(".tmp"):
            rel = (dest_dir / existing).relative_to(member_root).as_posix()
            return {"id": msg.get("id"), "updated_at": msg.get("updated_at"), "_local_file": rel}

    tmp_path = dest_dir / f"{ts}_{msg_id}.tmp"
    ok = False
    assert _media_sem is not None, "archive.initialize() 未调用"
    async with _media_sem:
        try:
            client = _media_client
            assert client is not None
            async with client.stream("GET", file_url, timeout=120) as resp:
                if resp.status_code == 200:
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(1 << 16):
                            f.write(chunk)
                    ok = True
                else:
                    log_all(f"⚠️ 归档媒体下载 HTTP {resp.status_code}: {file_url[:80]}", is_debug=True)
        except Exception as e:
            log_all(f"⚠️ 归档媒体下载失败: {file_url[:80]} — {type(e).__name__}: {e}", is_debug=True)

    if not ok:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"id": msg.get("id"), "updated_at": msg.get("updated_at"), "_download_failed": True}

    ext = _guess_extension(file_url, _sniff_content_type(tmp_path))
    final_path = dest_dir / f"{ts}_{msg_id}{ext}"
    os.replace(tmp_path, final_path)
    rel = final_path.relative_to(member_root).as_posix()
    return {"id": msg.get("id"), "updated_at": msg.get("updated_at"), "_local_file": rel}


# ──────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────
async def archive_message(member: dict, msg: dict, translated: str = "") -> None:
    """归档单条消息：按 JST 日本标准时间划分年月，先落 JSON，再下载媒体回填。幂等，可安全重复调用。"""
    m_name = member.get("m_name", "")
    ts_str = msg.get("published_at") or msg.get("updated_at", "")
    if not m_name or not ts_str:
        return
    try:
        dt = parse_jst_datetime(ts_str)
    except (ValueError, TypeError):
        log_all(f"⚠️ 归档跳过（无法解析时间戳 {ts_str!r}）", is_debug=True)
        return

    record = dict(msg)
    if translated:
        record["_translation"] = translated
    try:
        await _merge_write(m_name, dt, record)
        _local_file = ""
        if cfg.ARCHIVE_MEDIA and msg.get("file") and msg.get("type") in _MEDIA_TYPES:
            delta = await _download_media(m_name, dt, msg)
            await _merge_write(m_name, dt, delta)
            _local_file = delta.get("_local_file", "")
        # ── 图片后台打标签 ──
        if msg.get("type") in ("picture", "image") and msg.get("file") and _local_file:
            from src.tagger import schedule_tag as _schedule_tag
            _schedule_tag(member_dir_name(m_name), dict(msg, _local_file=_local_file))
    except Exception:
        import traceback
        log_all(f"⚠️ 归档失败 [{m_name}] id={msg.get('id')}:\n{traceback.format_exc()}", is_error=True)


def schedule_archive(member: dict, msg: dict, translated: str = "") -> None:
    """后台归档（不阻塞推送管线）。ARCHIVE_ENABLED 关闭时为 no-op。"""
    if not cfg.ARCHIVE_ENABLED:
        return
    task = asyncio.create_task(archive_message(member, msg, translated))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def wait_pending(timeout: float = 60) -> None:
    """等待后台归档任务收尾（优雅停机用）。"""
    if _bg_tasks:
        await asyncio.wait(list(_bg_tasks), timeout=timeout)


# ──────────────────────────────────────────────
# 查询（网页查看器 / 回填工具用）
# ──────────────────────────────────────────────
def list_members() -> list[str]:
    root = archive_root()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())


# 月度计数缓存：{json_path: (mtime, count)} —— members/months 接口每次请求
# 都要数全部月份，无缓存时随成员和月份数线性膨胀
_count_cache: dict[str, tuple[float, int]] = {}


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


def list_months(member_dir: str) -> list[dict]:
    """返回 [{year, month, count}]，新的在前。count 带 mtime 缓存。"""
    root = archive_root() / member_dir
    if not root.is_dir():
        return []
    out = []
    for year_dir in sorted((d for d in root.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True):
        for month_dir in sorted((d for d in year_dir.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True):
            json_path = month_dir / "messages.json"
            if not json_path.is_file():
                continue
            out.append({"year": int(year_dir.name), "month": int(month_dir.name),
                        "count": _month_count(json_path)})
    return out


_ORIGINAL_LIST_MONTHS = list_months




def _jst_date(utc_str: str) -> str:
    """UTC 时间串 → JST 日期串 YYYY-MM-DD（解析失败返回空串）。"""
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""
    from datetime import timedelta
    return (dt + timedelta(hours=9)).strftime("%Y-%m-%d")


# 按天计数缓存：{json_path: (mtime, {"YYYY-MM-DD": {类型: count}})}
# 缓存按类型细分存储，聚合时再按筛选求和——同一份缓存服务任意类型过滤
_day_cache: dict[str, tuple[float, dict[str, dict[str, int]]]] = {}


def day_counts(member_dir: str, type_filter: set[str] | None = None) -> dict[str, int]:
    """全档按 JST 日期统计消息数（日历视图用），逐月 mtime 缓存。
    type_filter 为 None 统计全部类型，否则只计入指定类型集合。"""
    out: dict[str, int] = {}
    root = archive_root() / member_dir
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
           limit: int = 500) -> list[dict]:
    """跨月搜索：优先使用 SQLite FTS5 全文索引，无 DB 或被 mock 时降级为 JSON 遍历。
    原文与译文都参与匹配，空格分词取 AND 语义。
    返回附加 _year/_month 字段的消息列表，新的在前，最多 limit 条。"""
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        return []

    if list_months is _ORIGINAL_LIST_MONTHS:
        conn = init_db()
        if conn and _has_fts5:
            try:
                match_expr = " ".join(f'"{t}"' for t in terms)
                sql = """
                    SELECT m.raw_json, m.year, m.month
                    FROM messages_fts f
                    JOIN messages m ON f.id = m.id
                    WHERE f.member_dir = ? AND messages_fts MATCH ?
                """
                params: list[str | int] = [member_dir, match_expr]
                if type_filter:
                    placeholders = ",".join("?" * len(type_filter))
                    sql += f" AND m.type IN ({placeholders})"
                    params.extend(list(type_filter))
                sql += " ORDER BY m.updated_at DESC LIMIT ?"
                params.append(limit)

                cursor = conn.cursor()
                cursor.execute(sql, params)
                results = []
                for raw, yr, mo in cursor.fetchall():
                    msg = json.loads(raw)
                    results.append({**msg, "_year": yr, "_month": mo})
                if results:
                    return results
            except Exception as e:
                log_all(f"⚠️ SQLite FTS5 搜索异常，降级回 JSON 文件匹配: {e}", is_debug=True)


    results: list[dict] = []
    for m in list_months(member_dir):          # 已是新月份在前
        msgs = load_month(member_dir, m["year"], m["month"])
        for msg in reversed(msgs):             # 月内也按新→旧
            if type_filter and msg.get("type") not in type_filter:
                continue
            haystack = (
                (msg.get("text") or "") + "\n" +
                (msg.get("_translation") or "") + "\n" +
                (msg.get("_tags") or "") + "\n" +
                (msg.get("_custom_tags") or "")
            ).lower()
            if all(t in haystack for t in terms):
                results.append({**msg, "_year": m["year"], "_month": m["month"]})
                if len(results) >= limit:
                    return results
    return results



def load_archived_ids(m_name: str) -> tuple[set[str], set[str]]:
    """返回 (已归档 ID, 媒体下载失败的 ID)。回填工具用于跳过与重试。"""
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
            # 需重试的两种情况：明确标记失败；或媒体消息既无本地文件也无失败标记
            # （下载中途进程被杀会留下这种"幽灵"状态，不重试就永久缺媒体）
            incomplete = (
                m.get("_download_failed")
                or (m.get("type") in _MEDIA_TYPES and m.get("file") and not m.get("_local_file"))
            )
            (fail_ids if incomplete else ok_ids).add(mid)
    return ok_ids, fail_ids


def realign_archive_timezones() -> int:
    """全自动历史数据对齐：扫描全部已归档消息，按 JST (UTC+9) 真实年月纠正跨月错位数据。
    返回修正的消息条数。
    """
    root = archive_root()
    if not root.is_dir():
        return 0

    realigned_count = 0
    # 扫描所有 member_dir
    for member_path in sorted(root.iterdir()):
        if not member_path.is_dir():
            continue
        m_dir = member_path.name
        # 遍历所有 messages.json
        for json_file in list(member_path.glob("[0-9]*/[0-9]*/messages.json")):
            try:
                cur_month_str = json_file.parent.name
                cur_year_str = json_file.parent.parent.name
                cur_year = int(cur_year_str)
                cur_month = int(cur_month_str)
            except ValueError:
                continue

            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    msgs = json.load(f)
            except Exception:
                continue

            if not isinstance(msgs, list):
                continue

            stay_msgs = []
            moved_records = []  # [(target_year, target_month, msg), ...]

            for m in msgs:
                pub_str = m.get("published_at") or m.get("updated_at") or ""
                try:
                    dt = parse_jst_datetime(pub_str)
                except Exception:
                    stay_msgs.append(m)
                    continue

                if dt.year == cur_year and dt.month == cur_month:
                    stay_msgs.append(m)
                else:
                    moved_records.append((dt.year, dt.month, m))

            if not moved_records:
                continue

            # 1. 更新当前月的 messages.json
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(stay_msgs, f, ensure_ascii=False, indent=2)

            # 2. 移动错位消息及其关联的媒体文件到正确的目标月份
            for t_year, t_month, m in moved_records:
                target_month_dir = member_path / f"{t_year:04d}" / f"{t_month:02d}"
                target_month_dir.mkdir(parents=True, exist_ok=True)
                target_json = target_month_dir / "messages.json"

                # 处理本地媒体文件移动
                old_local = m.get("_local_file")
                if old_local:
                    old_path = member_path / old_local
                    if old_path.exists():
                        media_sub = _media_subdir(m.get("type", ""))
                        new_media_dir = target_month_dir / media_sub
                        new_media_dir.mkdir(parents=True, exist_ok=True)
                        new_file_path = new_media_dir / old_path.name
                        if not new_file_path.exists():
                            try:
                                import shutil
                                shutil.move(str(old_path), str(new_file_path))
                            except Exception:
                                pass
                        new_rel = new_file_path.relative_to(member_path).as_posix()
                        m["_local_file"] = new_rel

                # 追加/合并进目标月份的 messages.json
                target_msgs = []
                if target_json.exists():
                    try:
                        with open(target_json, "r", encoding="utf-8") as f:
                            target_msgs = json.load(f)
                    except Exception:
                        target_msgs = []
                by_id = {str(item.get("id", "")): item for item in target_msgs}
                by_id[str(m.get("id", ""))] = m
                final_target_msgs = sorted(by_id.values(), key=lambda x: x.get("updated_at", ""))
                with open(target_json, "w", encoding="utf-8") as f:
                    json.dump(final_target_msgs, f, ensure_ascii=False, indent=2)

                # 更新 SQLite 数据库中的 year 和 month
                _save_msgs_to_sqlite(m_dir, t_year, t_month, [m])
                realigned_count += 1

            # 3. 如果当前月已变为空，删除旧空目录
            if not stay_msgs:
                try:
                    json_file.unlink(missing_ok=True)
                    json_file.parent.rmdir()
                    # 如果年目录也空了，清理年目录
                    year_dir = member_path / cur_year_str
                    if year_dir.exists() and not any(year_dir.iterdir()):
                        year_dir.rmdir()
                except Exception:
                    pass

    # 清空 day_cache 缓存以保证最新
    _day_cache.clear()
    if realigned_count > 0:
        log_all(f"🕒 历史归档时区自动自愈完成：已纠正 {realigned_count} 条跨月时区错位消息至 JST 年月")
    return realigned_count
