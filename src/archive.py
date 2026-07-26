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
from datetime import datetime
from pathlib import Path

import httpx

import config.config as cfg
from src.logger import log_all

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


def initialize(client: httpx.AsyncClient) -> None:
    """注入共享 HTTP 客户端（媒体下载用）。"""
    global _media_client, _media_sem
    _media_client = client
    _media_sem = asyncio.Semaphore(3)


# ──────────────────────────────────────────────
# 路径工具
# ──────────────────────────────────────────────
def member_dir_name(m_name: str) -> str:
    return _FILENAME_ILLEGAL.sub("_", m_name.replace(" ", "_"))


def archive_root() -> Path:
    return Path(cfg.ARCHIVE_DIR)


def _member_root(m_name: str) -> Path:
    return archive_root() / member_dir_name(m_name)


def _month_dir(m_name: str, dt: datetime) -> Path:
    return _member_root(m_name) / f"{dt.year:04d}" / f"{dt.month:02d}"


def _parse_utc(utc_str: str) -> datetime:
    return datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")


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
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, json_path)


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
    """归档单条消息：先落 JSON，再下载媒体回填。幂等，可安全重复调用。"""
    m_name = member.get("m_name", "")
    utc_str = msg.get("updated_at", "")
    if not m_name or not utc_str:
        return
    try:
        dt = _parse_utc(utc_str)
    except ValueError:
        log_all(f"⚠️ 归档跳过（无法解析时间戳 {utc_str!r}）", is_debug=True)
        return

    record = dict(msg)
    if translated:
        record["_translation"] = translated
    try:
        await _merge_write(m_name, dt, record)
        if cfg.ARCHIVE_MEDIA and msg.get("file") and msg.get("type") in _MEDIA_TYPES:
            delta = await _download_media(m_name, dt, msg)
            await _merge_write(m_name, dt, delta)
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


def _jst_date(utc_str: str) -> str:
    """UTC 时间串 → JST 日期串 YYYY-MM-DD（解析失败返回空串）。"""
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""
    from datetime import timedelta
    return (dt + timedelta(hours=9)).strftime("%Y-%m-%d")


# 按天计数缓存：{json_path: (mtime, {"YYYY-MM-DD": count})}
_day_cache: dict[str, tuple[float, dict[str, int]]] = {}


def day_counts(member_dir: str) -> dict[str, int]:
    """全档按 JST 日期统计消息数（日历视图用），逐月 mtime 缓存。"""
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
                    month_counts[d] = month_counts.get(d, 0) + 1
            _day_cache[key] = (mtime, month_counts)
        for d, n in month_counts.items():
            out[d] = out.get(d, 0) + n
    return out


def search(member_dir: str, query: str, type_filter: set[str] | None = None,
           limit: int = 500) -> list[dict]:
    """跨月搜索：原文与译文都参与匹配，空格分词取 AND 语义。
    返回附加 _year/_month 字段的消息列表，新的在前，最多 limit 条。"""
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        return []
    results: list[dict] = []
    for m in list_months(member_dir):          # 已是新月份在前
        msgs = load_month(member_dir, m["year"], m["month"])
        for msg in reversed(msgs):             # 月内也按新→旧
            if type_filter and msg.get("type") not in type_filter:
                continue
            haystack = ((msg.get("text") or "") + "\n" + (msg.get("_translation") or "")).lower()
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
