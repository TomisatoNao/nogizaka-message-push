"""NapCat 发送串行化、错误分类与熔断回归测试。"""

from __future__ import annotations

import asyncio

import pytest

import config.config as cfg
from src.platforms import napcat


class _Response:
    def __init__(self, status_code: int = 200, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if len(self.responses) > 1:
                return self.responses.pop(0)
            return self.responses[0]
        finally:
            self.active -= 1


def test_napcat_error_excerpt_redacts_tokens_and_local_paths():
    excerpt = napcat._safe_excerpt(
        "token=secret C:\\app\\data\\social_media\\x\\file.jpg /app/data/cache/file.jpg"
    )
    assert "secret" not in excerpt
    assert "<redacted>" in excerpt
    assert "<local-path>" in excerpt


@pytest.mark.asyncio
async def test_napcat_send_requests_are_serialized(monkeypatch):
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0)
    client = _FakeClient([_Response(body={"status": "ok", "retcode": 0})])
    napcat.initialize(client)

    results = await asyncio.gather(
        *(napcat.send_qq_message(100, [{"type": "text", "data": {"text": str(i)}}]) for i in range(5))
    )

    assert results == [True] * 5
    assert client.max_active == 1
    assert client.calls == 5


@pytest.mark.asyncio
async def test_kicked_offline_trips_gate_until_session_recovers(monkeypatch):
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0)
    client = _FakeClient([
        _Response(
            body={
                "status": "failed",
                "retcode": 200,
                "message": "EventChecker KickedOffLine: account offline",
            }
        ),
        _Response(body={"status": "ok", "retcode": 0}),
    ])
    napcat.initialize(client)

    assert await napcat.send_qq_message(100, [{"type": "text", "data": {"text": "first"}}], max_retries=1) is False
    snapshot = napcat.get_send_gate_snapshot()
    assert snapshot["blocked"] is True
    assert snapshot["blocked_reason"] == "qq_offline"

    # 熔断期间不再向已经掉线的账号发请求。
    assert await napcat.send_qq_message(100, [{"type": "text", "data": {"text": "skipped"}}], max_retries=1) is False
    assert client.calls == 1

    napcat.set_session_state("online")
    assert await napcat.send_qq_message(100, [{"type": "text", "data": {"text": "recovered"}}], max_retries=1) is True
    assert client.calls == 2


@pytest.mark.asyncio
async def test_rich_media_failure_does_not_trip_offline_gate(monkeypatch):
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0)
    client = _FakeClient([
        _Response(
            body={
                "status": "failed",
                "retcode": 200,
                "message": "EventChecker Failed: rich media transfer failed",
            }
        ),
        _Response(body={"status": "ok", "retcode": 0}),
    ])
    napcat.initialize(client)

    media = [{"type": "video", "data": {"file": "https://example.invalid/a.mp4"}}]
    assert await napcat.send_qq_message(100, media, max_retries=1) is False
    snapshot = napcat.get_send_gate_snapshot()
    assert snapshot["blocked"] is False
    assert snapshot["failure_streak"] == 0
    assert await napcat.send_qq_message(100, [{"type": "text", "data": {"text": "next"}}], max_retries=1) is True
    assert client.calls == 2


@pytest.mark.asyncio
async def test_http_403_is_classified_as_auth_failure(monkeypatch):
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0)
    client = _FakeClient([_Response(status_code=403, body={"message": "token verify failed!"})])
    napcat.initialize(client)

    assert await napcat.send_qq_message(100, [{"type": "text", "data": {"text": "auth"}}], max_retries=3) is False
    assert client.calls == 1
    assert napcat.get_send_gate_snapshot()["blocked_reason"] == "auth_failed"
