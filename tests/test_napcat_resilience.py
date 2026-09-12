"""NapCat 发送串行化、错误分类与熔断回归测试。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import config.config as cfg
from src import health
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


class _TemporaryClient(_FakeClient):
    def __init__(self, responses):
        super().__init__(responses)
        self.closed = False

    async def aclose(self):
        self.closed = True


def test_napcat_error_excerpt_redacts_tokens_and_local_paths():
    excerpt = napcat._safe_excerpt(
        "token=secret C:\\app\\data\\social_media\\x\\file.jpg /app/data/cache/file.jpg"
    )
    assert "secret" not in excerpt
    assert "<redacted>" in excerpt
    assert "<local-path>" in excerpt


def test_qqnt_network_error_code_is_classified_separately():
    body = {
        "status": "failed",
        "retcode": 200,
        "data": {"result": 1006514, "errMsg": "网络连接异常!"},
    }
    assert napcat._classify_error(200, body) == "qq_send_network_error"


def test_nested_qqnt_error_text_is_included_in_stable_classification():
    body = {
        "status": "failed",
        "retcode": 200,
        "message": "EventChecker Failed",
        "data": {"errMsg": "网络连接异常!"},
    }
    assert napcat._classify_error(200, body) == "qq_send_network_error"


def test_qq_group_message_quota_error_is_classified_as_rate_limited():
    body = {
        "status": "failed",
        "retcode": 200,
        "message": "本群每分钟只能发10条消息",
    }
    assert napcat._classify_error(200, body) == "rate_limited"


def test_forward_endpoint_reuses_configured_path_and_auth_query():
    url, headers = napcat._resolve_api_action_url(
        "http://napcat.example/api/send_group_msg?access_token=secret",
        "send_group_forward_msg",
    )
    assert url == "http://napcat.example/api/send_group_forward_msg?access_token=secret"
    assert headers["Authorization"] == "Bearer secret"


def test_structured_napcat_base_appends_action_and_uses_separate_token(monkeypatch):
    monkeypatch.setattr(cfg, "QQ_BOT_API", "http://napcat.example:36036", raising=False)
    monkeypatch.setattr(cfg, "NAPCAT_API_BASE", "http://napcat.example:36036", raising=False)
    monkeypatch.setattr(cfg, "NAPCAT_API_TOKEN", "separate-token", raising=False)

    url, headers = napcat._resolve_api_action_url(
        cfg.QQ_BOT_API,
        "send_group_forward_msg",
    )

    assert url == "http://napcat.example:36036/send_group_forward_msg"
    assert "access_token" not in url
    assert headers["Authorization"] == "Bearer separate-token"


@pytest.mark.asyncio
async def test_forward_read_timeout_is_not_retried_or_reported_failed(monkeypatch):
    """NapCat 已收到请求后的回包超时不能重试，否则会发出重复卡片。"""

    class _ReadTimeoutClient:
        def __init__(self):
            self.calls = 0

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            raise httpx.ReadTimeout(
                "slow forward response",
                request=httpx.Request("POST", "http://napcat/send_group_forward_msg"),
            )

    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0)
    monkeypatch.setattr(cfg, "QQ_BOT_API", "http://napcat:36036", raising=False)
    client = _ReadTimeoutClient()
    napcat.initialize(client)

    result = await napcat.send_group_forward_message(
        100,
        [{"type": "node", "data": {"content": []}}],
        max_retries=3,
    )

    assert result is True
    assert client.calls == 1
    assert napcat.get_send_gate_snapshot()["failure_streak"] == 0


def test_cross_loop_send_uses_temporary_client(monkeypatch):
    """工作线程的新事件循环不能复用主循环创建的 httpx transport。"""

    owner_client = _FakeClient([_Response(body={"status": "ok", "retcode": 0})])

    async def _bind_owner():
        napcat.initialize(owner_client)

    asyncio.run(_bind_owner())
    temporary = _TemporaryClient([
        _Response(body={"status": "ok", "retcode": 0})
    ])
    monkeypatch.setattr(napcat.httpx, "AsyncClient", lambda **_kwargs: temporary)
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0)
    monkeypatch.setattr(cfg, "QQ_BOT_API", "http://napcat:36036", raising=False)

    result = asyncio.run(
        napcat.send_qq_message(
            100,
            [{"type": "text", "data": {"text": "cross-loop"}}],
            max_retries=1,
        )
    )

    assert result is True
    assert owner_client.calls == 0
    assert temporary.calls == 1
    assert temporary.closed is True


@pytest.mark.asyncio
async def test_online_probe_does_not_hide_failed_send(monkeypatch):
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0)
    health.initialize()
    health.get_tracker().record_napcat_session("online", online=True, reason="在线")
    client = _FakeClient([
        _Response(
            body={
                "status": "failed",
                "retcode": 200,
                "message": "EventChecker result=1006514 网络连接异常!",
            }
        )
    ])
    napcat.initialize(client)

    assert await napcat.send_qq_message(100, [{"type": "text", "data": {"text": "probe"}}], max_retries=1) is False

    snapshot = health.get_tracker().snapshot()
    assert snapshot["napcat_session"]["state"] == "online"
    assert snapshot["napcat_send"]["state"] == "failed"
    assert snapshot["napcat_send"]["last_error"] == "qq_send_network_error"
    assert snapshot["napcat_send"]["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_qqnt_network_error_trips_gate_and_online_probe_keeps_it_blocked(monkeypatch):
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0)
    client = _FakeClient([
        _Response(
            body={
                "status": "failed",
                "retcode": 200,
                "message": "EventChecker result=1006514 网络连接异常!",
            }
        )
    ])
    napcat.initialize(client)

    for _ in range(3):
        assert await napcat.send_qq_message(100, [{"type": "text", "data": {"text": "probe"}}], max_retries=1) is False
    assert napcat.get_send_gate_snapshot()["blocked_reason"] == "qq_send_network_error"

    # get_status=online 不能把 QQNT 发送故障直接清掉。
    napcat.set_session_state("online")
    assert napcat.get_send_gate_snapshot()["blocked"] is True
    assert await napcat.send_qq_message(100, [{"type": "text", "data": {"text": "skipped"}}], max_retries=1) is False
    assert client.calls == 3
    send_snapshot = health.get_tracker().snapshot()["napcat_send"]
    assert send_snapshot["state"] == "degraded"
    assert send_snapshot["last_error"] == "qq_send_network_error"


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
