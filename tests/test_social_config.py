"""社交投递配置依赖注入回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import config.config as cfg
from src.social.delivery import SocialDeliveryDispatcher
from src.social.models import Post
from src.social.settings import RuntimeConfig


def test_runtime_config_prefers_channels_over_global_flags(monkeypatch):
    monkeypatch.setattr(cfg, "ENABLE_TG_BOT", True)
    monkeypatch.setattr(cfg, "ENABLE_NAPCAT_QQ", True)
    monkeypatch.setattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", True)

    view = RuntimeConfig(
        {
            "channels": {"tg": False, "napcat": "off", "qq_official": 0},
        }
    )

    assert view.channel_enabled("tg") is False
    assert view.channel_enabled("napcat") is False
    assert view.channel_enabled("qq_official") is False
    assert view.enabled("telegram") is False


def test_runtime_config_supports_normalized_keys_and_hot_reload():
    raw = {
        "enable_tg_bot": "true",
        "enable_napcat_qq": False,
        "enable_qq_official_bot": 1,
        "napcat_routes": [{"group_id": "injected"}],
    }
    view = RuntimeConfig(raw)

    assert view.channel_enabled("tg") is True
    assert view.channel_enabled("napcat") is False
    assert view.channel_enabled("qq_official") is True
    assert view.list("NAPCAT_ROUTES") == [{"group_id": "injected"}]

    # manager 热重载是原位更新，视图不应缓存旧值。
    raw["enable_tg_bot"] = False
    raw["napcat_routes"] = [{"group_id": "reloaded"}]
    assert view.channel_enabled("tg") is False
    assert view.list("NAPCAT_ROUTES") == [{"group_id": "reloaded"}]


@pytest.mark.asyncio
async def test_dispatcher_uses_injected_routes_and_flag(monkeypatch):
    """临时/WebUI 配置应隔离全局 cfg 的路由与开关。"""
    monkeypatch.setattr(cfg, "ENABLE_NAPCAT_QQ", False)
    monkeypatch.setattr(
        cfg,
        "NAPCAT_ROUTES",
        [{"group_id": "global", "push_instagram": True}],
    )

    calls: list[str] = []

    async def _send(group_id, _chain):
        calls.append(str(group_id))
        return True

    monkeypatch.setattr("src.social.delivery.napcat.send_qq_message", _send)
    dispatcher = SocialDeliveryDispatcher(
        config={
            "channels": {"napcat": True},
            "napcat_routes": [{"group_id": "injected", "push_instagram": True}],
        }
    )
    post = Post(
        platform="instagram",
        post_id="injected-route",
        author="test_account",
        extra={"account": "test_account"},
    )

    result = dispatcher.broadcast(post, "hello")

    assert result["matched_routes"] == 1
    assert result["results"] == (True,)
    assert calls == ["injected"]


def test_forwarder_reuses_same_runtime_view_for_recording_path(monkeypatch):
    from src.social.forwarder import SocialForwarder

    monkeypatch.setattr(cfg, "ENABLE_TG_BOT", True)
    forwarder = SocialForwarder(
        {"channels": {"tg": False}},
    )

    assert forwarder._runtime is forwarder._dispatcher.runtime_config
    assert forwarder._runtime.channel_enabled("tg") is False

    # 录制通知路径使用同一配置视图；这里仅验证禁用配置不会枚举/调用 Bot。
    calls: list[str] = []
    monkeypatch.setattr(
        "src.social.forwarder.tgbot.get_configured_bots",
        lambda: calls.append("tg") or [],
    )
    result = SimpleNamespace(
        delivery_succeeded=False,
        display_name="测试成员",
        start_str="",
        end_str="",
        duration_str="",
        size_str="",
        output_dir="",
        parts=[],
        note="",
    )

    forwarder.send_recording(result)

    assert result.delivery_succeeded is True
    assert calls == []
