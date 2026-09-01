"""关键 Token 续期失败必须可见、可告警且不能伪装为成功。"""

import asyncio

import httpx

from config import credentials


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
