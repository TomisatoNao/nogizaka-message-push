"""NapCat failure diagnostics retain route-local causes without sending messages."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import fetcher, notifier
from src.platforms import napcat


@pytest.mark.asyncio
async def test_napcat_failure_codes_are_route_local_under_timeout_tasks():
    async def sender(code):
        with napcat.track_delivery_progress() as progress:
            progress.last_error_code = code
            await asyncio.sleep(0.01)
            return False

    attempts = await asyncio.gather(*[
        notifier._run_delivery("napcat", "napcat:" + str(i), "QQ", str(i),
                               lambda code=code: sender(code), timeout_seconds=1)
        for i, code in enumerate(("network_error", "qq_offline", "auth_failed", "circuit_open"))
    ])
    assert [attempt.error_code for attempt in attempts] == [
        "network_error", "qq_offline", "auth_failed", "circuit_open",
    ]
    assert all(not attempt.ok for attempt in attempts)
    assert napcat.current_delivery_error() == ""


@pytest.mark.parametrize("error,guidance", [
    ("network_error", "监听端口"),
    ("qq_offline", "恢复登录"),
    ("auth_failed", "访问令牌"),
    ("timeout", "请求超时"),
])
@pytest.mark.asyncio
async def test_lane_alert_explains_root_cause_instead_of_generic_failure(monkeypatch, error, guidance):
    monkeypatch.setattr(napcat, "get_send_gate_snapshot", lambda: {
        "failure_threshold": 3, "failure_streak": 3, "last_error": error,
    })
    monkeypatch.setattr(fetcher, "_health_tracker", lambda: SimpleNamespace(alert_due=lambda *args: True))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert_message", send)
    await fetcher._maybe_alert_napcat_lane_failure(
        {"m_name": "测试成员"}, {"napcat:123"}, {"napcat:123": "delivery_failed"},
    )
    send.assert_awaited_once()
    message = send.await_args.args[1]
    assert error in message
    assert guidance in message
    assert "待补偿消息将按顺序保留" in message
    assert "delivery_failed" not in message


@pytest.mark.asyncio
async def test_lane_alert_does_not_replace_explicit_route_error_with_gate_error(monkeypatch):
    monkeypatch.setattr(napcat, "get_send_gate_snapshot", lambda: {
        "failure_threshold": 3, "failure_streak": 3, "last_error": "network_error",
    })
    monkeypatch.setattr(fetcher, "_health_tracker", lambda: SimpleNamespace(alert_due=lambda *args: True))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_alert_message", send)
    await fetcher._maybe_alert_napcat_lane_failure(
        {"m_name": "测试成员"}, {"napcat:123"}, {"napcat:123": "auth_failed"},
    )
    assert "auth_failed" in send.await_args.args[1]
    assert "network_error" not in send.await_args.args[1]
