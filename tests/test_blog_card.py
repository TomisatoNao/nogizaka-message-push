import pytest
from pathlib import Path
from src.blog_card_renderer import render_blog_card, is_playwright_available, _generate_html

@pytest.mark.asyncio
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

@pytest.mark.asyncio
async def test_blog_card_render_execution():
    if not is_playwright_available():
        pytest.skip("Playwright not installed in environment")

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
    assert img_path is not None
    assert img_path.exists()
    assert img_path.suffix == ".jpg"
    assert img_path.stat().st_size > 1000
