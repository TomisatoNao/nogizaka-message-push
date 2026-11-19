"""Blog delivery must propagate failures after a successful header."""
from unittest.mock import AsyncMock, patch

import pytest

from src.notifier import send_blog_post


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["card_only", "card_and_images", "text_and_images"])
@pytest.mark.parametrize("body_ok", [False, True])
async def test_napcat_blog_fallback_result(mode, body_ok):
    post = {"group_key": "nogizaka", "author": "author", "title": "title",
            "body": "original body", "url": "https://example.test/blog/1"}
    sender = AsyncMock(side_effect=[True, body_ok])
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_TG_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", True), \
         patch("config.config.NAPCAT_ROUTES", [{"group_id": "123", "push_blog": True,
                                                "blog_card_mode": mode}]), \
         patch("src.platforms.napcat.send_qq_message", sender), \
         patch("src.blog_card_renderer.render_blog_card", AsyncMock(return_value=None)), \
         patch("src.notifier.asyncio.sleep", AsyncMock()):
        assert await send_blog_post(post) is body_ok
    assert sender.await_count == 2
    assert "original body" in sender.await_args.args[1][0]["data"]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["card_only", "card_and_images", "text_and_images"])
async def test_telegram_blog_body_failure(mode):
    from src.platforms.tgbot import TGBot

    bot = TGBot(name="test", token="", target_chat="123", push_blog=True,
                blog_card_mode=mode)
    bot._post_message = AsyncMock(return_value=True)
    bot.send_translation_tg = AsyncMock(return_value=False)
    post = {"group_key": "nogizaka", "author": "author", "title": "title",
            "body": "original body", "url": "https://example.test/blog/1"}
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", False), \
         patch("config.config.ENABLE_TG_BOT", True), \
         patch("src.platforms.tgbot.get_configured_bots", return_value=[bot]), \
         patch("src.blog_card_renderer.render_blog_card", AsyncMock(return_value=None)), \
         patch("src.notifier.asyncio.sleep", AsyncMock()):
        assert await send_blog_post(post) is False
    bot.send_translation_tg.assert_awaited_once()
