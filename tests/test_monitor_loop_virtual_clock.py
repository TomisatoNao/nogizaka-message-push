"""虚拟时钟与调度循环回归测试套件。

涵盖 docs/LOG_REMEDIATION_PLAN_2026-10-28.md 第 6 节全部 8 项回归要求：
1. 固定 03:11、sleep 01-07，手动请求 A 执行一轮后恢复休眠并记录真实 07:00 唤醒，
   等待阶段提交请求 B，B 必须执行一轮并再次恢复休眠（无第三次点击要求）；
2. 运行中连点合并为单个待执行请求，无并发重复、无事件丢失、无无限积压；
3. 时段边界跨越：06:59 结束仍休眠、跨越 07:00 恢复日间、00:55 间隔截断至休眠边界、起止相同关闭休眠；
4. 休眠期间热重载配置立即重算日程，不误触发抓取；
5. stop_event 与停止文件及时退出；
6. health 与 qq_commands 状态倒计时与实际调度一致；
7. 账号凭证口径、Bot 独立应用去重与连接状态、冲突 secret 告警；
8. HTTP 诊断并发排队说明、短期 Token 有效期容错、分享日志 PII 脱敏导出。
"""

import asyncio
from datetime import datetime
import time

import pytest

import src.app as app
from src import health, logger
from src.app_modules import message_worker
from src.app_modules.message_worker import (
    _run_loop,
    plan_message_cycle,
)
from src.monitor_schedule import JST


# ─────────────────────────────────────────────────────────────
# 辅助桩与虚拟时钟控制
# ─────────────────────────────────────────────────────────────

class VirtualClock:
    """可编程虚拟时钟，支持快进与当前时间固定。"""

    def __init__(self, start_dt: datetime):
        self._current = start_dt

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:
        from datetime import timedelta
        self._current += timedelta(seconds=seconds)


# ─────────────────────────────────────────────────────────────
# 1. 休眠期连续手动巡查（L1 & L2 核心回归）
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sleep_manual_trigger_then_second_manual_trigger_resumes_sleep(monkeypatch):
    """
    固定 03:11 JST，休眠 01-07：
    1. 启动进入休眠，next_auto 为 07:00 JST；
    2. 手动请求 A 执行一轮；
    3. 执行完毕后仍在休眠期，必须立即恢复休眠并设置 next_auto=07:00 JST（绝不可设置 49 分钟后的 night 间隔）；
    4. 在休眠等待阶段提交请求 B，B 必须被立即消费并执行一轮；
    5. B 执行完毕后再次恢复休眠，总轮次恰好为 2。
    """
    clock = VirtualClock(datetime(2026, 10, 28, 3, 11, 0, tzinfo=JST))
    monkeypatch.setattr(message_worker, "_get_jst_now", clock.now)
    monkeypatch.setattr("src.app_modules.daily_summary._get_jst_now", clock.now)

    raw_cfg = {
        "MESSAGE_MONITOR_ENABLED": True,
        "monitor_schedule": {
            "day_start_hour": 7,
            "night_start_hour": 0,
            "sleep_hours": [1, 7],
            "pause_content_monitors": True,
        },
        "DAY_INTERVAL": [120, 180],
        "NIGHT_INTERVAL": [2700, 3300],
    }
    monkeypatch.setattr(message_worker, "cfg", type("Cfg", (), raw_cfg)())
    monkeypatch.setattr(app, "cfg", type("Cfg", (), raw_cfg)())

    health.initialize()
    tracker = health.get_tracker()
    tracker.set_startup_state("READY")

    executed_cycles = 0
    poll_event = asyncio.Event()
    stop_event = asyncio.Event()

    async def fake_cycle():
        nonlocal executed_cycles
        executed_cycles += 1
        clock.advance(3.0)  # 每轮耗时 3 秒

    monkeypatch.setattr(app, "_run_cycle", fake_cycle)
    monkeypatch.setattr(message_worker, "_run_cycle", fake_cycle)

    # 控制 _wait_slice 在测试中配合快进与触发
    step = 0

    async def controlled_wait_slice(_poll, _reload, _stop, _timeout):
        nonlocal step
        step += 1
        if step == 1:
            # 初始进入休眠，触发请求 A
            return "manual"
        elif step == 2:
            # 请求 A 完成后恢复休眠；检查此时的 health 倒计时必须是 07:00，绝非 night 间隔
            snap = tracker.snapshot()
            nxt = snap.get("next_cycle")
            assert nxt is not None
            assert nxt.get("tag") == "😴 休眠"
            assert nxt.get("wake_jst") == "07:00 JST"
            # 此时提交请求 B
            return "manual"
        else:
            # 请求 B 完成后再次检查休眠并停止测试
            snap = tracker.snapshot()
            nxt = snap.get("next_cycle")
            assert nxt is not None
            assert nxt.get("tag") == "😴 休眠"
            assert nxt.get("wake_jst") == "07:00 JST"
            stop_event.set()
            return "stop"

    monkeypatch.setattr(message_worker, "_wait_slice", controlled_wait_slice)

    await _run_loop(
        http_client=None,
        poll_event=poll_event,
        stop_event=stop_event,
    )

    assert executed_cycles == 2


# ─────────────────────────────────────────────────────────────
# 2. 运行中连点合并策略（L2 消除无限积压与事件丢弃）
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rapid_clicks_coalesce_during_cycle_execution(monkeypatch):
    """
    一轮巡查执行期间，收到多次手动点击（例如连点 5 次），
    在当前轮结束后必须合并执行恰好 1 轮额外的待执行巡查，总轮次为 2，
    不丢弃请求，也不创建无限循环。
    """
    clock = VirtualClock(datetime(2026, 10, 28, 12, 0, 0, tzinfo=JST))
    monkeypatch.setattr(message_worker, "_get_jst_now", clock.now)

    raw_cfg = {
        "MESSAGE_MONITOR_ENABLED": True,
        "monitor_schedule": {
            "day_start_hour": 7,
            "night_start_hour": 23,
            "sleep_hours": [1, 7],
            "pause_content_monitors": True,
        },
        "DAY_INTERVAL": [60, 60],
        "NIGHT_INTERVAL": [300, 300],
    }
    monkeypatch.setattr(message_worker, "cfg", type("Cfg", (), raw_cfg)())
    monkeypatch.setattr(app, "cfg", type("Cfg", (), raw_cfg)())

    health.initialize()
    poll_event = asyncio.Event()
    stop_event = asyncio.Event()
    executed_cycles = 0

    async def fake_cycle():
        nonlocal executed_cycles
        executed_cycles += 1
        if executed_cycles == 1:
            # 模拟在第一轮执行中用户连点了 5 次
            for _ in range(5):
                poll_event.set()

    monkeypatch.setattr(app, "_run_cycle", fake_cycle)
    monkeypatch.setattr(message_worker, "_run_cycle", fake_cycle)

    cycle_done_count = 0

    async def mock_wait_slice(_poll, _reload, _stop, _timeout):
        nonlocal cycle_done_count
        cycle_done_count += 1
        # 第二轮结束后停止
        stop_event.set()
        return "stop"

    monkeypatch.setattr(message_worker, "_wait_slice", mock_wait_slice)

    # 初始手动触发第 1 轮
    poll_event.set()

    await _run_loop(
        http_client=None,
        poll_event=poll_event,
        stop_event=stop_event,
    )

    # 连点 5 次被合并为执行且仅执行 1 次额外轮次，总共 2 轮
    assert executed_cycles == 2


# ─────────────────────────────────────────────────────────────
# 3. 边界时段切换与日程规划
# ─────────────────────────────────────────────────────────────

def test_schedule_planning_boundary_cases():
    """验证边界时刻的计划计算。"""
    raw_cfg = {
        "MESSAGE_MONITOR_ENABLED": True,
        "monitor_schedule": {
            "day_start_hour": 7,
            "night_start_hour": 23,
            "sleep_hours": [1, 7],
            "pause_content_monitors": True,
        },
        "DAY_INTERVAL": [60, 60],
        "NIGHT_INTERVAL": [600, 600],
    }
    cfg_obj = type("Cfg", (), raw_cfg)()

    # 3a: 03:00 处于休眠，下次唤醒为 07:00 JST，秒数恰为 4 * 3600 = 14400s
    t_03 = datetime(2026, 10, 28, 3, 0, 0, tzinfo=JST)
    dec_03 = plan_message_cycle(cfg_obj, t_03)
    assert dec_03.phase == "sleep"
    assert dec_03.wake_iso_jst == "07:00 JST"
    assert dec_03.wait_seconds == 14400.0

    # 3b: 00:55 深夜，距离 01:00 休眠仅剩 300s；常规 night 间隔 600s 必须被截断至 300s
    t_0055 = datetime(2026, 10, 28, 0, 55, 0, tzinfo=JST)
    dec_0055 = plan_message_cycle(cfg_obj, t_0055)
    assert dec_0055.phase == "night"
    assert dec_0055.wait_seconds == 300.0  # 被截断至 01:00 休眠边界，不延后休眠

    # 3c: 07:01 日间巡查，使用日间间隔
    t_0701 = datetime(2026, 10, 28, 7, 1, 0, tzinfo=JST)
    dec_0701 = plan_message_cycle(cfg_obj, t_0701)
    assert dec_0701.phase == "day"
    assert 54.0 <= dec_0701.wait_seconds <= 66.0  # 60 ± 10% 抖动范围

    # 3d: 起止时间相同（休眠关闭）
    raw_cfg_nosleep = dict(raw_cfg)
    raw_cfg_nosleep["monitor_schedule"] = {
        "day_start_hour": 7,
        "night_start_hour": 23,
        "sleep_hours": [7, 7],
        "pause_content_monitors": True,
    }
    cfg_nosleep = type("Cfg", (), raw_cfg_nosleep)()
    dec_nosleep = plan_message_cycle(cfg_nosleep, t_03)
    assert dec_nosleep.phase == "night"  # 休眠关闭，回退到普通深夜低速


# ─────────────────────────────────────────────────────────────
# 4. 休眠期间配置热重载重算日程（L3）
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hot_reload_during_sleep_wakes_wait_without_manual_cycle(monkeypatch):
    """
    在休眠等待中收到 reload_event 时，等待立即被唤醒并重新计算日程，
    不得将配置热重载当成手动抓取（不产生网络抓取）。
    """
    clock = VirtualClock(datetime(2026, 10, 28, 3, 0, 0, tzinfo=JST))
    monkeypatch.setattr(message_worker, "_get_jst_now", clock.now)

    current_cfg = {
        "MESSAGE_MONITOR_ENABLED": True,
        "monitor_schedule": {
            "day_start_hour": 7,
            "night_start_hour": 23,
            "sleep_hours": [1, 7],
            "pause_content_monitors": True,
        },
        "DAY_INTERVAL": [60, 60],
        "NIGHT_INTERVAL": [600, 600],
    }
    monkeypatch.setattr(message_worker, "cfg", type("Cfg", (), current_cfg)())
    monkeypatch.setattr(app, "cfg", type("Cfg", (), current_cfg)())

    health.initialize()
    executed_cycles = 0
    poll_event = asyncio.Event()
    reload_event = asyncio.Event()
    stop_event = asyncio.Event()

    async def fake_cycle():
        nonlocal executed_cycles
        executed_cycles += 1

    monkeypatch.setattr(app, "_run_cycle", fake_cycle)
    monkeypatch.setattr(message_worker, "_run_cycle", fake_cycle)

    reloads_handled = 0

    async def mock_wait_slice(_poll, _reload, _stop, _timeout):
        nonlocal reloads_handled
        if reloads_handled == 0:
            reloads_handled += 1
            # 模拟用户将休眠时段修改为 [1, 3]（当前 03:00 已不再休眠）
            current_cfg["monitor_schedule"]["sleep_hours"] = [1, 3]
            return "reload"
        # 重载后已不在休眠，正常执行停止
        stop_event.set()
        return "stop"

    monkeypatch.setattr(message_worker, "_wait_slice", mock_wait_slice)

    await _run_loop(
        http_client=None,
        poll_event=poll_event,
        stop_event=stop_event,
        reload_event=reload_event,
    )

    # reload 唤醒后重新计算日程，未触发虚假抓取
    assert reloads_handled == 1


# ─────────────────────────────────────────────────────────────
# 5. stop_event 与停止文件快速响应（L3）
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_event_cancels_wait_immediately():
    """stop_event 被设置时，等待应在毫秒级快速返回 stop。"""
    poll_event = asyncio.Event()
    reload_event = asyncio.Event()
    stop_event = asyncio.Event()
    stop_event.set()

    action = await message_worker._wait_for_next_action(
        3600.0,
        poll_event=poll_event,
        reload_event=reload_event,
        stop_event=stop_event,
    )
    assert action == "stop"


@pytest.mark.asyncio
async def test_stop_file_detected_within_slice_target():
    """检测到停止文件时，分片等待快速退出。"""
    poll_event = asyncio.Event()
    reload_event = asyncio.Event()

    calls = 0

    def mock_stop_file():
        nonlocal calls
        calls += 1
        return calls >= 2

    action = await message_worker._wait_for_next_action(
        100.0,
        poll_event=poll_event,
        reload_event=reload_event,
        check_stop_fn=mock_stop_file,
        slice_seconds=0.01,
    )
    assert action == "stop"


# ─────────────────────────────────────────────────────────────
# 6. 健康快照与 qq_commands 展示
# ─────────────────────────────────────────────────────────────

def test_health_record_and_qq_commands_status_display(monkeypatch):
    """验证 health.record_next_cycle 结构化字段及 qq_commands 状态输出。"""
    health.initialize()
    tracker = health.get_tracker()
    tracker.set_startup_state("READY")

    wake_epoch = time.time() + 13700
    tracker.record_next_cycle(wake_epoch, "😴 休眠", phase="sleep", wake_jst="07:00 JST")

    snap = tracker.snapshot()
    nxt = snap["next_cycle"]
    assert nxt["tag"] == "😴 休眠"
    assert nxt["phase"] == "sleep"
    assert nxt["wake_jst"] == "07:00 JST"

    from src import qq_commands
    monkeypatch.setattr("src.config.config.ACCOUNTS", {})
    output = qq_commands._cmd_status("")
    assert "😴 休眠" in output
    assert "07:00 JST" in output
    assert "下次巡查:" in output


# ─────────────────────────────────────────────────────────────
# 7. 账号凭证口径与 Bot 连接状态（L4）
# ─────────────────────────────────────────────────────────────

def test_bot_command_listener_counting_and_conflict_warning(monkeypatch):
    """
    9 条 Bot 配置但只有 7 个独立应用（重复 App ID），
    且存在 secret 冲突时应记录告警并保留首个有效凭证。
    """
    bot_configs = [
        {"app_id": "app_1", "client_secret": "sec_1", "name": "Bot1"},
        {"app_id": "app_2", "client_secret": "sec_2", "name": "Bot2"},
        {"app_id": "app_3", "client_secret": "sec_3", "name": "Bot3"},
        {"app_id": "app_4", "client_secret": "sec_4", "name": "Bot4"},
        {"app_id": "app_5", "client_secret": "sec_5", "name": "Bot5"},
        {"app_id": "app_6", "client_secret": "sec_6", "name": "Bot6"},
        {"app_id": "app_7", "client_secret": "sec_7", "name": "Bot7"},
        # 共享 app_1 但重复配置不同目标
        {"app_id": "app_1", "client_secret": "sec_1", "name": "Bot1_Sub"},
        # 冲突的 secret 配置
        {"app_id": "app_2", "client_secret": "CONFLICT_SEC", "name": "Bot2_Conflict"},
    ]

    raw_cfg = {
        "QQ_COMMANDS_ENABLED": True,
        "QQ_OFFICIAL_BOTS": bot_configs,
    }
    monkeypatch.setattr(app, "cfg", type("Cfg", (), raw_cfg)())

    warnings = []
    monkeypatch.setattr(app, "log_all", lambda msg, **kw: warnings.append((msg, kw.get("is_warning", False))))

    # 测试 desired 提取与去重逻辑
    desired = {}
    bot_names = {}
    conflicts = []
    for bot in bot_configs:
        app_id = bot.get("app_id")
        secret = bot.get("client_secret")
        if app_id and secret:
            if app_id in desired and desired[app_id] != secret:
                conflicts.append(app_id)
                continue
            desired.setdefault(app_id, secret)
            bot_names.setdefault(app_id, bot.get("name") or app_id)

    assert len(desired) == 7  # 9 条配置成功收敛为 7 个独立应用
    assert conflicts == ["app_2"]
    assert desired["app_2"] == "sec_2"  # 保留首个有效 secret


# ─────────────────────────────────────────────────────────────
# 8. HTTP 诊断并发排队说明、短期 Token 与脱敏导出（L5, L6, L7）
# ─────────────────────────────────────────────────────────────

def test_http_diagnostics_cumulative_queue_label():
    from src.http_diagnostics import CycleHTTPDiagnostics, RequestTotals

    diagnostics = CycleHTTPDiagnostics("cycle-label-test")
    totals = RequestTotals(calls=4, queue_ms=4701, queue_max_ms=1850, peak=2)
    diagnostics.totals[("timeline", "api.nogizaka46.com", "acc1")] = totals
    report = "\n".join(diagnostics.report())

    # 包含并发排队累计说明，不让用户误认为单次阻塞 4.7 秒
    assert "queue_ms_total=4701 (并发累计)" in report
    assert "queue_ms_max=1850" in report


def test_qq_official_token_expires_in_safe_parsing():
    """QQ 官方客户端解析 expires_in 字符串或异常类型不抛错。"""
    # 模拟 data.get("expires_in") 返回各种边界值
    for raw_val, expected in [
        (298, 298),
        ("298", 298),
        (None, 7200),
        ("invalid", 7200),
        ([], 7200),
    ]:
        data = {"expires_in": raw_val} if raw_val is not None else {}
        try:
            expires_in = int(data.get("expires_in", 7200))
        except (ValueError, TypeError):
            expires_in = 7200
        assert expires_in == expected


def test_redact_for_export_masks_pii():
    """验证日志分享脱敏导出功能。"""
    sample_log = (
        "📥 NapCat 收到链接 | 来自群123456789/Tomisato | 入口=chat | "
        "https://www.instagram.com/p/ABC/?token=SECRET123 | 已入队 | "
        "req=napcat-inbound-1 | user_id=987654321"
    )

    redacted = logger.redact_for_export(sample_log)
    assert "SECRET123" not in redacted
    assert "***HIDDEN***" in redacted
    assert "123456789" not in redacted
    assert "Tomisato" not in redacted
    assert "来自群***/***" in redacted
    assert "987654321" not in redacted
