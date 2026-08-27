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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_pub_desc ON messages(published_at DESC);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_type_pub ON messages(type, published_at DESC);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_member_type_pub ON messages(member_dir, type, published_at DESC);")

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS letters (
                id INTEGER PRIMARY KEY,
                group_id INTEGER,
                member_name TEXT NOT NULL,
                member_dir TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                text TEXT,
                file_url TEXT,
                local_file TEXT,
                thumbnail_url TEXT,
                is_favorite INTEGER DEFAULT 0,
                raw_json TEXT NOT NULL
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_letters_member ON letters(member_dir, created_at DESC);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_letters_created ON letters(created_at DESC);")

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


def sync_all_to_sqlite(force: bool = False) -> int:
    """自动扫描磁盘下所有历史归档 JSON，高性能单事务批量全量同步导入 SQLite 数据库。"""
    root = archive_root()
    if not root.is_dir():
        return 0

    conn = init_db()
    if not conn:
        return 0

    if not force:
        try:
            cur = conn.execute("SELECT COUNT(*) FROM messages;")
            row = cur.fetchone()
            if row and row[0] > 0:
                log_all(f"💾 SQLite 归档已就绪（共 {row[0]} 条记录）", is_debug=True)
                return row[0]
        except Exception:
            pass

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
# ──────────────────────────────────────────────
# 媒体下载与复用
# ──────────────────────────────────────────────
def find_media_bytes_by_url(m_name: str, file_url: str) -> bytes | None:
    """根据成员名与媒体 URL 查找本地已有归档文件，返回字节内容。用于多通道直接复用已下载媒体。"""
    if not m_name or not file_url:
        return None
    root = _member_root(m_name)
    if not root.exists():
        return None

    # 从 URL 提取 msg_id（例如 /172506-20260827-1122 -> 172506）
    m_match = re.search(r'/(\d+)[-_]', file_url)
    msg_id = m_match.group(1) if m_match else ""
    if not msg_id:
        id_match = re.search(r'/(\d{4,})', file_url)
        msg_id = id_match.group(1) if id_match else ""

    # 按 URL 中的日期定位年月目录（如 20260827 -> 2026/08）
    d_match = re.search(r'[-_](\d{4})(\d{2})\d{2}', file_url)
    target_dirs = []
    if d_match:
        year_str, month_str = d_match.group(1), d_match.group(2)
        m_dir = root / year_str / month_str
        if m_dir.exists():
            target_dirs.append(m_dir)
        m_dir2 = root / (year_str + month_str)
        if m_dir2.exists():
            target_dirs.append(m_dir2)

    if not target_dirs:
        try:
            for y_dir in root.iterdir():
                if y_dir.is_dir():
                    for m_sub in y_dir.iterdir():
                        if m_sub.is_dir():
                            target_dirs.append(m_sub)
                    target_dirs.append(y_dir)
        except OSError:
            return None

    for m_dir in target_dirs:
        for sub in ("images", "videos", "voice", "other"):
            sub_dir = m_dir / sub
            if not sub_dir.exists():
                continue
            try:
                for f in os.listdir(sub_dir):
                    if f.endswith(".tmp"):
                        continue
                    if msg_id and (f"_{msg_id}." in f or f"_{msg_id}_" in f or f.startswith(f"{msg_id}_") or f.endswith(f"_{msg_id}")):
                        p = sub_dir / f
                        if p.stat().st_size > 0:
                            return p.read_bytes()
            except OSError:
                pass
    return None


async def _download_media(m_name: str, dt: datetime, msg: dict, headers: dict[str, str] | None = None) -> dict:
    """下载消息媒体，返回带 _local_file / _download_failed 字段的增量记录。"""
    file_url = msg.get("file", "")
    thumb_url = msg.get("thumbnail", "")
    msg_id = str(msg.get("id", "0"))
    ts = dt.strftime("%Y%m%d_%H%M%S")
    dest_dir = _month_dir(m_name, dt) / _media_subdir(msg.get("type", ""))
    dest_dir.mkdir(parents=True, exist_ok=True)
    member_root = _member_root(m_name)

    # 已有本地文件（按 时间戳_id 前缀匹配，扩展名无关）→ 检查大小后直接复用
    for existing in os.listdir(dest_dir):
        if (existing.startswith(f"{ts}_{msg_id}") or f"_{msg_id}." in existing or f"_{msg_id}_" in existing) and not existing.endswith(".tmp"):
            f_path = dest_dir / existing
            try:
                if f_path.stat().st_size > 0:
                    rel = f_path.relative_to(member_root).as_posix()
                    return {"id": msg.get("id"), "updated_at": msg.get("updated_at"), "_local_file": rel}
                else:
                    f_path.unlink(missing_ok=True)
            except OSError:
                pass

    tmp_path = dest_dir / f"{ts}_{msg_id}.tmp"
    ok = False
    used_url = file_url
    assert _media_sem is not None, "archive.initialize() 未调用"

    # 自动解析账号凭据 headers（私有媒体资源需附带鉴权请求头）
    if not headers:
        try:
            from config.credentials import get_source_headers_for_account
            account_id = msg.get("account_id", "")
            group_type = msg.get("group_type", "")
            if not account_id or not group_type:
                from config.config import MONITOR_LIST
                for m in MONITOR_LIST:
                    if m.get("m_name") == m_name or m.get("name") == m_name:
                        account_id = account_id or m.get("account_id", "")
                        group_type = group_type or m.get("group_type", "")
                        break
            if account_id and group_type:
                headers = get_source_headers_for_account(account_id, group_type)
        except Exception:
            pass

    # 优先下载主媒体文件；主文件失效/404 时若为图片且有缩略图则回退
    candidate_urls = [u for u in [file_url, thumb_url] if u]
    if not candidate_urls:
        return {"id": msg.get("id"), "updated_at": msg.get("updated_at"), "_download_failed": True}

    async with _media_sem:
        client = _media_client
        for u in candidate_urls:
            if ok:
                break
            for attempt in range(3):
                try:
                    if client is not None and not getattr(client, "is_closed", False):
                        resp = await client.get(u, headers=headers or {}, timeout=60, follow_redirects=True)
                    else:
                        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as fallback_client:
                            resp = await fallback_client.get(u, headers=headers or {})

                    if resp.status_code == 200 and resp.content:
                        with open(tmp_path, "wb") as f:
                            f.write(resp.content)
                        if tmp_path.exists() and tmp_path.stat().st_size > 0:
                            ok = True
                            used_url = u
                            break
                        else:
                            log_all(f"⚠️ 归档媒体下载为空文件 (第 {attempt+1} 次尝试): {u[:80]}", is_debug=True)
                    elif resp.status_code in (403, 404, 410):
                        log_all(f"⚠️ 归档媒体下载 HTTP {resp.status_code} (资源已失效或无权访问): {u[:80]}", is_debug=True)
                        break
                    else:
                        log_all(f"⚠️ 归档媒体下载 HTTP {resp.status_code} (第 {attempt+1} 次重试): {u[:80]}", is_debug=True)
                except Exception as e:
                    log_all(f"⚠️ 归档媒体下载异常 (第 {attempt+1} 次重试): {u[:80]} — {type(e).__name__}: {e}", is_debug=True)
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))

    if not ok:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"id": msg.get("id"), "updated_at": msg.get("updated_at"), "_download_failed": True}

    ext = _guess_extension(used_url, _sniff_content_type(tmp_path))
    final_path = dest_dir / f"{ts}_{msg_id}{ext}"
    os.replace(tmp_path, final_path)
    rel = final_path.relative_to(member_root).as_posix()
    return {"id": msg.get("id"), "updated_at": msg.get("updated_at"), "_local_file": rel}


_FN_TIMESTAMP_RE = re.compile(r'(\d{8})[-_](\d{6})')


def extract_upload_time(msg: dict) -> str | None:
    """从消息的媒体文件 URL 中提取成员真实上传时间戳 (ISO 8601 UTC 字符串)。"""
    file_url = msg.get("file") or msg.get("thumbnail") or ""
    if not file_url and msg.get("raw_json"):
        try:
            raw = json.loads(msg["raw_json"]) if isinstance(msg["raw_json"], str) else msg["raw_json"]
            file_url = raw.get("file") or raw.get("thumbnail") or ""
        except Exception:
            pass
    if not file_url:
        return None
    m = _FN_TIMESTAMP_RE.search(file_url)
    if not m:
        return None
    date_str, time_str = m.group(1), m.group(2)
    try:
        from datetime import timezone
        dt_utc = datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return dt_utc.isoformat()
    except Exception:
        return None


def infer_member_group(name: str) -> str:
    """根据成员姓名或配置推断所属坂道：'nogizaka' | 'sakurazaka' | 'hinatazaka' | ''。"""
    import config.config as cfg
    norm = name.replace(" ", "").replace("　", "").replace("_", "")
    for m in getattr(cfg, "MONITOR_LIST", []):
        m_norm = m.get("m_name", "").replace(" ", "").replace("　", "").replace("_", "")
        if m_norm == norm:
            return m.get("group_type", "")

    hinata_names = [
        "金村", "大野", "佐藤", "片山", "坂井", "下田", "山下葉", "大田", "正源司", "藤嶌", "渡辺", "小坂",
        "加藤", "齐藤", "佐佐木", "東村", "松田好", "河田", "丹生", "濱岸", "富田", "高本", "高瀬",
        "上村ひ", "高橋", "森本", "山口", "平尾", "平冈", "竹内", "岸", "小西", "清水", "宮地", "石塚", "松尾桜", "マネダコ"
    ]
    sakura_names = [
        "石森", "小池", "小林", "田村保", "森田", "藤吉", "山崎", "谷口", "中川", "山田", "浅井", "的野",
        "上村莉", "齋藤冬", "菅井", "土生", "守屋", "渡邉理", "渡辺梨", "井上梨", "遠藤光", "大園", "大沼",
        "幸阪", "武元", "増本", "松田里", "村井", "村山", "山下瞳", "小島", "向井"
    ]
    nogi_names = [
        "冨里", "賀喜", "一ノ瀬", "井上和", "川崎", "五百城", "中西", "池田", "奥田", "菅原", "小川",
        "秋元", "生田", "生驹", "伊藤", "岩本", "梅澤", "遠藤さ", "久保", "齋藤飛", "阪口", "佐藤楓",
        "柴田", "白石", "新内", "鈴木", "高山", "田村真", "筒井", "西野", "橋本", "樋口", "星野",
        "松村", "向井葉", "山下美", "弓木", "与田", "川端", "小津", "松尾美"
    ]
    for k in hinata_names:
        if k in norm:
            return "hinatazaka"
    for k in sakura_names:
        if k in norm:
            return "sakurazaka"
    for k in nogi_names:
        if k in norm:
            return "nogizaka"
    return ""


# ──────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────
async def archive_message(member: dict, msg: dict, translated: str = "") -> None:
    """归档单条消息：按 JST 日本标准时间划分年月，先落 JSON，再下载媒体回填。幂等，可安全重复调用。"""
    m_name = member.get("m_name", "") or member.get("name", "")
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
            from config.credentials import get_source_headers_for_account
            headers = get_source_headers_for_account(
                member.get("account_id", "") or msg.get("account_id", ""),
                member.get("group_type", "") or msg.get("group_type", "")
            )
            delta = await _download_media(m_name, dt, msg, headers=headers)
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
# 粉丝信件 (Fan Letters) 归档与查询
# ──────────────────────────────────────────────
def _member_letters_dir(m_name: str) -> Path:
    d = _member_root(m_name) / "letters"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def archive_single_letter(member_name: str, letter: dict, headers: dict | None = None) -> dict:
    """归档单封粉丝信件：下载高清信纸卡片、持久化至 SQLite letters 表和本地文件。"""
    letter_id = letter.get("id")
    if not letter_id or not member_name:
        return letter

    m_dir = member_dir_name(member_name)
    raw_json = json.dumps(letter, ensure_ascii=False)
    created_at = letter.get("created_at") or ""
    updated_at = letter.get("updated_at") or created_at
    text = letter.get("text") or ""
    file_url = letter.get("file") or ""
    thumbnail_url = letter.get("thumbnail") or ""
    is_favorite = 1 if letter.get("is_favorite") else 0
    group_id = letter.get("group_id") or letter.get("member_id") or 0

    # 尝试解析时间并下载信纸原图卡片
    local_file = ""
    if file_url:
        try:
            dt = parse_jst_datetime(created_at) if created_at else datetime.now(JST_TZ)
            time_prefix = dt.strftime("%Y%m%d_%H%M%S")
            letters_dir = _member_letters_dir(member_name)
            target_path = letters_dir / f"{time_prefix}_{letter_id}.jpg"
            
            if target_path.exists() and target_path.stat().st_size > 0:
                local_file = str(target_path.relative_to(archive_root())).replace("\\", "/")
            else:
                client = _media_client
                async with _media_sem:
                    try:
                        if client is not None and not client.is_closed:
                            resp = await client.get(file_url, headers=headers, timeout=30.0)
                        else:
                            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as temp_c:
                                resp = await temp_c.get(file_url, headers=headers)
                        if resp.status_code == 200 and len(resp.content) > 0:
                            target_path.write_bytes(resp.content)
                            local_file = str(target_path.relative_to(archive_root())).replace("\\", "/")
                            log_all(f"✉️ [信件归档] 成功保存信件卡片: {m_dir}/letters/{target_path.name} ({len(resp.content)/1024:.1f} KB)", is_debug=True)
                    except Exception as ex:
                        log_all(f"⚠️ [信件归档] 下载信件卡片失败 (ID: {letter_id}): {ex}", is_debug=True)
        except Exception as ex:
            log_all(f"⚠️ [信件归档] 处理信件媒体异常 (ID: {letter_id}): {ex}", is_debug=True)

    # 写入/更新 SQLite
    conn = init_db()
    if conn:
        try:
            with conn:
                conn.execute("""
                    INSERT INTO letters (id, group_id, member_name, member_dir, created_at, updated_at, text, file_url, local_file, thumbnail_url, is_favorite, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        group_id=excluded.group_id,
                        updated_at=excluded.updated_at,
                        text=excluded.text,
                        file_url=excluded.file_url,
                        local_file=COALESCE(NULLIF(excluded.local_file, ''), letters.local_file),
                        thumbnail_url=excluded.thumbnail_url,
                        is_favorite=excluded.is_favorite,
                        raw_json=excluded.raw_json;
                """, (letter_id, group_id, member_name, m_dir, created_at, updated_at, text, file_url, local_file, thumbnail_url, is_favorite, raw_json))
        except Exception as ex:
            log_all(f"⚠️ 保存信件到 SQLite 失败 (ID: {letter_id}): {ex}", is_debug=True)

    res = dict(letter)
    if local_file:
        res["local_file"] = local_file
    return res


async def archive_letters_batch(member_name: str, letters: list[dict], headers: dict | None = None) -> list[dict]:
    """批量归档信件列表，并更新本地 letters.json 汇总。"""
    if not member_name or not letters:
        return []
    results = []
    for l_item in letters:
        r = await archive_single_letter(member_name, l_item, headers=headers)
        results.append(r)

    # 同步写入/更新本地 letters/letters.json
    try:
        letters_dir = _member_letters_dir(member_name)
        json_path = letters_dir / "letters.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    except Exception as ex:
        log_all(f"⚠️ 保存 letters.json 失败 ({member_name}): {ex}", is_debug=True)

    return results


def get_existing_letter_ids(member_dir: str) -> set[int]:
    """获取指定成员已归档的所有信件 ID 集合。"""
    conn = init_db()
    if conn:
        try:
            rows = conn.execute("SELECT id FROM letters WHERE member_dir = ?;", (member_dir,)).fetchall()
            return {int(r[0]) for r in rows}
        except Exception:
            pass
    letters = get_archive_letters(member_dir)
    return {int(item.get("id")) for item in letters if item.get("id")}


def get_archive_letters(member_dir: str) -> list[dict]:
    """从 SQLite 获取指定成员已归档的所有粉丝信件（新在前）。"""
    conn = init_db()
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
    json_path = archive_root() / member_dir / "letters" / "letters.json"
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def get_letters_count(member_dir: str) -> int:
    """获取指定成员已归档的信件数量。"""
    conn = init_db()
    if conn:
        try:
            row = conn.execute("SELECT COUNT(*) FROM letters WHERE member_dir = ?;", (member_dir,)).fetchone()
            if row:
                return row[0]
        except Exception:
            pass
    letters = get_archive_letters(member_dir)
    return len(letters)


# ──────────────────────────────────────────────
# 查询（网页查看器 / 回填工具用）
# ──────────────────────────────────────────────
def list_members() -> list[str]:
    """返回所有有归档消息的成员列表。优先从 SQLite 获取。"""
    conn = init_db()
    if conn:
        try:
            rows = conn.execute("SELECT DISTINCT member_dir FROM messages ORDER BY member_dir ASC;").fetchall()
            if rows:
                return [r[0] for r in rows]
        except Exception:
            pass
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
    """返回 [{year, month, count}]，新的在前。支持 mtime 缓存与快速统计。"""
    root = archive_root() / member_dir
    if root.is_dir():
        out = []
        for year_dir in sorted((d for d in root.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True):
            for month_dir in sorted((d for d in year_dir.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True):
                json_path = month_dir / "messages.json"
                if not json_path.is_file():
                    continue
                out.append({"year": int(year_dir.name), "month": int(month_dir.name),
                            "count": _month_count(json_path)})
        if out:
            return out

    conn = init_db()
    if conn:
        try:
            rows = conn.execute("""
                SELECT year, month, COUNT(*) FROM messages
                WHERE member_dir = ?
                GROUP BY year, month
                ORDER BY year DESC, month DESC;
            """, (member_dir,)).fetchall()
            if rows:
                return [{"year": r[0], "month": r[1], "count": r[2]} for r in rows]
        except Exception:
            pass
    return []


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


def realign_archive_timezones(force: bool = False) -> int:
    """全自动历史数据对齐：扫描全部已归档消息，按 JST (UTC+9) 真实年月纠正跨月错位数据。
    返回修正的消息条数。
    """
    root = archive_root()
    if not root.is_dir():
        return 0

    marker_file = root / ".timezone_realigned"
    if not force and marker_file.exists():
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
    try:
        marker_file.touch(exist_ok=True)
    except Exception:
        pass
    if realigned_count > 0:
        log_all(f"🕒 历史归档时区自动自愈完成：已纠正 {realigned_count} 条跨月时区错位消息至 JST 年月")
    return realigned_count
