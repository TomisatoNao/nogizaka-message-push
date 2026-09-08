"""巡查超时、取消清理与真实监控心跳的回归测试。"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest

import config.config as cfg
from src import health
from src.app_modules import message_worker


def test_health_snapshot_reports_cycle_lifecycle_and_stale_heartbeat():
    health.initialize(
        cycle_timeout_seconds=1,
        monitor_stale_seconds=2,
        startup_grace_seconds=0,
    )
    tracker = health.get_tracker()
    tracker.set_startup_state("READY")
    tracker.record_cycle_started("cycle-test", "member_fetch")

    running = tracker.snapshot()
    assert running["cycle_in_progress"] is True
    assert running["monitor_healthy"] is True
    assert running["monitor"]["cycle_id"] == "cycle-test"
    assert running["monitor"]["phase"] == "member_fetch"

    tracker.record_cycle_finished("success", elapsed_ms=123.4)
    tracker.record_next_cycle(time.time() + 60, "test")
    scheduled = tracker.snapshot()
    assert scheduled["monitor_healthy"] is True
    assert scheduled["monitor_reason"] == "next_cycle_scheduled"
    assert scheduled["monitor"]["last_cycle_elapsed_ms"] == 123.4

    # 模拟没有下一轮调度且最近心跳已经过期。
    tracker._next_cycle = None
    tracker._last_cycle_completed_at = time.time() - 10
    stale = tracker.snapshot()
    assert stale["monitor_healthy"] is False
    assert stale["monitor_reason"] == "cycle_heartbeat_stale"

    health.initialize(cycle_timeout_seconds=1, monitor_stale_seconds=2, startup_grace_seconds=0)
    tracker = health.get_tracker()
    tracker.set_startup_state("READY")
    tracker.record_next_cycle(time.time() + 60, "😴 休眠")
    assert tracker.snapshot()["monitor_reason"] == "next_cycle_scheduled"


def test_health_startup_grace_begins_after_readiness_transition():
    """初始化/握手耗时不应消耗首轮监控的宽限期。"""
    health.initialize(monitor_stale_seconds=10, startup_grace_seconds=2)
    tracker = health.get_tracker()
    tracker.set_startup_state("READY")
    tracker._start_time = time.monotonic() - 30
    assert tracker.snapshot()["monitor_reason"] == "startup_grace"

    tracker._startup_updated_at = time.time() - 3
    stale = tracker.snapshot()
    assert stale["monitor_healthy"] is False
    assert stale["monitor_reason"] == "no_cycle_heartbeat"


def test_health_live_probe_exposes_stale_cycle(monkeypatch):
    import src.webui as webui

    health.initialize(
        cycle_timeout_seconds=1,
        monitor_stale_seconds=2,
        startup_grace_seconds=0,
    )
    tracker = health.get_tracker()
    tracker.set_startup_state("READY")
    tracker._last_cycle_completed_at = time.time() - 10
    tracker._next_cycle = None

    previous_poll = webui._on_poll_cb
    server = webui.start_webui(host="127.0.0.1", port=0, on_poll=lambda: None)
    assert server is not None
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(base + "/api/health/live", timeout=5)
        assert exc_info.value.code == 503
        payload = json.loads(exc_info.value.read().decode("utf-8"))
        assert payload["ok"] is False
        assert payload["monitor_healthy"] is False
        assert "heartbeat" in payload["monitor_reason"]
        assert any("后台巡查不可用" in reason for reason in payload["reasons"])
    finally:
        server.shutdown()
        server.server_close()
        webui._on_poll_cb = previous_poll


@pytest.mark.asyncio
async def test_member_fetch_timeout_keeps_context_and_does_not_cancel_other_tasks():
    async def slow_fetch():
        await asyncio.sleep(1)
        return "never"

    async def fast_fetch():
        return "ok"

    slow, fast = await asyncio.gather(
        message_worker._fetch_member_bounded(
            {"m_id": "55", "account_id": "nogizaka_main"},
            slow_fetch(),
            0.01,
        ),
        message_worker._fetch_member_bounded(
            {"m_id": "34", "account_id": "hinatazaka_main"},
            fast_fetch(),
            0.2,
        ),
    )
    assert isinstance(slow, message_worker._MemberFetchTimeout)
    assert slow.member_id == "55"
    assert slow.account_id == "nogizaka_main"
    assert fast == "ok"


@pytest.mark.asyncio
async def test_cycle_timeout_is_cancelled_and_lifecycle_is_closed(monkeypatch):
    health.initialize(
        cycle_timeout_seconds=0.01,
        monitor_stale_seconds=1,
        startup_grace_seconds=0,
    )
    health.get_tracker().set_startup_state("READY")
    cancelled = False

    async def never_finishes():
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise

    with pytest.raises(asyncio.TimeoutError):
        await message_worker._run_cycle_bounded(never_finishes, 0.01, "cycle-timeout")

    snap = health.get_tracker().snapshot()
    assert cancelled is True
    assert snap["cycle_in_progress"] is False
    assert snap["monitor"]["last_cycle_outcome"] == "timeout"
    assert any("cycle-timeout" in item["msg"] for item in snap["errors"])


@pytest.mark.asyncio
async def test_run_loop_continues_after_timed_out_cycle(monkeypatch):
    """一轮超时后，主循环仍能进入下一轮而不是永久停在 gather。"""
    import src.app as app

    health.initialize(
        cycle_timeout_seconds=0.01,
        monitor_stale_seconds=1,
        startup_grace_seconds=0,
    )
    health.get_tracker().set_startup_state("READY")
    calls = 0

    async def cycle():
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)

    def should_stop():
        return calls >= 2

    async def wait_or_trigger(_event, _timeout):
        return True

    monkeypatch.setattr(app, "_run_cycle", cycle)
    monkeypatch.setattr(app, "_stop_requested", should_stop)
    monkeypatch.setattr(message_worker, "_calc_sleep_seconds", lambda: 0)
    monkeypatch.setattr(message_worker, "_next_interval", lambda: (1, "test"))
    monkeypatch.setattr(message_worker, "_wait_or_trigger", wait_or_trigger)
    monkeypatch.setattr(cfg, "MESSAGE_CYCLE_TIMEOUT_SECONDS", 0.01, raising=False)

    await message_worker._run_loop(None, asyncio.Event())
    assert calls == 2


def test_login_page_has_request_timeout_and_visible_fallback():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "src" / "webui_static" / "login.html").read_text(
        encoding="utf-8"
    )
    assert "AbortController" in html
    assert "登录请求超时（30 秒）" in html
    assert "正在验证账号" in html
