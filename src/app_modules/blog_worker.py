"""官方博客独立调度任务。

博客抓取曾经嵌在 Message 主循环中，导致 ``blog_monitor`` 中的间隔只是
页面上的“假配置”。本模块只负责博客任务的生命周期、时段门闩和退避；
具体抓取、翻译、归档仍由 :mod:`src.blog_fetcher` 负责，推送继续走统一
``send_blog_post`` 入口。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping
from typing import Any, Awaitable

from src import blog_fetcher, health
from src.app_modules.daily_summary import _get_jst_now
from src.logger import log_all
from src.monitor_schedule import MonitorSchedule


def _as_interval(value: Any, default: tuple[int, int]) -> tuple[int, int]:
    """把博客间隔配置归一化为正整数区间。"""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            lo, hi = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return default
        lo, hi = sorted((lo, hi))
        if lo > 0:
            return max(1, lo), max(1, hi)
    return default


def _positive_int(value: Any, default: int) -> int:
    """读取正整数；博客退避配置缺失或非法时使用安全默认值。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class BlogWorker:
    """单独运行官方博客轮询的 asyncio worker。

    ``config_getter`` 每轮调用一次，因而 config watcher 原地更新配置后，
    下一轮即可看到新的开关、频率和全局时段。``cycle_runner`` 可注入以便
    测试，不改变生产上的 ``blog_fetcher.run_blog_cycle`` 契约。
    """

    def __init__(
        self,
        client,
        db,
        config_getter: Callable[[], Mapping[str, Any]],
        stop_event: asyncio.Event,
        *,
        cycle_runner: Callable[..., Awaitable[list[dict]]] | None = None,
        send_post: Callable[[dict], Awaitable[bool]] | None = None,
        idle_sleep_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._db = db
        self._config_getter = config_getter
        self._stop_event = stop_event
        self._cycle_runner = cycle_runner or blog_fetcher.run_blog_cycle
        self._send_post = send_post
        self._idle_sleep_seconds = max(1.0, float(idle_sleep_seconds))
        self._cycle_lock = asyncio.Lock()
        self._sleep_logged = False
        self._failure_count = 0

    @property
    def config(self) -> Mapping[str, Any]:
        try:
            value = self._config_getter()
        except Exception:  # nosec B110 - 配置异常时按未启用处理
            return {}
        return value if isinstance(value, Mapping) else {}

    def _blog_config(self, config: Mapping[str, Any]) -> Mapping[str, Any]:
        value = config.get("blog_monitor")
        return value if isinstance(value, Mapping) else {}

    def _interval(self, config: Mapping[str, Any]) -> tuple[int, str]:
        schedule = MonitorSchedule.from_config(config)
        blog_cfg = self._blog_config(config)
        if schedule.interval_phase(_get_jst_now()) == "night":
            default = (1650, 1950)
            raw = blog_cfg.get("night_interval", default)
            label = "🌙 博客深夜低速"
        else:
            default = (60, 120)
            raw = blog_cfg.get("day_interval", default)
            label = "☀️ 博客日间巡查"
        lo, hi = _as_interval(raw, default)
        return random.randint(lo, hi), label  # nosec B311

    async def _wait(self, seconds: float) -> bool:
        """等待指定时间，返回 True 表示收到停止信号。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.1, seconds))
        except asyncio.TimeoutError:
            return False
        return True

    async def _wait_for_schedule(self, schedule: MonitorSchedule) -> bool:
        """休眠期间分段等待，便于热重载边界且不拖慢优雅停止。"""
        while not self._stop_event.is_set():
            current = MonitorSchedule.from_config(self.config)
            remaining = current.seconds_until_wake(_get_jst_now())
            if remaining <= 0:
                return False
            if await self._wait(min(30, max(1, remaining))):
                return True
            # 配置可能在等待期间关闭休眠，下一轮立即恢复。
            if not MonitorSchedule.from_config(self.config).is_sleeping(_get_jst_now()):
                return False
        return True

    async def _run_cycle(self, config: Mapping[str, Any]) -> None:
        """运行一次抓取、归档和博客推送；锁住 worker 内的 DB 边界。"""
        async with self._cycle_lock:
            posts = await self._cycle_runner(self._client, self._db, dict(config))
        if not posts:
            health.get_tracker().record_member_fetch("博客 (全局)", True)
            return

        sender = self._send_post
        if sender is None:
            from src.notifier import send_blog_post
            sender = send_blog_post
        log_all(f"📝 博客更新：{len(posts)} 篇")
        all_ok = True
        for post in posts:
            try:
                ok = await sender(post)
                if ok:
                    log_all(f"✅ 博客 [{post.get('title', '无题')}] 推送完成")
                else:
                    all_ok = False
                    log_all(
                        f"⚠️ 博客 [{post.get('title', '无题')}] 推送失败（无可用通道）",
                        is_error=True,
                    )
                    health.get_tracker().record_member_push("博客 (全局)", False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # nosec B110 - 单篇失败不丢后续博客
                all_ok = False
                log_all(
                    f"💥 博客推送异常 | phase=blog | error={type(exc).__name__}: {exc}",
                    is_error=True,
                )
                health.get_tracker().record_member_push("博客 (全局)", False)
            await asyncio.sleep(0.5)
        health.get_tracker().record_member_push("博客 (全局)", all_ok)

    async def run(self) -> None:
        """长驻博客调度循环。"""
        while not self._stop_event.is_set():
            config = self.config
            blog_cfg = self._blog_config(config)
            enabled = bool(blog_cfg.get("enabled", False)) and config.get("blog_records") is not None
            if not enabled:
                self._sleep_logged = False
                if await self._wait(self._idle_sleep_seconds):
                    return
                continue

            schedule = MonitorSchedule.from_config(config)
            if schedule.is_sleeping(_get_jst_now()):
                if not self._sleep_logged:
                    log_all(
                        f"😴 博客内容监控进入全局休眠 | {schedule.sleep_start_hour:02d}:00–"
                        f"{schedule.sleep_end_hour:02d}:00",
                        is_debug=True,
                    )
                    self._sleep_logged = True
                if await self._wait_for_schedule(schedule):
                    return
                continue

            if self._sleep_logged:
                log_all("☀️ 博客内容监控已结束休眠，恢复独立轮询", is_debug=True)
                self._sleep_logged = False

            try:
                await self._run_cycle(config)
                self._failure_count = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # nosec B110 - 博客异常只影响下一轮
                self._failure_count += 1
                health.get_tracker().record_member_fetch(
                    "博客 (全局)", False, health.ErrorTier.TRANSIENT, str(exc)
                )
                log_all(
                    f"⚠️ 博客巡查异常 | phase=blog | error={type(exc).__name__}: {exc}",
                    is_error=True,
                )

            if self._failure_count:
                blog_cfg = self._blog_config(config)
                base = _positive_int(blog_cfg.get("error_backoff_seconds"), 60)
                cap = max(base, _positive_int(blog_cfg.get("error_backoff_max_seconds"), 900))
                wait_for = min(cap, base * (2 ** min(self._failure_count - 1, 5)))
                label = "⏳ 博客异常退避"
            else:
                wait_for, label = self._interval(config)
            log_all(f"{label} | 下次博客巡查: {wait_for}s 后", is_debug=True)
            if await self._wait(wait_for):
                return


async def run_blog_loop(
    client,
    db,
    config_getter: Callable[[], Mapping[str, Any]],
    stop_event: asyncio.Event,
    **kwargs: Any,
) -> None:
    """函数式兼容入口，供 app 生命周期和测试调用。"""
    await BlogWorker(client, db, config_getter, stop_event, **kwargs).run()


__all__ = ["BlogWorker", "run_blog_loop"]
