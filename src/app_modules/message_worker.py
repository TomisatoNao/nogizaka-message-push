"""
src/app_modules/message_worker.py — 成员消息巡查、退避调度与多成员并发流水线
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import random
import sys
import time
import traceback

import httpx

import config.config as cfg
from config.credentials import proactive_refresh_if_expiring
from src import blog_fetcher, fetcher, health
from src.app_modules.daily_summary import _get_jst_now
from src.app_modules.process_lock import _stop_requested
from src.logger import log_all
from src.utils import in_hour_range


@dataclass(frozen=True)
class _MemberCycleResult:
    """单个成员在本轮消息巡查中的结果，用于生成低噪声汇总日志。"""

    name: str
    fetch_ok: bool = False
    skipped: bool = False
    new_count: int = 0
    push_ok: bool | None = None

    @property
    def failed(self) -> bool:
        """是否需要在本轮汇总中作为异常成员突出显示。"""
        return not self.skipped and (not self.fetch_ok or self.push_ok is False)


def _message_cycle_summary(results: list[_MemberCycleResult], elapsed: float) -> tuple[str, bool]:
    """生成消息巡查摘要，返回 ``(文本, 是否有异常)``。"""
    total = len(results)
    fetch_ok = sum(result.fetch_ok for result in results)
    skipped = sum(result.skipped for result in results)
    new_count = sum(result.new_count for result in results)
    processed_count = sum(
        result.new_count for result in results if result.push_ok is True
    )
    error_members = [result.name for result in results if result.failed]
    has_errors = bool(error_members)

    summary = (
        f"🔍 消息巡查完毕 | 成员 {total} | 请求成功 {fetch_ok} | "
        f"新增 {new_count} | 处理完成 {processed_count} | "
        f"异常 {len(error_members)} | 跳过 {skipped} | 耗时 {elapsed:.1f}s"
    )
    if has_errors:
        summary += f" | 异常成员: {' · '.join(error_members)}"
    return summary, has_errors


def _calc_sleep_seconds() -> int:
    """若当前在休眠时段内，返回距离休眠结束的秒数；否则返回 0。
    支持跨午夜休眠窗口（如 SLEEP_START=22, SLEEP_END=6）。"""
    jst = _get_jst_now()
    if in_hour_range(jst.hour, cfg.SLEEP_START_HOUR, cfg.SLEEP_END_HOUR):
        wake = jst.replace(hour=cfg.SLEEP_END_HOUR, minute=0, second=0, microsecond=0)
        if wake <= jst:
            wake += timedelta(days=1)
        return int((wake - jst).total_seconds())
    return 0


def _next_interval() -> tuple[int, str]:
    jst = _get_jst_now()
    if in_hour_range(jst.hour, cfg.NIGHT_START_HOUR, cfg.DAY_START_HOUR):
        base = random.randint(*cfg.NIGHT_INTERVAL)  # nosec B311
        tag = "🌙 深夜低速"
    else:
        base = random.randint(*cfg.DAY_INTERVAL)  # nosec B311
        tag = "☀️ 日间巡查"
    # ±10% 抖动，最低不低于 1s
    jitter = int(base * random.uniform(-0.1, 0.1))  # nosec B311
    return max(1, base + jitter), tag


async def _wait_or_trigger(event: asyncio.Event, timeout: float) -> bool:
    """等待 timeout 秒；期间事件被置位（网页「立即巡查」）则提前返回 True。"""
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        event.clear()
        return True
    except asyncio.TimeoutError:
        return False


def _is_message_monitor_enabled() -> bool:
    app_mod = sys.modules.get("src.app")
    fn = getattr(app_mod, "_message_monitor_enabled", None)
    if fn is not None:
        return fn()
    return bool(getattr(cfg, "MESSAGE_MONITOR_ENABLED", False))


def _alert_group_for_account(acc_id: str) -> int:
    app_mod = sys.modules.get("src.app")
    fn = getattr(app_mod, "_alert_group_for_account", None)
    if fn is not None:
        return fn(acc_id)
    for m in getattr(cfg, "MONITOR_LIST", []):
        if m.get("account_id") == acc_id and m.get("target_groups"):
            return m["target_groups"][0]
    return 0


async def _run_cycle() -> None:
    """单轮巡查：主动续期 → 并发抓取 → 串行推送。"""
    app_mod = sys.modules.get("src.app")
    cycle_summary_fn = getattr(app_mod, "_message_cycle_summary", _message_cycle_summary) if app_mod else _message_cycle_summary

    # ── Phase 1: Message 消息巡查 ──
    if _is_message_monitor_enabled():
        message_cycle_started = time.monotonic()
        valid_monitors = [m for m in cfg.MONITOR_LIST if m.get("account_id") and m.get("m_id")]

        # 每个账号取一个 target_group 作为报警目标
        account_target_groups: dict[str, int] = {
            m["account_id"]: _alert_group_for_account(m["account_id"])
            for m in valid_monitors
            if m.get("account_id")
        }

        # ── 改进 1：每轮巡查前主动检查并刷新即将过期的 Token ──
        if account_target_groups:
            await asyncio.gather(*[
                proactive_refresh_if_expiring(acc_id, grp)
                for acc_id, grp in account_target_groups.items()
            ])

        # ── 改进 4：随机打乱成员轮询顺序 ──
        shuffled = list(valid_monitors)
        random.shuffle(shuffled)

        if shuffled:
            # Phase 1: 并发抓取所有成员的消息
            fetch_results = await asyncio.gather(
                *[fetcher.fetch_member_messages(m) for m in shuffled],
                return_exceptions=True,
            )

            # Phase 2: 多成员并发流水线推送（各成员内部保持时间顺序，跨成员完全并发）
            async def _push_one_member(i: int, result: object) -> _MemberCycleResult:
                member = shuffled[i]
                name = member['m_name'].replace(" ", "")

                if isinstance(result, Exception):
                    log_all(f"💥 抓取异常 [{name}]: {result}", is_error=True)
                    return _MemberCycleResult(name=name)

                if result is None:
                    acc_id = member.get("account_id") or ""
                    mid = str(member.get("m_id") or "")
                    from src.member_directory import is_member_active_subscription
                    if not acc_id or not mid or is_member_active_subscription(acc_id, mid) is False:
                        # 纯社媒/博客或未订阅/离线成员，跳过是正常调度，不作为巡查异常
                        return _MemberCycleResult(name=name, skipped=True)
                    return _MemberCycleResult(name=name)

                new_msgs, id_list, id_set, l_time_ref, time_file, file_lock = result  # type: ignore[misc]
                new_count = sum(
                    1 for msg in new_msgs
                    if str(msg.get("id") or msg.get("updated_at", "")) not in id_set
                )
                try:
                    ok = await fetcher.push_member_messages(
                        member, new_msgs, id_list, id_set, l_time_ref, time_file, file_lock
                    )
                except Exception:
                    log_all(f"💥 推送异常 [{name}]:\n{traceback.format_exc()}", is_error=True)
                    health.get_tracker().record_member_push(name, False)
                    return _MemberCycleResult(
                        name=name, fetch_ok=True, new_count=new_count, push_ok=False
                    )

                return _MemberCycleResult(
                    name=name, fetch_ok=True, new_count=new_count, push_ok=ok
                )

            member_results = await asyncio.gather(
                *[_push_one_member(i, res) for i, res in enumerate(fetch_results)]
            )
            summary, has_errors = cycle_summary_fn(
                member_results, time.monotonic() - message_cycle_started
            )
            log_all(summary, is_error=has_errors)
    else:
        log_all("⏸️ Message 监控已暂停（配置已关闭）", is_debug=True)

    # ── 博客巡查 ──
    blog_cfg = cfg._config.get("blog_monitor") or {}
    if blog_cfg.get("enabled", False) and cfg._config.get("blog_records") is not None:
        try:
            blog_client = getattr(app_mod, "_blog_client", None) if app_mod else None
            blog_db = getattr(app_mod, "_blog_db", None) if app_mod else None
            new_posts = await blog_fetcher.run_blog_cycle(
                blog_client, blog_db, cfg._config)
            health.get_tracker().record_member_fetch("博客 (全局)", True)
            if new_posts:
                from src.notifier import send_blog_post
                log_all(f"📝 博客更新：{len(new_posts)} 篇")
                for post in new_posts:
                    try:
                        ok = await send_blog_post(post)
                        if ok:
                            log_all(f"✅ 博客 [{post.get('title', '无题')}] 推送完成")
                        else:
                            log_all(f"⚠️ 博客 [{post.get('title', '无题')}] 推送失败（无可用通道）", is_error=True)
                    except Exception as e:
                        log_all(f"💥 博客推送异常: {e}", is_error=True)
                        health.get_tracker().record_member_push("博客 (全局)", False)
                    await asyncio.sleep(0.5)
            health.get_tracker().record_member_push("博客 (全局)", True)

        except Exception as e:
            health.get_tracker().record_member_fetch("博客 (全局)", False, health.ErrorTier.TRANSIENT, str(e))
            log_all(f"⚠️ 博客巡查异常: {e}", is_error=True)


async def _run_loop(http_client: httpx.AsyncClient, poll_event: asyncio.Event,
                    stop_event: asyncio.Event | None = None) -> None:
    app_mod = sys.modules.get("src.app")
    check_stop_fn = getattr(app_mod, "_stop_requested", _stop_requested) if app_mod else _stop_requested
    run_cycle_fn = getattr(app_mod, "_run_cycle", _run_cycle) if app_mod else _run_cycle

    while True:
        if check_stop_fn():
            log_all("🛑 检测到停止信号文件，优雅退出")
            if stop_event is not None:
                stop_event.set()
            return
        try:
            # ── 改进 3：休眠时段暂停轮询（手动触发可唤醒）──
            sleep_sec = _calc_sleep_seconds()
            if sleep_sec > 0:
                jst = _get_jst_now()
                log_all(
                    f"😴 休眠时段（{cfg.SLEEP_START_HOUR}:00-{cfg.SLEEP_END_HOUR}:00 JST），"
                    f"当前 {jst.hour:02d}:{jst.minute:02d}，暂停 {sleep_sec}s",
                    is_debug=True,
                )
                health.get_tracker().record_next_cycle(time.time() + sleep_sec, "😴 休眠")
                if not await _wait_or_trigger(poll_event, sleep_sec):
                    continue
                log_all("⏩ 休眠时段手动触发巡查", is_debug=True)

            await run_cycle_fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 任何未预料的异常都不该终止长驻循环：记录后照常等待下一轮
            log_all(f"💥 巡查轮次异常，跳过本轮:\n{traceback.format_exc()}", is_error=True)
            health.get_tracker().record_error("巡查轮次异常", health.ErrorTier.TRANSIENT)

        summary = health.get_tracker().cycle_complete()
        if summary:
            log_all(summary)

        wait_time, tag = _next_interval()
        health.get_tracker().record_next_cycle(time.time() + wait_time, tag)
        log_all(f"{tag} | 下次巡查: {wait_time}s 后", is_debug=True)
        # 长等待期间也要能及时响应停止信号，切成小段轮询
        waited = 0.0
        while waited < wait_time:
            slice_s = min(10.0, wait_time - waited)
            if await _wait_or_trigger(poll_event, slice_s):
                log_all("⏩ 手动触发巡查", is_debug=True)
                break
            waited += slice_s
            if check_stop_fn():
                break


__all__ = [
    "_MemberCycleResult",
    "_message_cycle_summary",
    "_calc_sleep_seconds",
    "_next_interval",
    "_wait_or_trigger",
    "_run_cycle",
    "_run_loop",
]
