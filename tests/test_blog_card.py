"""验证博客长图卡片生成、优雅降级与通道路由推送机制

运行: python tests/test_blog_card.py
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import pytest
    _async_test = pytest.mark.asyncio
except ImportError:
    def _async_test(fn):
        return fn

from src.blog_card_renderer import render_blog_card, is_playwright_available, _generate_html
from src.notifier import send_blog_post
from src.platforms.tgbot import TGBot, media_caption_fits


@_async_test
async def test_blog_card_html_generation():
    mock_post = {
        "group_key": "nogizaka",
        "author": "冨里 奈央",
        "title": "テストブログ",
        "date": "2026-08-19 12:00",
        "translation": "<p><em>こんにちは</em><br/><span>你好</span></p>",
        "translation_model": "GLM-4-Flash",
        "image_paths": []
    }
    html = _generate_html(mock_post, [])
    assert "乃木坂46" in html
    assert "冨里 奈央" in html
    assert "テストブログ" in html
    assert "GLM-4-Flash" in html
    assert "author-avatar" in html
    assert 'class="footer-brand-icon"' in html
    assert "data:image/svg+xml;base64," in html
    assert "🌸 坂道联合监控系统" not in html

    # 测试未知作者优雅降级为文字头像
    mock_unknown = {
        "group_key": "nogizaka",
        "author": "未知成员999",
        "title": "テスト",
        "date": "2026-08-19 12:00",
        "translation": "<p><em>テスト</em></p>",
        "image_paths": []
    }
    html_unk = _generate_html(mock_unknown, [])
    assert "author-avatar-fallback" in html_unk
    assert "未" in html_unk


@_async_test
async def test_blog_card_render_execution():
    if not is_playwright_available():
        print("  ℹ️ Playwright 未安装，跳过真实无头浏览器渲染测试")
        return

    mock_post = {
        "group_key": "hinatazaka",
        "author": "金村 美玖",
        "title": "ユニットテスト用ブログ",
        "date": "2026-08-19 12:30",
        "translation": "<p><em>今日も一日頑張ろう！</em><br/><span>今天一天也要加油！</span></p>",
        "translation_model": "gemini-3.7-flash",
        "image_paths": []
    }
    img_path = await render_blog_card(mock_post)
    if img_path is None:
        print("  ℹ️ Playwright Chromium 浏览器内核未安装，跳过实际渲染图片断言")
        return
    assert img_path.exists()
    assert img_path.suffix == ".jpg"
    assert img_path.stat().st_size > 1000


@_async_test
async def test_notifier_card_only_routing():
    mock_post = {
        "group_key": "hinatazaka",
        "group_name": "日向坂46",
        "author": "鶴崎 仁香",
        "title": "繋いだ手、離さないでいて？",
        "date": "2026.8.19 21:38",
        "url": "https://www.hinatazaka46.com/s/official/diary/detail/70653",
        "images": ["https://example.com/1.jpg", "https://example.com/2.jpg"],
        "image_paths": [],
        "translation": "<p><em>こんにちは</em><br/><span>你好</span></p>",
        "translation_model": "gemini-2.5-flash-lite"
    }

    dummy_dir = Path("data/cache")
    dummy_dir.mkdir(parents=True, exist_ok=True)
    dummy_card = dummy_dir / "test_routing_card.jpg"
    dummy_card.write_bytes(b"FAKE_DATA")

    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", True),          patch("src.blog_card_renderer.render_blog_card", new_callable=AsyncMock) as mock_render,          patch("src.platforms.qq_official.get_configured_bots") as mock_get_bots:

        mock_render.return_value = dummy_card
        mock_bot = MagicMock()
        mock_bot.name = "bot_only"
        mock_bot.group_openid = "GRP1"
        mock_bot.target_openid = ""
        mock_bot.push_blog = True
        mock_bot.blog_filter = []
        mock_bot.blog_card_mode = "card_only"
        mock_bot.send_group_text = AsyncMock(return_value=True)
        mock_bot.send_media_file = AsyncMock(return_value=True)
        mock_bot.send_translation_qq = AsyncMock(return_value=True)
        mock_get_bots.return_value = [mock_bot]

        ok = await send_blog_post(mock_post)
        assert ok is True
        assert mock_bot.send_group_text.call_count == 1
        assert mock_bot.send_media_file.call_count == 1
        assert mock_bot.send_translation_qq.call_count == 0


@_async_test
async def test_tg_classic_blog_sends_header_with_first_album_then_body():
    post = {
        "group_key": "hinatazaka", "group_name": "日向坂46",
        "author": "大野 愛実", "title": "写真と近況", "date": "2026.9.29 16:49",
        "url": "https://example.com/blog/123", "images": [f"https://example.com/{i}.jpg" for i in range(12)],
        "translation": "本文",
    }
    bot = TGBot(name="test", token="", target_chat="123", push_blog=True,
                blog_card_mode="text_and_images")
    bot._bot = MagicMock()
    sent = []

    async def send_header(chat_id, text):
        sent.append(("header", text))
        return True

    async def send_album(**kwargs):
        sent.append(("photos", kwargs["media"]))
        return []

    async def send_body(pairs):
        sent.append(("body", pairs))
        return True

    bot._post_message = AsyncMock(side_effect=send_header)
    bot._bot.send_media_group = AsyncMock(side_effect=send_album)
    bot.send_translation_tg = AsyncMock(side_effect=send_body)
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", False), \
         patch("config.config.ENABLE_TG_BOT", True), \
         patch("src.platforms.tgbot.get_configured_bots", return_value=[bot]), \
         patch("src.blog_card_renderer.render_blog_card", new_callable=AsyncMock, return_value=None), \
         patch("src.notifier._extract_bilingual_pairs", return_value=[("原文", "译文")]):
        assert await send_blog_post(post) is True

    assert [item[0] for item in sent] == ["photos", "photos", "body"]
    assert [len(item[1]) for item in sent[:2]] == [10, 2]
    assert [media.media for item in sent[:2] for media in item[1]] == post["images"]
    assert "作者：大野 愛実" in sent[0][1][0].caption
    assert "https://example.com/blog/123" in sent[0][1][0].caption
    assert all(media.caption is None for media in sent[1][1])
    bot._post_message.assert_not_awaited()


@_async_test
async def test_tg_classic_blog_without_photos_keeps_header_before_body():
    post = {
        "group_key": "hinatazaka", "group_name": "日向坂46",
        "author": "大野 愛実", "title": "文字ブログ", "date": "2026.9.29",
        "url": "https://example.com/blog/123", "images": [], "translation": "本文",
    }
    bot = MagicMock()
    bot.target_chat = "123"
    bot.push_blog = True
    bot.blog_filter = []
    bot.blog_card_mode = "text_and_images"
    sent = []
    bot._post_message = AsyncMock(side_effect=lambda *_: sent.append("header") or True)
    bot.send_media_group_photos = AsyncMock()
    bot.send_translation_tg = AsyncMock(side_effect=lambda *_: sent.append("body") or True)
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", False), \
         patch("config.config.ENABLE_TG_BOT", True), \
         patch("src.platforms.tgbot.get_configured_bots", return_value=[bot]), \
         patch("src.blog_card_renderer.render_blog_card", new_callable=AsyncMock, return_value=None), \
         patch("src.notifier._extract_bilingual_pairs", return_value=[("原文", "译文")]):
        assert await send_blog_post(post) is True
    assert sent == ["header", "body"]
    bot.send_media_group_photos.assert_not_called()


@_async_test
async def test_tg_classic_blog_long_header_falls_back_to_separate_text():
    post = {
        "group_key": "hinatazaka", "group_name": "日向坂46",
        "author": "大野 愛実", "title": "标题" * 600, "date": "2026.9.29",
        "url": "https://example.com/blog/123",
        "images": ["https://example.com/1.jpg", "https://example.com/2.jpg"],
        "translation": "本文",
    }
    bot = MagicMock()
    bot.target_chat = "123"
    bot.push_blog = True
    bot.blog_filter = []
    bot.blog_card_mode = "text_and_images"
    sent = []
    bot._post_message = AsyncMock(side_effect=lambda *_: sent.append("header") or True)
    bot.send_media_group_photos = AsyncMock(
        side_effect=lambda *_args, **kwargs: sent.append(("photos", kwargs["caption"])) or True
    )
    bot.send_translation_tg = AsyncMock(side_effect=lambda *_: sent.append("body") or True)
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", False), \
         patch("config.config.ENABLE_TG_BOT", True), \
         patch("src.platforms.tgbot.get_configured_bots", return_value=[bot]), \
         patch("src.blog_card_renderer.render_blog_card", new_callable=AsyncMock, return_value=None), \
         patch("src.notifier._extract_bilingual_pairs", return_value=[("原文", "译文")]):
        assert await send_blog_post(post) is True
    assert sent == ["header", ("photos", ""), "body"]


@_async_test
async def test_tg_photo_batches_keep_caption_only_on_first_ten():
    photos = [f"https://example.com/{i}.jpg" for i in range(21)]
    caption = "作者：A & B <test>\n链接：https://example.com/blog"
    bot = TGBot(name="test", token="", target_chat="123")
    bot._bot = MagicMock()
    bot._bot.send_media_group = AsyncMock(return_value=[])
    bot._post_media = AsyncMock(return_value=True)
    assert media_caption_fits(caption)

    assert await bot.send_media_group_photos(photos, caption=caption) is True
    albums = [call.kwargs["media"] for call in bot._bot.send_media_group.await_args_list]
    assert [len(album) for album in albums] == [10, 10]
    assert albums[0][0].caption == "作者：A &amp; B &lt;test&gt;\n链接：https://example.com/blog"
    assert all(item.caption is None for album in albums for item in album if item is not albums[0][0])
    bot._post_media.assert_awaited_once_with("123", "image", photos[20], "")


@_async_test
async def test_tg_album_failure_falls_back_to_photos_with_first_caption():
    bot = TGBot(name="test", token="", target_chat="123")
    bot._bot = MagicMock()
    bot._send_with_retry = AsyncMock(return_value=False)
    bot._post_media = AsyncMock(return_value=True)
    photos = ["https://example.com/1.jpg", "https://example.com/2.jpg"]

    assert await bot.send_media_group_photos(photos, caption="博客通知") is True
    assert [call.args for call in bot._post_media.await_args_list] == [
        ("123", "image", photos[0], "博客通知"),
        ("123", "image", photos[1], ""),
    ]


@_async_test
async def test_compress_large_image():
    from PIL import Image
    import io
    import os
    from src.platforms.qq_official import _compress_image_if_needed

    # Create a noisy image that won't compress trivially
    img = Image.frombytes("RGB", (1600, 1600), os.urandom(1600 * 1600 * 3))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    large_bytes = buf.getvalue()
    
    assert len(large_bytes) > 2.0 * 1024 * 1024
    compressed = _compress_image_if_needed(large_bytes, max_bytes=int(1.5 * 1024 * 1024))
    assert len(compressed) <= int(1.5 * 1024 * 1024)
    assert len(compressed) < len(large_bytes)


def main():
    asyncio.run(test_blog_card_html_generation())
    asyncio.run(test_blog_card_render_execution())
    asyncio.run(test_notifier_card_only_routing())
    asyncio.run(test_compress_large_image())
    print("  ✓ test_blog_card 全部通过")


if __name__ == "__main__":
    main()
