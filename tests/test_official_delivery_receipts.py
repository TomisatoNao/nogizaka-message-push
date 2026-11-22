"""Conservative official-QQ message receipts in durable member mode."""
from unittest.mock import AsyncMock

import httpx
import pytest

from src.delivery_receipts import durable_delivery_receipts
from src.platforms.qq_official import QQOfficialBot
from src.platforms.qq_official_client import QQOfficialClient


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("lost receipt"), httpx.Response(503, text="unavailable")])
async def test_unknown_visible_message_is_not_retried(failure):
    client = QQOfficialClient("", "", name="test")
    client._wait_rate_limit = AsyncMock()
    client._safe_post = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
    with durable_delivery_receipts():
        assert await client._post_json("https://example.test/v2/groups/target/messages", {}) is None
    assert client._safe_post.await_count == 1


@pytest.mark.asyncio
async def test_upload_metadata_keeps_existing_retry_policy(monkeypatch):
    monkeypatch.setattr("src.platforms.qq_official_client.asyncio.sleep", AsyncMock())
    client = QQOfficialClient("", "", name="test")
    client._wait_rate_limit = AsyncMock()
    response = httpx.Response(201, json={"file_info": "uploaded"})
    client._safe_post = AsyncMock(side_effect=[httpx.ReadTimeout("lost upload metadata"), response])
    with durable_delivery_receipts():
        assert await client._post_json("https://example.test/v2/groups/target/files", {}) is response
    assert client._safe_post.await_count == 2


@pytest.mark.asyncio
async def test_unknown_text_receipt_stops_media_tail():
    bot = QQOfficialBot("test", "", "", "target")
    bot.ensure_access_token = AsyncMock(return_value=True)
    bot._wait_rate_limit = AsyncMock()
    bot._safe_post = AsyncMock(side_effect=httpx.ReadTimeout("lost message receipt"))
    bot._upload_media = AsyncMock()
    with durable_delivery_receipts():
        assert await bot.send_message_chain({}, [{"type": "text", "data": {"text": "body"}}],
                                            media_payloads=[("image", b"media")]) is False
    assert bot._safe_post.await_count == 1
    bot._upload_media.assert_not_awaited()
