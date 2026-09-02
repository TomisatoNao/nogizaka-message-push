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
