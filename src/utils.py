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
