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
