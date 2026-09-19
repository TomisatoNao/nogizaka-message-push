"""关键 Token 续期失败必须可见、可告警且不能伪装为成功。"""

import asyncio

import httpx
import pytest

from src.config import credentials


def _configure_web_account(monkeypatch):
    monkeypatch.setattr(credentials.cfg, "ACCOUNTS", {
        "demo": {"group_type": "nogizaka46"},
    })
    monkeypatch.setattr(credentials, "ACCOUNT_CREDS", {
        "demo": {"token": "old-token", "cookies": {"session": "old-cookie"}},
    })
    monkeypatch.setattr(credentials, "_alert_last_sent", {})
    monkeypatch.setattr(credentials, "_refresh_state", {})


def test_web_token_refresh_network_failure_returns_false_and_alerts(monkeypatch):
    _configure_web_account(monkeypatch)
    alerted = []

    async def fail_post(*_args, **_kwargs):
        raise httpx.ReadTimeout("simulated timeout")

    async def fake_alert(group, text):
        alerted.append((group, text))

    monkeypatch.setattr(credentials, "_post", fail_post)
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "send_alert_message", fake_alert)

    assert not asyncio.run(credentials.refresh_token("demo", 12345))
    assert alerted and alerted[0][0] == 12345
    assert "未判定" in alerted[0][1]


def test_web_token_refresh_persistence_failure_is_not_reported_as_success(monkeypatch):
    _configure_web_account(monkeypatch)
    alerted = []

    class Response:
        status_code = 200
        text = '{"access_token":"new-token"}'

        class Headers:
            @staticmethod
            def get_list(_name):
                return []

        headers = Headers()

        @staticmethod
        def json():
            return {"access_token": "new-token"}

    async def successful_post(*_args, **_kwargs):
        return Response()

    async def fake_alert(group, text):
        alerted.append((group, text))

    monkeypatch.setattr(credentials, "_post", successful_post)
    monkeypatch.setattr(credentials, "_save_cred", lambda *_args: False)
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "send_alert_message", fake_alert)

    assert not asyncio.run(credentials.refresh_token("demo", 12345))
    assert alerted and "Cookie" in alerted[0][1]


def test_network_refresh_failure_is_cooled_down_and_not_repeated(monkeypatch):
    _configure_web_account(monkeypatch)
    calls = []

    async def fail_post(*_args, **_kwargs):
        calls.append(1)
        raise httpx.PoolTimeout("simulated pool exhaustion")

    async def fake_alert(*_args, **_kwargs):
        return None

    monkeypatch.setattr(credentials, "_post", fail_post)
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "send_alert_message", fake_alert)

    assert not asyncio.run(credentials.refresh_token("demo", 12345))
    assert not asyncio.run(credentials.refresh_token("demo", 12345))
    assert len(calls) == 1
    state = credentials.get_refresh_state("demo")
    assert state["kind"] == "transient_network"
    assert state["blocked"] is True


def test_refresh_failure_blocks_member_fetch(monkeypatch):
    _configure_web_account(monkeypatch)
    credentials._record_refresh_failure("demo", "transient_network", "PoolTimeout")

    from src import fetcher

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("续期失败后的成员不应继续请求 timeline")

    monkeypatch.setattr(fetcher, "_http_client", type("Client", (), {"get": fail_if_called})())
    monkeypatch.setattr(fetcher, "_semaphore", __import__("asyncio").Semaphore(1))
    member = {
        "account_id": "demo",
        "group_type": "nogizaka46",
        "m_id": "member-1",
        "m_name": "测试成员",
        "target_groups": [],
    }

    assert __import__("asyncio").run(fetcher.fetch_member_messages(member)) is None


def test_post_closed_loop_self_heal(monkeypatch):
    """测试复用客户端若发生 Event loop is closed 时能自动自愈回退。"""
    class ClosedLoopClient:
        is_closed = False

        async def post(self, *args, **kwargs):
            raise RuntimeError("Event loop is closed")

    fallback_called = []

    class MockFreshClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            fallback_called.append(True)
            resp = httpx.Response(200, json={"access_token": "healed"})
            return resp

    loop = asyncio.new_event_loop()
    try:
        credentials._http_client = ClosedLoopClient()
        credentials._auth_http_client = ClosedLoopClient()
        credentials._client_loop = loop
        monkeypatch.setattr(httpx, "AsyncClient", MockFreshClient)

        res = loop.run_until_complete(credentials._post("https://example.com/test", headers={}))
        assert res.status_code == 200
        assert len(fallback_called) == 1
    finally:
        credentials.clear_loop_state(loop)
        loop.close()


def test_post_pool_timeout_rebuilds_managed_auth_pool_and_retries_once(monkeypatch):
    """认证池耗尽时应替换旧池，并在新池上安全重试一次。"""
    calls = []

    class BrokenClient:
        is_closed = False

        async def post(self, *_args, **_kwargs):
            calls.append("broken")
            raise httpx.PoolTimeout("pool exhausted")

    class HealthyClient:
        is_closed = False

        async def post(self, *_args, **_kwargs):
            calls.append("healthy")
            return httpx.Response(200, json={"access_token": "renewed"})

    broken = BrokenClient()
    healthy = HealthyClient()

    async def fake_reset_auth_client(*, expected_client=None):
        assert expected_client is broken
        credentials.initialize(auth_client=healthy)
        return healthy

    from src import http_pool

    monkeypatch.setattr(http_pool, "reset_auth_client", fake_reset_auth_client)
    loop = asyncio.new_event_loop()
    try:
        credentials._auth_http_client = broken
        credentials._client_loop = loop
        response = loop.run_until_complete(
            credentials._post("https://example.com/v2/update_token", headers={})
        )
        assert response.status_code == 200
        assert calls == ["broken", "healthy"]
    finally:
        credentials.clear_loop_state(loop)
        loop.close()


def test_concurrent_pool_timeouts_share_rebuilt_auth_pool(monkeypatch):
    """并发失败的旧池请求都应转移到首个协程建立的新池。"""
    calls = []
    reset_calls = []
    both_started = asyncio.Event()

    class BrokenClient:
        is_closed = False

        async def post(self, *_args, **_kwargs):
            calls.append("broken")
            if calls.count("broken") == 2:
                both_started.set()
            await both_started.wait()
            raise httpx.PoolTimeout("pool exhausted")

    class HealthyClient:
        is_closed = False

        async def post(self, *_args, **_kwargs):
            calls.append("healthy")
            return httpx.Response(200, json={"access_token": "renewed"})

    broken = BrokenClient()
    healthy = HealthyClient()

    async def fake_reset_auth_client(*, expected_client=None):
        reset_calls.append(expected_client)
        credentials.initialize(auth_client=healthy)
        return healthy

    from src import http_pool

    monkeypatch.setattr(http_pool, "reset_auth_client", fake_reset_auth_client)

    async def run_two_posts():
        loop = asyncio.get_running_loop()
        credentials._auth_http_client = broken
        credentials._client_loop = loop
        try:
            return await asyncio.gather(
                credentials._post("https://example.com/v2/update_token", headers={}),
                credentials._post("https://example.com/v2/update_token", headers={}),
            )
        finally:
            credentials.clear_loop_state(loop)

    responses = asyncio.run(run_two_posts())
    assert [response.status_code for response in responses] == [200, 200]
    assert calls.count("broken") == 2
    assert calls.count("healthy") == 2
    assert reset_calls == [broken, broken]


def test_post_read_timeout_does_not_retry_refresh_request(monkeypatch):
    """可能已抵达上游的请求不可自动重放，避免 refresh_token 被重复消费。"""
    calls = []

    class ReadTimeoutClient:
        is_closed = False

        async def post(self, *_args, **_kwargs):
            calls.append(1)
            raise httpx.ReadTimeout("response lost")

    async def fail_if_reset(*_args, **_kwargs):
        raise AssertionError("ReadTimeout 不应重建并重试认证请求")

    from src import http_pool

    monkeypatch.setattr(http_pool, "reset_auth_client", fail_if_reset)
    loop = asyncio.new_event_loop()
    try:
        credentials._auth_http_client = ReadTimeoutClient()
        credentials._client_loop = loop
        with pytest.raises(httpx.ReadTimeout):
            loop.run_until_complete(
                credentials._post("https://example.com/v2/update_token", headers={})
            )
        assert calls == [1]
    finally:
        credentials.clear_loop_state(loop)
        loop.close()


def test_refresh_token_runtime_error_classification(monkeypatch):
    """验证遇到 RuntimeError（如事件循环关闭）时，归类为暂态调度异常而非凭据失效。"""
    _configure_web_account(monkeypatch)
    alerted = []

    async def fail_post(*_args, **_kwargs):
        raise RuntimeError("Event loop is closed")

    async def fake_alert(group, text):
        alerted.append((group, text))

    monkeypatch.setattr(credentials, "_post", fail_post)
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "send_alert_message", fake_alert)

    assert not asyncio.run(credentials.refresh_token("demo", 12345))
    state = credentials.get_refresh_state("demo")
    # 必须归类为暂态网络/环境异常，不能归类为 response_invalid 或 credential_invalid
    assert state["kind"] == "transient_network"
    assert "事件循环" in state["detail"]
    # 告警中应为暂态提示，而不是要求用户检查持久化
    if alerted:
        assert "请检查 Cookie/Token 持久化" not in alerted[0][1]
