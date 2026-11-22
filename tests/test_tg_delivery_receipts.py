"""Telegram uncertainty must not become a confirmed durable receipt."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError, RetryAfter, TimedOut

from src.platforms.tgbot import TGBot, durable_delivery_receipts


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimedOut(), NetworkError("response lost")])
async def test_durable_member_stops_after_unknown_first_media(error):
    bot = TGBot("test", "", "123")
    bot._bot = SimpleNamespace(send_photo=AsyncMock(side_effect=error))
    chain = [{"type": "image", "data": {"file": url}}
             for url in ["https://example.test/1.jpg", "https://example.test/2.jpg"]]
    with durable_delivery_receipts():
        assert await bot.send_member_message(chain) is False
    assert bot._bot.send_photo.await_count == 1


@pytest.mark.asyncio
async def test_receipt_policy_is_task_local_and_legacy_behavior_is_preserved():
    bot = TGBot("test", "", "123")

    async def invoke(strict):
        with durable_delivery_receipts(strict):
            async def send():
                await asyncio.sleep(0)
                raise TimedOut()
            return await bot._send_with_retry("test", send)

    assert await asyncio.gather(invoke(True), invoke(False)) == [False, True]
    assert await bot._send_with_retry("legacy", AsyncMock(side_effect=TimedOut())) is True


@pytest.mark.asyncio
async def test_explicit_flood_rejection_can_retry_safely(monkeypatch):
    monkeypatch.setattr("src.platforms.tgbot.asyncio.sleep", AsyncMock())
    bot = TGBot("test", "", "123")
    send = AsyncMock(side_effect=[RetryAfter(0), None])
    with durable_delivery_receipts():
        assert await bot._send_with_retry("test", send) is True
    assert send.await_count == 2
