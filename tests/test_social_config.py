"""社交投递配置依赖注入回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.config.config as cfg
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

def test_platform_settings_supports_direct_platform_dict():
    from src.social.settings import platform_settings
    direct_x = {
        "enabled": True,
        "accounts": ["test_acc"],
        "backends": ["nitter"],
    }
    merged = platform_settings(direct_x, "x")
    assert merged["enabled"] is True
    assert merged["accounts"] == ["test_acc"]
    assert "https://nitter.perennialte.ch" in merged["nitter_instances"]


def test_member_bound_instagram_account_joins_manual_monitor_list():
    from src.social.fetchers.instagram_fetcher import InstagramFetcher

    fetcher = InstagramFetcher({
        "platforms": {"instagram": {"enabled": True, "accounts": ["manual", "shared"]}},
        "monitor": [
            {"name": "成员 A", "social": {"instagram": [" @shared"]}},
        ],
    })

    assert fetcher.accounts == ["manual", "shared"]
    assert fetcher.member_name("shared") == "成员 A"
    assert fetcher.display_name("shared") == "成员 A (shared)"


def test_member_tiktok_binding_is_shared_with_live_and_legacy_live_is_kept():
    from src.social.fetchers.tiktok_fetcher import TikTokFetcher
    from src.social.fetchers.tiktok_live_fetcher import TikTokLiveFetcher

    config = {
        "platforms": {
            "tiktok": {"accounts": ["manual_video"]},
            "tiktok_live": {"accounts": ["manual_live"]},
        },
        "monitor": [
            {"name": "成员 A", "social": {"tiktok": ["shared_user"]}},
            {"name": "成员 B", "social": {"tiktok_live": ["legacy_live"]}},
        ],
    }
    video = TikTokFetcher(config)
    live = TikTokLiveFetcher(config)
    assert video.accounts == ["manual_video", "shared_user"]
    assert live.accounts == ["manual_live", "shared_user", "legacy_live"]
    assert live.member_name("shared_user") == "成员 A"
    assert live.member_name("legacy_live") == "成员 B"


def test_ig_session_check_session_fallback_proxy(monkeypatch):
    from src.social import ig_session
    monkeypatch.setattr(cfg, "PROXY", "http://127.0.0.1:9999")
    called_proxies = []

    def fake_get(url, **kwargs):
        called_proxies.append(kwargs.get("proxies"))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"form_data": {"username": "test_user"}}
        )

    monkeypatch.setattr("requests.get", fake_get)
    import sys
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)

    res = ig_session.check_session({"sessionid": "mock_id"}, proxy="")
    assert res["valid"] is True
    assert res["username"] == "test_user"
    assert called_proxies and called_proxies[0]["http"] == "http://127.0.0.1:9999"


def test_x_fetcher_failure_warning_rate_limiting(monkeypatch, caplog):
    import logging
    from src.social.fetchers.x_fetcher import TimelineUnavailable, XFetcher

    fetcher = XFetcher({
        "platforms": {
            "x": {
                "enabled": True,
                "accounts": ["test_fail_acc"],
                "backends": ["syndication"],
            }
        }
    })

    def fake_syndication(account):
        raise RuntimeError("Network timeout")

    fetcher._backend_syndication = fake_syndication

    with caplog.at_level(logging.WARNING):
        # 第一次全后端失败应有 WARNING 告警
        with pytest.raises(TimelineUnavailable):
            fetcher._fetch_timeline("test_fail_acc")
        assert any("时间线抓取失败" in record.message for record in caplog.records)

        # 紧接着第二次应受频控抑制，不重复刷屏
        caplog.clear()
        with pytest.raises(TimelineUnavailable):
            fetcher._fetch_timeline("test_fail_acc")
        assert not any("时间线抓取失败" in record.message for record in caplog.records)


def test_x_unconfigured_api_is_not_reported_as_attempted(monkeypatch, caplog):
    import logging
    from src.social.fetchers.x_fetcher import TimelineUnavailable, XFetcher

    fetcher = XFetcher({"platforms": {"x": {"enabled": True,
                                              "accounts": ["demo"],
                                              "backends": ["syndication", "apiv2"]}}})
    monkeypatch.setattr(fetcher, "_backend_syndication",
                        lambda _account: (_ for _ in ()).throw(RuntimeError("HTTP 451")))
    with caplog.at_level(logging.WARNING), pytest.raises(TimelineUnavailable):
        fetcher._fetch_timeline("demo")
    assert "实际尝试: syndication" in caplog.text
    assert "API v2 未配置 Bearer Token" in caplog.text


def test_x_valid_empty_timeline_is_not_an_outage(monkeypatch):
    from src.social.fetchers.x_fetcher import XFetcher

    fetcher = XFetcher({"platforms": {"x": {"enabled": True,
                                              "accounts": ["demo"],
                                              "backends": ["syndication"]}}})
    monkeypatch.setattr(fetcher, "_backend_syndication", lambda _account: [])
    assert fetcher.fetch() == []
    assert fetcher.discovery_status()["demo"]["state"] == "ok"


def test_x_partial_failure_keeps_healthy_account_and_backoff(monkeypatch):
    from src.social.fetchers.x_fetcher import TimelineUnavailable, XFetcher
    from src.social.models import Post

    fetcher = XFetcher({"platforms": {"x": {"enabled": True,
                                              "accounts": ["bad", "good"]}}})
    calls = []

    def fake_fetch(account):
        calls.append(account)
        if account == "bad":
            raise TimelineUnavailable("HTTP 451")
        return [Post(platform="x", post_id="x_1", author="good", text="hello")]

    monkeypatch.setattr(fetcher, "_fetch_account", fake_fetch)
    assert len(fetcher.fetch()) == 1
    assert fetcher.discovery_status()["bad"]["state"] == "failed"
    assert fetcher.discovery_status()["good"]["state"] == "ok"
    assert len(fetcher.fetch()) == 1
    assert calls.count("bad") == 1  # failed account cools down; healthy one continues


def test_x_all_failed_is_not_no_new_content(monkeypatch):
    from src.social.fetchers.x_fetcher import TimelineUnavailable, XFetcher

    fetcher = XFetcher({"platforms": {"x": {"enabled": True, "accounts": ["bad"]}}})
    monkeypatch.setattr(fetcher, "_fetch_account",
                        lambda _account: (_ for _ in ()).throw(TimelineUnavailable("HTTP 451")))
    with pytest.raises(TimelineUnavailable):
        fetcher.fetch()
    assert fetcher.discovery_status()["bad"]["consecutive_failures"] == 1


def test_x_recovery_marks_possible_history_gap(monkeypatch):
    import time
    from src.social.fetchers.x_fetcher import XFetcher, _RawTweet

    fetcher = XFetcher({"platforms": {"x": {"enabled": True, "accounts": ["demo"]}}})
    last_success = time.time() - 3600
    fetcher._account_health["demo"] = {
        "state": "failed", "last_success_at": last_success,
        "next_retry_at": 0,
    }
    tweets = [
        _RawTweet(tweet_id=str(i), text="post", created_ts=last_success + i + 1,
                  author="demo", screen_name="demo")
        for i in range(20)
    ]
    monkeypatch.setattr(fetcher, "_fetch_timeline", lambda _account: tweets)
    monkeypatch.setattr(fetcher, "_bootstrap_guard", lambda *_args: False)
    monkeypatch.setattr(fetcher, "is_sent", lambda _post_id: True)

    assert fetcher.fetch() == []
    assert fetcher.discovery_status()["demo"]["gap_risk"] is True
