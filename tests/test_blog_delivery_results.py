"""Blog delivery must propagate failures after a successful header."""
from unittest.mock import AsyncMock, patch

import pytest

from src.notifier import send_blog_post


@pytest.fixture(autouse=True)
def _isolate_blog_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_blogs.db"
    monkeypatch.setattr("src.blog_fetcher.BLOG_DB_PATH", test_db)


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


@pytest.mark.asyncio
async def test_blog_partial_route_success_is_not_complete():
    sender = AsyncMock(side_effect=[True, True, False])
    post = {"group_key": "nogizaka", "body": "original body"}
    routes = [{"group_id": str(i), "push_blog": True,
               "blog_card_mode": "text_and_images"} for i in (123, 456)]
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_TG_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", True), \
         patch("config.config.NAPCAT_ROUTES", routes), \
         patch("src.platforms.napcat.send_qq_message", sender), \
         patch("src.blog_card_renderer.render_blog_card", AsyncMock(return_value=None)), \
         patch("src.notifier.asyncio.sleep", AsyncMock()):
        assert await send_blog_post(post) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["card_only", "card_and_images", "text_and_images"])
async def test_official_blog_body_failure(mode):
    from src.platforms.qq_official import QQOfficialBot

    bot = QQOfficialBot(name="test", app_id="", client_secret="", target_openid="target",
                        push_blog=True, blog_card_mode=mode)
    bot.send_private_text = AsyncMock(return_value=True)
    bot.send_translation_qq = AsyncMock(return_value=False)
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", True), \
         patch("config.config.ENABLE_NAPCAT_QQ", False), \
         patch("config.config.ENABLE_TG_BOT", False), \
         patch("src.platforms.qq_official.get_configured_bots", return_value=[bot]), \
         patch("src.blog_card_renderer.render_blog_card", AsyncMock(return_value=None)), \
         patch("src.notifier.asyncio.sleep", AsyncMock()):
        assert await send_blog_post({"group_key": "nogizaka", "body": "body"}) is False
    bot.send_translation_qq.assert_awaited_once()
