"""NapCat 会话掉线告警回归测试。"""

from __future__ import annotations

import pytest

from src import app, notifier


@pytest.mark.asyncio
async def test_napcat_offline_alert_uses_existing_alert_routes(monkeypatch):
    calls: list[tuple[int, str]] = []

    async def fake_send_alert(target_group: int, text: str) -> bool:
        calls.append((target_group, text))
        return True

    monkeypatch.setattr(notifier, "send_alert_message", fake_send_alert)

    result = await app._send_napcat_session_alert("offline", "QQ 账号登录已失效")

    assert result is True
    assert calls == [(0, "NapCat QQ 会话已离线（可能是 KickedOffline/账号登录失效）。探针原因：QQ 账号登录已失效。请在 NapCat 中重新登录。")]


@pytest.mark.asyncio
async def test_napcat_recovery_alert_and_unknown_event(monkeypatch):
    calls: list[str] = []

    async def fake_send_alert(_target_group: int, text: str) -> bool:
        calls.append(text)
        return True

    monkeypatch.setattr(notifier, "send_alert_message", fake_send_alert)

    assert await app._send_napcat_session_alert("recovered") is True
    assert await app._send_napcat_session_alert("unreachable") is False
    assert calls == ["NapCat QQ 会话已恢复在线，离线熔断已解除。"]


@pytest.mark.asyncio
async def test_napcat_alert_failure_isolated(monkeypatch):
    async def failing_send_alert(_target_group: int, _text: str) -> bool:
        raise RuntimeError("simulated notifier failure")

    monkeypatch.setattr(notifier, "send_alert_message", failing_send_alert)

    assert await app._send_napcat_session_alert("offline", "QQ 账号离线") is False
