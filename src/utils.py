# ============================================================
# utils.py — 公共工具：时间转换、速率限制
# ============================================================
import asyncio
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from src.logger import log_all

_JST = timezone(timedelta(hours=9))


def _parse_utc(raw: str) -> datetime | None:
    """解析 API 返回的时间字符串，失败返回 None（不抛异常）。
    优先 ISO 8601（兼容小数秒和 ±HH:MM 偏移），回退到 '%Y-%m-%dT%H:%M:%SZ'。"""
    s = (raw or "").strip()
    if not s:
        return None
    # Python 3.10 的 fromisoformat 不认结尾的 Z，先换成显式偏移
    iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def utc_to_jst(utc_str: str, fmt: str = "%m/%d %H:%M:%S") -> str:
    """将 UTC 时间字符串转换为 JST 格式化字符串。
    无法解析时原样返回入参 —— 单条畸形时间戳不应中断整轮推送。"""
    dt = _parse_utc(utc_str)
    if dt is None:
        log_all(f"⚠️ 无法解析时间戳 {utc_str!r}，原样输出", is_debug=True)
        return utc_str
    return dt.astimezone(_JST).strftime(fmt)


def in_hour_range(hour: int, start: int, end: int) -> bool:
    """判断小时是否在 [start, end) 区间内，正确处理跨午夜（如 22→6）。"""
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def match_member_filter(
    member_name: str,
    filters: list[str] | None,
    member_id: str | int | None = None,
) -> bool:
    """检查成员名或 ID 是否命中过滤列表。

    支持：
    1. 空列表或 None 表示不过滤（放行全部）
    2. 精确匹配（如 '冨里 奈央' in filters）
    3. 忽略全角/半角空格和下划线的模糊匹配（如 '冨里奈央' 匹配 '冨里 奈央'）
    4. 成员 ID 匹配（如 '55' in filters）
    """
    if not filters:
        return True
    if not member_name and member_id is None:
        return False
    if member_name in filters:
        return True
    if member_id is not None and str(member_id) in [str(f).strip() for f in filters]:
        return True

    # 归一化（去除所有空格、全角空格、下划线并转小写）
    norm_name = str(member_name or "").replace(" ", "").replace("　", "").replace("_", "").lower()
    norm_filters = {str(f).replace(" ", "").replace("　", "").replace("_", "").lower() for f in filters}
    return norm_name in norm_filters


class RateLimiter:
    """基于时间戳与线程安全锁的异步速率限制器（支持多线程与不同 event loop）。

    interval 可以是固定 float 或 Callable[[], float]，后者每次检查时实时读取，
    自然支持配置热重载（只需确保 Callable 读取的是模块级变量而非本地副本）。

    保证进入区间的操作间隔 >= interval 秒，多协程/多线程并发调用时严格串行。
    """

    def __init__(self, interval: float | Callable[[], float]):
        if callable(interval):
            self._get_interval = interval
        else:
            self._get_interval = lambda: interval
        self._thread_lock = threading.Lock()
        self._next_allowed_ts: float = 0.0

    async def __aenter__(self):
        with self._thread_lock:
            now = time.monotonic()
            interval = float(self._get_interval())
            # 计算当前请求获准执行的时间点
            scheduled_ts = max(now, self._next_allowed_ts)
            self._next_allowed_ts = scheduled_ts + interval
            wait_time = scheduled_ts - now

        if wait_time > 0:
            await asyncio.sleep(wait_time)
        return self

    async def __aexit__(self, *args):
        pass


def format_bytes(num_bytes: int | float) -> str:
    """将字节数格式化为人类可读的字符串（B / KB / MB / GB / TB）。"""
    n = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


_storage_cache: dict = {}
_storage_cache_time: float = 0.0
_storage_lock = threading.Lock()


def get_storage_breakdown(force_refresh: bool = False) -> dict:
    """计算磁盘总空间及 Message、博客、社媒、数据库与日志的分项存储占用（带 30s 内存缓存）。"""
    global _storage_cache, _storage_cache_time
    import shutil
    from pathlib import Path

    now = time.time()
    with _storage_lock:
        if not force_refresh and _storage_cache and (now - _storage_cache_time < 30):
            return dict(_storage_cache)

        base_dir = Path(__file__).resolve().parent.parent

        # 1. 宿主机 / 容器文件系统磁盘用量
        try:
            total_b, used_b, free_b = shutil.disk_usage(str(base_dir))
            used_pct = round((used_b / total_b * 100), 1) if total_b > 0 else 0.0
        except Exception:
            total_b, used_b, free_b, used_pct = 0, 0, 0, 0.0

        def _scan_dir(dir_path: Path, ext_filter=None, ext_exclude=None) -> tuple[int, int]:
            """返回 (总字节数, 文件总数)"""
            if not dir_path.exists() or not dir_path.is_dir():
                return 0, 0
            size = 0
            count = 0
            try:
                for entry in dir_path.rglob("*"):
                    if entry.is_file():
                        if ext_filter and entry.suffix.lower() not in ext_filter:
                            continue
                        if ext_exclude and entry.suffix.lower() in ext_exclude:
                            continue
                        try:
                            size += entry.stat().st_size
                            count += 1
                        except OSError:
                            pass
            except Exception:
                pass
            return size, count

        # 2. 分项统计
        # Message 媒体（data/archive 中排除 .db 数据库文件）
        msg_media_b, msg_media_cnt = _scan_dir(base_dir / "data" / "archive", ext_exclude=(".db", ".db-wal", ".db-shm"))
        # 博客原图 (data/blog_images)
        blog_img_b, blog_img_cnt = _scan_dir(base_dir / "data" / "blog_images")
        # 社媒下载媒体 (data/social_media)
        social_b, social_cnt = _scan_dir(base_dir / "data" / "social_media")
        # 直播录制 (recordings / data/recordings)
        live_b1, live_cnt1 = _scan_dir(base_dir / "recordings")
        live_b2, live_cnt2 = _scan_dir(base_dir / "data" / "recordings")
        live_b = live_b1 + live_b2
        live_cnt = live_cnt1 + live_cnt2

        # 核心数据库文件 (data/*.db, data/archive/*.db, etc.)
        db_bytes = 0
        db_count = 0
        for p in (base_dir / "data").rglob("*.db*"):
            if p.is_file():
                try:
                    db_bytes += p.stat().st_size
                    db_count += 1
                except OSError:
                    pass

        # 系统运行日志 (logs/)
        logs_b, logs_cnt = _scan_dir(base_dir / "logs")

        app_total_b = msg_media_b + blog_img_b + social_b + live_b + db_bytes + logs_b

        res = {
            "disk": {
                "total_bytes": total_b,
                "total_human": format_bytes(total_b),
                "used_bytes": used_b,
                "used_human": format_bytes(used_b),
                "free_bytes": free_b,
                "free_human": format_bytes(free_b),
                "used_percent": used_pct,
            },
            "app_total": {
                "bytes": app_total_b,
                "human": format_bytes(app_total_b),
            },
            "categories": {
                "message_media": {
                    "name": "Message 媒体",
                    "bytes": msg_media_b,
                    "human": format_bytes(msg_media_b),
                    "count": msg_media_cnt,
                    "color": "#ec4899",
                },
                "blog_images": {
                    "name": "博客原图",
                    "bytes": blog_img_b,
                    "human": format_bytes(blog_img_b),
                    "count": blog_img_cnt,
                    "color": "#8b5cf6",
                },
                "social_media": {
                    "name": "社媒媒体",
                    "bytes": social_b,
                    "human": format_bytes(social_b),
                    "count": social_cnt,
                    "color": "#3b82f6",
                },
                "live_recordings": {
                    "name": "直播录像",
                    "bytes": live_b,
                    "human": format_bytes(live_b),
                    "count": live_cnt,
                    "color": "#f59e0b",
                },
                "databases": {
                    "name": "SQLite 数据库",
                    "bytes": db_bytes,
                    "human": format_bytes(db_bytes),
                    "count": db_count,
                    "color": "#10b981",
                },
                "logs": {
                    "name": "运行日志",
                    "bytes": logs_b,
                    "human": format_bytes(logs_b),
                    "count": logs_cnt,
                    "color": "#6b7280",
                },
            },
            "updated_at": now,
        }

        _storage_cache = res
        _storage_cache_time = now
        return dict(res)


def clean_storage_category(category: str) -> tuple[bool, str, int]:
    """清理指定分类的存储媒体/缓存，返回 (成功, 描述信息, 清理释放的字节数)。"""
    import shutil
    from pathlib import Path

    base_dir = Path(__file__).resolve().parent.parent
    freed_bytes = 0
    deleted_files = 0

    def _remove_files_in_dir(d: Path) -> tuple[int, int]:
        nonlocal freed_bytes, deleted_files
        b_count, f_count = 0, 0
        if not d.exists() or not d.is_dir():
            return 0, 0
        for item in list(d.iterdir()):
            try:
                if item.is_file():
                    sz = item.stat().st_size
                    item.unlink()
                    b_count += sz
                    f_count += 1
                elif item.is_dir():
                    for sub in item.rglob("*"):
                        if sub.is_file():
                            try:
                                b_count += sub.stat().st_size
                                f_count += 1
                            except OSError:
                                pass
                    shutil.rmtree(item, ignore_errors=True)
            except Exception:
                pass
        freed_bytes += b_count
        deleted_files += f_count
        return b_count, f_count

    if category == "live_recordings":
        _remove_files_in_dir(base_dir / "recordings")
        _remove_files_in_dir(base_dir / "data" / "recordings")
        get_storage_breakdown(force_refresh=True)
        return True, f"已清理直播录像（共删除 {deleted_files} 个文件，释放 {format_bytes(freed_bytes)}）", freed_bytes

    elif category == "social_media":
        _remove_files_in_dir(base_dir / "data" / "social_media")
        get_storage_breakdown(force_refresh=True)
        return True, f"已清理社交媒体下载缓存（共删除 {deleted_files} 个文件，释放 {format_bytes(freed_bytes)}）", freed_bytes

    elif category == "logs":
        log_dir = base_dir / "logs"
        if log_dir.exists() and log_dir.is_dir():
            for f in log_dir.glob("*.log*"):
                if f.is_file():
                    try:
                        sz = f.stat().st_size
                        with open(f, "w", encoding="utf-8"):
                            pass
                        freed_bytes += sz
                        deleted_files += 1
                    except Exception:
                        pass
        get_storage_breakdown(force_refresh=True)
        return True, f"已截断清空运行日志（释放 {format_bytes(freed_bytes)}）", freed_bytes

    else:
        return False, f"不支持清理该分类: {category}", 0
