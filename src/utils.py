# ============================================================
# utils.py — 公共工具：时间转换、速率限制
# ============================================================
import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone


def utc_to_jst(utc_str: str, fmt: str = "%m/%d %H:%M:%S") -> str:
    """将 UTC 时间字符串（%Y-%m-%dT%H:%M:%SZ）转换为 JST 格式化字符串。"""
    return (
        datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .astimezone(timezone(timedelta(hours=9)))
        .strftime(fmt)
    )


def in_hour_range(hour: int, start: int, end: int) -> bool:
    """判断小时是否在 [start, end) 区间内，正确处理跨午夜（如 22→6）。"""
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class RateLimiter:
    """基于 asyncio.Lock + 时间间隔的异步速率限制器。

    interval 可以是固定 float 或 Callable[[], float]，后者每次检查时实时读取，
    自然支持配置热重载（只需确保 Callable 读取的是模块级变量而非本地副本）。

    保证 lock 覆盖的区间内操作间隔 >= interval 秒，多协程调用时严格串行。
    """

    def __init__(self, interval: float | Callable[[], float]):
        if callable(interval):
            self._get_interval = interval
        else:
            self._get_interval = lambda: interval
        self._lock: asyncio.Lock = asyncio.Lock()
        self._last_ts: float = 0.0

    def update_interval(self, interval_seconds: float) -> None:
        """运行时替换为固定间隔（覆盖构造时传入的 getter）。"""
        self._get_interval = lambda: interval_seconds

    async def __aenter__(self):
        await self._lock.acquire()
        elapsed = time.monotonic() - self._last_ts
        need = self._get_interval() - elapsed
        if need > 0:
            await asyncio.sleep(need)
        return self

    async def __aexit__(self, *args):
        self._last_ts = time.monotonic()
        self._lock.release()
