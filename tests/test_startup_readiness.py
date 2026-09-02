"""首次启动与配置未完成时的低噪声/无副作用回归测试。"""

from __future__ import annotations

import pytest
from pathlib import Path
import httpx

from src import app, health, logger


class _NoCallClient:
    """健康检查在首次运行不应调用任何外部推送探针。"""

    async def get(self, *_args, **_kwargs):
        raise AssertionError("首次运行未启用推送通道，不应发起 NapCat 探针请求")


@pytest.mark.asyncio
async def test_first_run_health_check_is_setup_state_without_errors(monkeypatch):
    """示例配置的默认状态不会把未配置账号/目标记录成 ERROR。"""
    monkeypatch.setattr(app.cfg, "ENABLE_NAPCAT_QQ", False)
    monkeypatch.setattr(app.cfg, "ENABLE_QQ_OFFICIAL_BOT", False)
    monkeypatch.setattr(app.cfg, "ENABLE_TG_BOT", False)
    monkeypatch.setattr(app.cfg, "MESSAGE_MONITOR_ENABLED", False)
    monkeypatch.setattr(app.cfg, "MONITOR_LIST", [])
    monkeypatch.setattr(app.cfg, "ACCOUNTS", {"preset": {}})
    monkeypatch.setattr(app.cfg, "BLOG_MONITOR", {"enabled": False})
    monkeypatch.setattr(app.cfg, "PLATFORMS", {})
    monkeypatch.setattr(app.cfg, "QQ_BOT_API", "http://127.0.0.1:3000/send_group_msg")

    logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(app, "log_all", lambda content, **kwargs: logs.append((str(content), kwargs)))
    health.initialize()

    result = await app._health_check(_NoCallClient())

    assert result is True
    assert not any(item.get("is_error") for _, item in logs)
    assert any("推送通道尚未启用" in text for text, _ in logs)
    assert any("Message 监控尚未启用" in text for text, _ in logs)
    startup = health.get_tracker().snapshot()["startup"]
    assert startup["state"] == "SETUP_REQUIRED"
    assert "尚未启用推送通道" in startup["reasons"]
    assert "尚未启用监控任务" in startup["reasons"]


@pytest.mark.asyncio
async def test_init_accounts_only_runs_for_referenced_monitors(monkeypatch):
    """账号池预设不应在 Message 未启用或未被监控项引用时触发刷新。"""
    monkeypatch.setattr(app.cfg, "MESSAGE_MONITOR_ENABLED", False)
    monkeypatch.setattr(app.cfg, "MONITOR_LIST", [])
    monkeypatch.setattr(app, "log_all", lambda *_args, **_kwargs: None)

    def should_not_read_token(*_args, **_kwargs):
        raise AssertionError("未启用 Message 时不应读取账号 Token")

    monkeypatch.setattr(app, "get_token_remaining_seconds", should_not_read_token)
    await app._init_accounts()


@pytest.mark.asyncio
async def test_incomplete_enabled_monitor_is_warning_not_error(monkeypatch):
    monkeypatch.setattr(app.cfg, "ENABLE_NAPCAT_QQ", False)
    monkeypatch.setattr(app.cfg, "ENABLE_QQ_OFFICIAL_BOT", False)
    monkeypatch.setattr(app.cfg, "ENABLE_TG_BOT", False)
    monkeypatch.setattr(app.cfg, "MESSAGE_MONITOR_ENABLED", True)
    monkeypatch.setattr(app.cfg, "MONITOR_LIST", [{
        "account_id": "demo", "m_id": "1", "m_name": "测试成员",
        "target_groups": [], "tg_chat_id": "",
    }])
    monkeypatch.setattr(app.cfg, "ACCOUNTS", {"demo": {"auth_method": "web"}})
    monkeypatch.setattr(app.cfg, "BLOG_MONITOR", {"enabled": False})
    monkeypatch.setattr(app.cfg, "PLATFORMS", {})
    monkeypatch.setattr(app.cfg, "QQ_BOT_API", "http://127.0.0.1:3000/send_group_msg")

    import config.credentials as credentials
    monkeypatch.setattr(credentials, "ACCOUNT_CREDS", {})
    logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(app, "log_all", lambda content, **kwargs: logs.append((str(content), kwargs)))
    health.initialize()

    result = await app._health_check(_NoCallClient())

    assert result is False
    assert not any(item.get("is_error") for _, item in logs)
    assert any(item.get("is_warning") and "凭证" in text for text, item in logs)
    assert health.get_tracker().snapshot()["startup"]["state"] == "SETUP_REQUIRED"


@pytest.mark.asyncio
async def test_enabled_channel_transport_failure_remains_error(monkeypatch):
    monkeypatch.setattr(app.cfg, "ENABLE_NAPCAT_QQ", True)
    monkeypatch.setattr(app.cfg, "ENABLE_QQ_OFFICIAL_BOT", False)
    monkeypatch.setattr(app.cfg, "ENABLE_TG_BOT", False)
    monkeypatch.setattr(app.cfg, "MESSAGE_MONITOR_ENABLED", False)
    monkeypatch.setattr(app.cfg, "MONITOR_LIST", [])
    monkeypatch.setattr(app.cfg, "BLOG_MONITOR", {"enabled": False})
    monkeypatch.setattr(app.cfg, "PLATFORMS", {})
    monkeypatch.setattr(app.cfg, "QQ_BOT_API", "http://127.0.0.1:3000/send_group_msg")

    class _FailingClient:
        async def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("simulated NapCat outage")

    logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(app, "log_all", lambda content, **kwargs: logs.append((str(content), kwargs)))
    health.initialize()

    result = await app._health_check(_FailingClient())

    assert result is False
    assert any(item.get("is_error") and "NapCat" in text for text, item in logs)
    assert health.get_tracker().snapshot()["startup"]["state"] == "DEGRADED"


def test_warning_log_is_not_written_to_error_stream(monkeypatch, capsys):
    """WARN 应进入系统日志/实时日志，而不是错误日志文件。"""
    class _Sink:
        def __init__(self):
            self.warning_calls: list[str] = []
            self.error_calls: list[str] = []
            self.info_calls: list[str] = []

        def warning(self, value):
            self.warning_calls.append(value)

        def error(self, value):
            self.error_calls.append(value)

        def info(self, value):
            self.info_calls.append(value)

        def debug(self, _value):
            pass

    system_sink = _Sink()
    error_sink = _Sink()
    monkeypatch.setattr(logger, "system_logger", system_sink)
    monkeypatch.setattr(logger, "error_logger", error_sink)

    logger.log_all("等待凭证配置", is_warning=True)

    output = capsys.readouterr().out
    assert "[WARN ]" in output
    assert system_sink.warning_calls == ["等待凭证配置"]
    assert not error_sink.error_calls


def test_health_snapshot_exposes_startup_state():
    health.initialize()
    health.get_tracker().set_startup_state("READY")
    snapshot = health.get_tracker().snapshot()
    assert snapshot["startup"]["state"] == "READY"
    assert snapshot["startup"]["reasons"] == []


def test_status_page_exposes_startup_state():
    html = (Path(__file__).resolve().parent.parent / "src" / "webui_static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="stStartup"' in html
    assert 'startup.state' in html
