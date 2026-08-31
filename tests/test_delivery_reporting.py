"""投递调度：部分失败必须可见，且不能中断其他通道。"""
import asyncio

import httpx

from src import health, notifier


class _OfficialBot:
    name = "official-test"
    remark = "官方测试"
    target_openid = "official-openid-123"
    group_openid = ""
    push_message = True
    push_alert = True
    member_filter: list[str] = []

    async def send_message_chain(self, *args, **kwargs) -> bool:
        return False

    async def send_text(self, text: str) -> bool:
        return False


class _TgBot:
    name = "tg-test"
    remark = "TG 测试"
    target_chat = "-100123"
    push_message = True
    push_alert = True
    member_filter: list[str] = []

    async def send_member_message(self, message_chain: list[dict]) -> bool:
        return True

    async def send_text(self, text: str) -> bool:
        return True


def _prepare_channels(monkeypatch):
    tracker = health.HealthTracker()
    tracker.initialize()
    monkeypatch.setattr(notifier.health, "get_tracker", lambda: tracker)
    monkeypatch.setattr(notifier.cfg, "ENABLE_NAPCAT_QQ", True)
    monkeypatch.setattr(notifier.cfg, "ENABLE_QQ_OFFICIAL_BOT", True)
    monkeypatch.setattr(notifier.cfg, "ENABLE_TG_BOT", True)
    monkeypatch.setattr(
        notifier.cfg,
        "NAPCAT_ROUTES",
        [{"group_id": 123, "remark": "NapCat 测试", "push_message": True, "push_alert": True}],
    )
    official = _OfficialBot()
    telegram = _TgBot()
    monkeypatch.setattr(notifier, "get_configured_bots", lambda: [official])
    monkeypatch.setattr(notifier.qq_official, "download_media_payloads", _empty_media)
    monkeypatch.setattr(notifier.tgbot, "get_configured_bots", lambda: [telegram])
    return tracker


async def _empty_media(member: dict, message_chain: list[dict]) -> list[tuple[str, bytes | None]]:
    return []


def test_member_delivery_keeps_partial_success_and_classifies_exception(monkeypatch) -> None:
    tracker = _prepare_channels(monkeypatch)

    async def napcat_timeout(group_id: int, message_chain: list[dict]) -> bool:
        raise httpx.ReadTimeout("socket timed out")

    monkeypatch.setattr(notifier, "send_qq_message", napcat_timeout)

    report = asyncio.run(notifier.send_member_message_detailed(
        {"m_name": "测试成员", "m_id": "1"}, [{"type": "text", "data": {"text": "hello"}}]
    ))

    assert report.ok is True
    assert report.partial is True
    assert report.success_count == 1
    assert report.failure_count == 2
    assert {a.error_code for a in report.attempts if not a.ok} == {"timeout", "delivery_failed"}
    assert tracker.snapshot()["channels"]["napcat"]["last_error"] == "timeout"
    assert tracker.snapshot()["channels"]["tg:tg-test"]["healthy"] is True


def test_alert_delivery_returns_success_when_only_one_route_succeeds(monkeypatch) -> None:
    _prepare_channels(monkeypatch)

    async def napcat_down(group_id: int, message_chain: list[dict]) -> bool:
        return False

    monkeypatch.setattr(notifier, "send_qq_message", napcat_down)
    # enabled_channels 需要官方 Bot 的存在；此测试只验证调度结果，不依赖其初始化状态。
    monkeypatch.setattr(notifier, "has_bots", lambda: True)

    assert asyncio.run(notifier.send_alert_message(0, "test alert")) is True


def test_official_media_failure_does_not_block_telegram(monkeypatch) -> None:
    _prepare_channels(monkeypatch)
    monkeypatch.setattr(notifier.cfg, "ENABLE_NAPCAT_QQ", False)

    async def media_down(member: dict, message_chain: list[dict]) -> list[tuple[str, bytes | None]]:
        raise httpx.ConnectError("upstream unavailable")

    monkeypatch.setattr(notifier.qq_official, "download_media_payloads", media_down)
    report = asyncio.run(notifier.send_member_message_detailed(
        {"m_name": "测试成员", "m_id": "1"}, [{"type": "image", "data": {"file": "https://x/y.jpg"}}]
    ))

    assert report.ok is True
    official = next(a for a in report.attempts if a.channel.startswith("official:"))
    assert official.error_code == "media_prepare_failed"
