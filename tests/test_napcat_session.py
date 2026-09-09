"""NapCat 会话探针回归测试。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import config.config as cfg
from src.platforms.napcat_session import (
    NapCatSessionMonitor,
    classify_status_response,
    resolve_status_endpoint,
)


class _Response:
    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls: list[str] = []
        self.headers: list[dict] = []

    async def get(self, url, *, headers=None):
        self.urls.append(url)
        self.headers.append(dict(headers or {}))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_resolve_status_endpoint_preserves_token_and_path(monkeypatch):
    monkeypatch.setattr(cfg, "QQ_USER_AGENT", "test-agent")
    endpoint, headers = resolve_status_endpoint(
        "http://napcat:3000/send_group_msg?access_token=secret-token"
    )
    assert endpoint == "http://napcat:3000/get_status?access_token=secret-token"
    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["User-Agent"] == "test-agent"


def test_status_classifier_handles_business_auth_failure_on_http_200():
    state, online, reason = classify_status_response(
        200,
        {"status": "failed", "retcode": 200, "message": "token verify failed!"},
    )
    assert (state, online) == ("auth_failed", None)
    assert reason == "鉴权失败"


@pytest.mark.asyncio
async def test_session_monitor_reports_online_then_offline_and_notifies():
    client = _FakeClient([
        _Response(200, {"status": "ok", "retcode": 0, "data": {"online": True}}),
        _Response(200, {"status": "ok", "retcode": 0, "data": {"online": False}}),
    ])
    snapshots = []
    monitor = NapCatSessionMonitor(
        client,
        "http://napcat:3000/send_group_msg",
        on_snapshot=snapshots.append,
    )

    first = await monitor.check_once()
    second = await monitor.check_once()

    assert first.state == "online"
    assert first.online is True
    assert second.state == "offline"
    assert second.online is False
    assert [item.state for item in snapshots] == ["online", "offline"]


@pytest.mark.asyncio
async def test_session_monitor_classifies_auth_and_network_failures():
    client = _FakeClient([
        _Response(403, {"message": "token verify failed!"}),
        httpx.ConnectError("unreachable"),
    ])
    monitor = NapCatSessionMonitor(client, "http://napcat:3000/send_group_msg")

    auth = await monitor.check_once()
    network = await monitor.check_once()

    assert auth.state == "auth_failed"
    assert auth.online is None
    assert network.state == "unreachable"
    assert network.consecutive_failures == 1


@pytest.mark.asyncio
async def test_session_monitor_start_stop_does_not_leave_task():
    client = _FakeClient([
        _Response(200, {"status": "ok", "retcode": 0, "data": {"online": True}}),
    ])
    monitor = NapCatSessionMonitor(client, "http://napcat:3000/send_group_msg")

    task = monitor.start()
    await asyncio.sleep(0)
    assert task is monitor.task
    await monitor.stop()
    assert monitor.task is None
    assert task.done()
