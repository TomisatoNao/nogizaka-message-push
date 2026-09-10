"""
src/app_modules/message_worker.py — 成员消息巡查、退避调度与多成员并发流水线
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import random
import sys
import time
import traceback
import uuid

import httpx

import config.config as cfg
from config.credentials import proactive_refresh_if_expiring
from src import fetcher, health, http_pool
from src.app_modules.daily_summary import _get_jst_now
from src.app_modules.process_lock import _stop_requested
from src.logger import log_all
from src.monitor_schedule import MonitorSchedule


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


class _MemberFetchTimeout(TimeoutError):
    """带成员上下文的抓取超时结果，不让一个成员拖住整轮巡查。"""

    def __init__(self, member_id: str, account_id: str, timeout_seconds: float):
        self.member_id = str(member_id or "")
        self.account_id = str(account_id or "")
        self.timeout_seconds = float(timeout_seconds)
        super().__init__(
            f"成员抓取超时（member_id={self.member_id or '-'}, "
            f"account_id={self.account_id or '-'}, timeout={self.timeout_seconds:g}s）"
        )


def _timeout_setting(name: str, default: float) -> float:
    """读取正数超时配置；配置损坏时回退安全默认值。"""
    try:
        value = float(getattr(cfg, name, default))
    except (TypeError, ValueError):
        value = float(default)
    return max(0.1, value)


def _current_cycle_id() -> str:
    """读取当前轮次 ID；日志上下文缺失时使用稳定占位符。"""
    try:
        return str(health.get_tracker().monitor_snapshot().get("cycle_id") or "-")
    except Exception:
        return "-"


def _consume_task_result(task: asyncio.Task) -> None:
    """消费被取消后迟到完成的任务结果，避免 ``Task exception was never retrieved``。"""
    try:
        task.result()
    except BaseException:
        pass


async def _cancel_task_bounded(task: asyncio.Task, cleanup_timeout: float = 5.0) -> bool:
    """请求取消任务并在有限时间内等待清理，返回是否已结束。"""
    if task.done():
        _consume_task_result(task)
        return True
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=max(0.1, cleanup_timeout))
    if done:
        _consume_task_result(task)
        return True
    task.add_done_callback(_consume_task_result)
    return False


async def _run_cycle_bounded(run_cycle_fn, timeout_seconds: float, cycle_id: str) -> None:
    """运行一轮巡查并设置总预算；超时后显式取消并有限等待清理。"""
    tracker = health.get_tracker()
    started = time.monotonic()
    tracker.record_cycle_started(cycle_id, "starting")
    log_all(
        f"🔄 巡查轮次开始 | cycle_id={cycle_id} | timeout={timeout_seconds:g}s",
        is_debug=True,
    )
    task = asyncio.create_task(run_cycle_fn(), name=f"message-cycle:{cycle_id}")
    try:
        done, pending = await asyncio.wait({task}, timeout=timeout_seconds)
        if pending:
            phase = tracker.monitor_snapshot().get("phase", "unknown")
            cleaned = await _cancel_task_bounded(task, cleanup_timeout=5.0)
            pool_reset = "skipped"
            # 只有主程序已绑定通用 Client 时才重建连接池。单元测试/独立
            # 工具未绑定 Client 不应因为一次超时凭空创建无法回收的新池。
            if getattr(http_pool, "_general_client", None) is not None:
                try:
                    await http_pool.reset_general_client()
                    pool_reset = "ok"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # nosec B110 - 恢复失败不能阻断下一轮
                    pool_reset = f"failed:{type(exc).__name__}"
                    log_all(
                        f"⚠️ 巡查超时后重建 HTTP 连接池失败 | cycle_id={cycle_id} | "
                        f"error={type(exc).__name__}: {exc}",
                        is_error=True,
                    )
            elapsed_ms = (time.monotonic() - started) * 1000
            tracker.record_cycle_finished("timeout", elapsed_ms=elapsed_ms, phase=phase)
            tracker.record_error(
                f"巡查轮次超时（cycle_id={cycle_id}, phase={phase}, "
                f"timeout={timeout_seconds:g}s）",
                health.ErrorTier.TRANSIENT,
            )
            log_all(
                f"⏱️ 巡查轮次超时 | cycle_id={cycle_id} | phase={phase} | "
                f"timeout={timeout_seconds:g}s | elapsed={elapsed_ms:.0f}ms | "
                f"cleanup={'ok' if cleaned else 'pending'} | pool_reset={pool_reset}",
                is_error=True,
            )
            raise asyncio.TimeoutError(
                f"巡查轮次超时（cycle_id={cycle_id}, phase={phase}）"
            )

        # wait() 已确认 task 完成；result() 会正确传播业务异常。
        task.result()
    except asyncio.CancelledError:
        phase = tracker.monitor_snapshot().get("phase", "unknown")
        await _cancel_task_bounded(task, cleanup_timeout=5.0)
        tracker.record_cycle_finished(
            "cancelled", elapsed_ms=(time.monotonic() - started) * 1000, phase=phase
        )
        raise
    except Exception:
        # 业务异常由外层循环记录；生命周期先结束，避免健康探针永久显示 running。
        if tracker.monitor_snapshot().get("cycle_in_progress"):
            tracker.record_cycle_finished(
                "error", elapsed_ms=(time.monotonic() - started) * 1000
            )
        raise
    else:
        elapsed_ms = (time.monotonic() - started) * 1000
        tracker.record_cycle_finished(
            "success", elapsed_ms=elapsed_ms, phase="idle"
        )
        log_all(
            f"✅ 巡查轮次结束 | cycle_id={cycle_id} | phase=idle | "
            f"outcome=success | elapsed_ms={elapsed_ms:.0f}",
            is_debug=True,
        )


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
    """若当前在全局内容监控休眠时段，返回到唤醒的秒数。"""
    return MonitorSchedule.from_config(cfg).seconds_until_wake(_get_jst_now())


def _next_interval() -> tuple[int, str]:
    schedule = MonitorSchedule.from_config(cfg)
    if schedule.interval_phase(_get_jst_now()) == "night":
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


async def _refresh_account_bounded(
    acc_id: str,
    target_group: int,
    account_cfg: object,
    timeout_seconds: float,
) -> None:
    """为单个账号续期设置边界，失败只影响该账号而不取消整轮。"""
    try:
        await asyncio.wait_for(
            proactive_refresh_if_expiring(
                acc_id,
                target_group,
                account_cfg=account_cfg,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        message = f"账号 {acc_id} Token 续期超时（{timeout_seconds:g}s）"
        health.get_tracker().record_error(message, health.ErrorTier.TRANSIENT)
        log_all(f"⏱️ {message} | cycle_id={_current_cycle_id()} | phase=token_refresh", is_error=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = f"账号 {acc_id} Token 续期异常: {type(exc).__name__}"
        health.get_tracker().record_error(message, health.ErrorTier.TRANSIENT)
        log_all(
            f"⚠️ {message} | cycle_id={_current_cycle_id()} | phase=token_refresh: {exc}",
            is_error=True,
        )


async def _fetch_member_bounded(member: dict, fetch_coro, timeout_seconds: float) -> object:
    """为单个成员抓取设置边界，并返回可定位的超时结果。"""
    try:
        return await asyncio.wait_for(fetch_coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return _MemberFetchTimeout(
            member.get("m_id", ""), member.get("account_id", ""), timeout_seconds
        )


async def _run_cycle() -> None:
    """单轮巡查：获取周期快照 → 主动续期 → 并发抓取 → 串行推送。"""
    app_mod = sys.modules.get("src.app")
    cycle_summary_fn = getattr(app_mod, "_message_cycle_summary", _message_cycle_summary) if app_mod else _message_cycle_summary

    # 小范围试点：获取本轮巡查不可变快照，保持本轮巡查期间配置视图一致
    snapshot = cfg.get_cycle_snapshot() if hasattr(cfg, "get_cycle_snapshot") else None

    # ── Phase 1: Message 消息巡查 ──
    if _is_message_monitor_enabled():
        message_cycle_started = time.monotonic()
        source_monitors = snapshot.monitor_list if snapshot else cfg.MONITOR_LIST
        valid_monitors = [m for m in source_monitors if m.get("account_id") and m.get("m_id")]

        # 每个账号取一个 target_group 作为报警目标
        account_target_groups: dict[str, int] = {
            m["account_id"]: _alert_group_for_account(m["account_id"])
            for m in valid_monitors
            if m.get("account_id")
        }

        # ── 改进 1：每轮巡查前主动检查并刷新即将过期的 Token ──
        if account_target_groups:
            tracker = health.get_tracker()
            tracker.record_cycle_phase("token_refresh")
            refresh_timeout = _timeout_setting("TOKEN_REFRESH_TIMEOUT_SECONDS", 30.0)
            await asyncio.gather(*[
                _refresh_account_bounded(
                    acc_id,
                    grp,
                    snapshot.accounts.get(acc_id) if snapshot else None,
                    refresh_timeout,
                )
                for acc_id, grp in account_target_groups.items()
            ])

        # ── 改进 4：随机打乱成员轮询顺序 ──
        shuffled = list(valid_monitors)
        random.shuffle(shuffled)

        if shuffled:
            # Phase 1: 并发抓取所有成员的消息（传入快照中的账号配置、回溯时间和过滤类型）
            tracker = health.get_tracker()
            tracker.record_cycle_phase("member_fetch")
            member_fetch_timeout = _timeout_setting("MEMBER_FETCH_TIMEOUT_SECONDS", 60.0)

            def _build_fetch_coro(m: dict):
                acc_id = m.get("account_id") or ""
                acc_cfg = snapshot.accounts.get(acc_id) if snapshot else None
                bt_hours = snapshot.backtrack_hours if snapshot else None
                skip_types = snapshot.skip_publish_types if snapshot else None
                return fetcher.fetch_member_messages(
                    m,
                    account_cfg=acc_cfg,
                    backtrack_hours=bt_hours,
                    skip_publish_types=skip_types,
                )

            fetch_results = await asyncio.gather(
                *[
                    _fetch_member_bounded(m, _build_fetch_coro(m), member_fetch_timeout)
                    for m in shuffled
                ],
                return_exceptions=True,
            )

            # Phase 2: 多成员并发流水线推送（各成员内部保持时间顺序，跨成员完全并发）
            tracker.record_cycle_phase("member_push")
            async def _push_one_member(i: int, result: object) -> _MemberCycleResult:
                member = shuffled[i]
                name = member['m_name'].replace(" ", "")
                member_id = str(member.get("m_id") or "-")
                account_id = str(member.get("account_id") or "-")

                if isinstance(result, _MemberFetchTimeout):
                    error = str(result)
                    health.get_tracker().record_member_fetch(
                        name, False, health.ErrorTier.TRANSIENT, error
                    )
                    log_all(
                        f"⏱️ 抓取超时 [{name}] | member_id={result.member_id or '-'} | "
                        f"account_id={result.account_id or '-'} | "
                        f"timeout={result.timeout_seconds:g}s | cycle_id={_current_cycle_id()} | "
                        "phase=member_fetch",
                        is_error=True,
                    )
                    return _MemberCycleResult(name=name)

                if isinstance(result, Exception):
                    health.get_tracker().record_member_fetch(
                        name, False, health.ErrorTier.TRANSIENT, str(result)
                    )
                    log_all(
                        f"💥 抓取异常 [{name}] | member_id={member_id} | "
                        f"account_id={account_id} | cycle_id={_current_cycle_id()} | "
                        f"phase=member_fetch | error={type(result).__name__}: {result}",
                        is_error=True,
                    )
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
                except Exception as exc:
                    log_all(
                        f"💥 推送异常 [{name}] | member_id={member_id} | "
                        f"account_id={account_id} | cycle_id={_current_cycle_id()} | "
                        f"phase=member_push | error={type(exc).__name__}: {exc}\n"
                        f"{traceback.format_exc()}",
                        is_error=True,
                    )
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
            log_all(f"{summary} | cycle_id={_current_cycle_id()} | phase=member_push", is_error=has_errors)
    else:
        log_all("⏸️ Message 监控已暂停（配置已关闭）", is_debug=True)

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
                schedule = MonitorSchedule.from_config(cfg)
                log_all(
                    f"😴 全局内容监控休眠（{schedule.sleep_start_hour}:00-"
                    f"{schedule.sleep_end_hour}:00 JST），"
                    f"当前 {jst.hour:02d}:{jst.minute:02d}，暂停 {sleep_sec}s",
                    is_debug=True,
                )
                health.get_tracker().record_next_cycle(time.time() + sleep_sec, "😴 休眠")
                if not await _wait_or_trigger(poll_event, sleep_sec):
                    continue
                log_all("⏩ 休眠时段手动触发巡查", is_debug=True)

            cycle_id = f"{int(time.time() * 1000):x}-{uuid.uuid4().hex[:6]}"
            cycle_timeout = _timeout_setting("MESSAGE_CYCLE_TIMEOUT_SECONDS", 900.0)
            await _run_cycle_bounded(run_cycle_fn, cycle_timeout, cycle_id)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            # _run_cycle_bounded 已记录 cycle_id / 阶段 / 清理结果；此处只继续调度。
            pass
        except Exception:
            # 任何未预料的异常都不该终止长驻循环：记录后照常等待下一轮
            monitor = health.get_tracker().monitor_snapshot()
            log_all(
                f"💥 巡查轮次异常，跳过本轮 | cycle_id={monitor.get('cycle_id') or '-'} | "
                f"phase={monitor.get('phase') or 'unknown'}:\n{traceback.format_exc()}",
                is_error=True,
            )
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
    "_MemberFetchTimeout",
    "_message_cycle_summary",
    "_calc_sleep_seconds",
    "_next_interval",
    "_wait_or_trigger",
    "_run_cycle_bounded",
    "_run_cycle",
    "_run_loop",
]
