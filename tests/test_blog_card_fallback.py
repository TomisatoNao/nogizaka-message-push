"""验证博客翻译失败/缺失时的卡片正文回退、图文顺序保持与通知降级机制。

对应方案：docs/BLOG_CARD_TRANSLATION_FALLBACK_PLAN_2026-10-28.md
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
import pytest

from src.blog_card_renderer import _generate_html, render_blog_card
from src.blog_content import (
    normalize_blog_content,
    parse_blog_html_to_blocks,
    validate_and_normalize_content_json,
)
from src.notifier import send_blog_post


def test_auto_post_translation_failure_preserves_text_and_order():
    """自动抓取的 post 翻译失败时（无 content_json，仅 body/body_html），卡片仍完整展示原文并保持图文穿插顺序。"""
    post = {
        "group_key": "sakurazaka",
        "author": "森田 ひかる",
        "title": "写真日記",
        "date": "2026-10-28 12:00",
        "body": "こんにちは、森田です。\n\n今日も頑張ります！",
        "body_html": "<p>こんにちは、森田です。</p><p><img src=\"/img/p1.jpg\"/></p><p>今日も頑張ります！</p>",
        "images": ["https://sakurazaka46.com/img/p1.jpg"],
        "image_paths": ["data/archive/images/p1.jpg"],
        "translation": "",
        "content_json": "",
        "translation_status": "failed",
        "translation_error": "timeout",
        "translation_model": "gemini-2.0-flash",
    }
    content = normalize_blog_content(post, mode="ja-zh")
    assert content.translation_state == "none"
    assert content.actual_mode == "ja-only"
    assert content.badge_text == "📖 原文 · 暂无译文"
    assert len(content.blocks) == 3
    assert [b["type"] for b in content.blocks] == ["text", "img", "text"]
    assert [b["jp"] for b in content.blocks if b["type"] == "text"] == [
        "こんにちは、森田です。",
        "今日も頑張ります！",
    ]

    html = _generate_html(post, ["data:image/jpeg;base64,mock1"])
    assert "こんにちは、森田です。" in html
    assert "今日も頑張ります！" in html
    assert "📖 原文 · 暂无译文" in html
    assert "AI 智能双语精读对照" not in html
    # 验证顺序：段落1 -> 图片 -> 段落2
    idx1 = html.index("こんにちは、森田です。")
    idx_img = html.index("data:image/jpeg;base64,mock1")
    idx2 = html.index("今日も頑張ります！")
    assert idx1 < idx_img < idx2


def test_five_images_interleaved_blog_case():
    """5 图混排案例：原文前、中、后标记均可见，图片恰好出现一次且顺序严格一致。"""
    body_html_parts = ["<p>开篇段落【MARKER_START】</p>"]
    for i in range(1, 6):
        body_html_parts.append(f'<p><img src="/img/pic_{i}.jpg"/></p>')
        body_html_parts.append(f"<p>第{i}张照片配文【MARKER_MID_{i}】</p>")
    body_html_parts.append("<p>结尾签名【MARKER_END】</p>")
    full_html = "".join(body_html_parts)

    post = {
        "group_key": "hinatazaka",
        "author": "金村 美玖",
        "title": "5枚の写真",
        "date": "2026-10-28 15:00",
        "body_html": full_html,
        "images": [f"https://www.hinatazaka46.com/img/pic_{i}.jpg" for i in range(1, 6)],
        "translation": "",
        "content_json": "",
        "translation_status": "failed",
    }

    mock_images = [f"data:image/jpeg;base64,pic_{i}" for i in range(1, 6)]
    html = _generate_html(post, mock_images)

    assert "【MARKER_START】" in html
    assert "【MARKER_END】" in html
    for i in range(1, 6):
        assert f"【MARKER_MID_{i}】" in html
        assert f"data:image/jpeg;base64,pic_{i}" in html

    # 检查顺序递增
    prev_idx = html.index("【MARKER_START】")
    for i in range(1, 6):
        img_pos = html.index(f"data:image/jpeg;base64,pic_{i}")
        text_pos = html.index(f"【MARKER_MID_{i}】")
        assert prev_idx < img_pos < text_pos
        prev_idx = text_pos
    assert prev_idx < html.index("【MARKER_END】")


def test_untrusted_html_and_script_injection_escaped():
    """输入不可信数据（XSS 脚本、恶意属性、特殊字符）时，离线卡片安全转义且不执行任意脚本。"""
    malicious_html = (
        '<p>正常文字<script>alert("xss")</script></p>'
        '<p><img src="javascript:alert(1)" onerror="alert(2)"/></p>'
        '<p>特殊字符: &lt;b&gt;bold&lt;/b&gt; & " \' </p>'
    )
    blocks = parse_blog_html_to_blocks(malicious_html, "nogizaka")
    # 验证 script 标签内容已被 BeautifulSoup 跳过
    assert not any("alert" in str(b.get("jp", "")) for b in blocks)

    post = {
        "group_key": "nogizaka",
        "author": "<script>alert(1)</script>",
        "title": '标题"with"quotes & <tags>',
        "body_html": malicious_html,
    }
    html = _generate_html(post, [])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html or "author-avatar-fallback" in html
    assert "alert(&quot;xss&quot;)" not in html


def test_malformed_content_json_safely_falls_back():
    """针对损坏/非预期的 content_json，不抛 AttributeError，优雅降级到原文并保留正文。"""
    base_post = {
        "group_key": "sakurazaka",
        "author": "森田 ひかる",
        "title": "テスト",
        "body_html": "<p>有效原文段落</p>",
        "translation": "",
    }

    # Case A: 非列表对象（字典）
    assert validate_and_normalize_content_json('{"some": "dict"}') is None
    dict_post = dict(base_post, content_json='{"some": "dict"}')
    html_a = _generate_html(dict_post, [])
    assert "有效原文段落" in html_a
    assert "📖 原文 · 暂无译文" in html_a

    # Case B: 包含非字典元素的列表
    assert validate_and_normalize_content_json('[1, "bad_element", null]') is None
    list_post = dict(base_post, content_json='[1, "bad_element", null]')
    html_b = _generate_html(list_post, [])
    assert "有效原文段落" in html_b

    # Case C: 语法错误的 JSON 字符串
    assert validate_and_normalize_content_json('{"unclosed') is None
    bad_json_post = dict(base_post, content_json='{"unclosed')
    html_c = _generate_html(bad_json_post, [])
    assert "有效原文段落" in html_c

    # Case D: 直接传入非字符串非列表对象
    assert validate_and_normalize_content_json(12345) is None
    invalid_obj_post = dict(base_post, content_json=12345)
    html_d = _generate_html(invalid_obj_post, [])
    assert "有效原文段落" in html_d


def test_translation_states_and_badges():
    """验证完整翻译、部分翻译、无翻译和纯图博客的徽标标识与状态归一。"""
    # 1. 完整翻译
    full_post = {
        "group_key": "nogizaka",
        "content_json": json.dumps([
            {"type": "text", "jp": "日文1", "zh": "中文1"},
            {"type": "text", "jp": "日文2", "zh": "中文2"},
        ]),
        "translation_model": "deepseek-chat",
    }
    c_full = normalize_blog_content(full_post, mode="ja-zh")
    assert c_full.translation_state == "full"
    assert "AI 智能双语精读对照 · deepseek-chat" in c_full.badge_text

    # 2. 部分翻译
    partial_post = {
        "group_key": "nogizaka",
        "content_json": json.dumps([
            {"type": "text", "jp": "日文1", "zh": "中文1"},
            {"type": "text", "jp": "日文2", "zh": ""},
        ]),
        "translation_model": "deepseek-chat",
    }
    c_partial = normalize_blog_content(partial_post, mode="ja-zh")
    assert c_partial.translation_state == "partial"
    assert "部分段落暂无译文" in c_partial.badge_text

    # 3. 纯图片博客
    img_post = {
        "group_key": "hinatazaka",
        "body_html": "<img src='https://example.com/1.jpg'><img src='https://example.com/2.jpg'>",
    }
    c_img = normalize_blog_content(img_post, mode="ja-zh")
    assert c_img.translation_state == "image_only"
    assert c_img.badge_text == "📷 原文写真"


def test_language_mode_zh_only_fallback_when_untranslated():
    """zh-only 模式在没有任何有效中文时，安全回退 ja-only 展示原文，不生成空正文卡片。"""
    post = {
        "group_key": "sakurazaka",
        "body_html": "<p>日本語の本文です。</p>",
        "translation": "",
        "content_json": "",
    }
    c = normalize_blog_content(post, mode="zh-only")
    assert c.actual_mode == "ja-only"
    assert c.badge_text == "📖 原文 · 暂无译文"

    html = _generate_html(post, [], mode="zh-only")
    assert "日本語の本文です。" in html
    assert "📖 原文 · 暂无译文" in html


def test_plain_text_fallback_when_html_absent():
    """缺少 body_html 时，从纯文本 body_text 或 body 字段按自然换行切分段落。"""
    post = {
        "group_key": "nogizaka",
        "author": "冨里 奈央",
        "title": "なおなお",
        "body": "初めまして！\n\n冨里奈央です。\n\nよろしくお願いします！",
        "images": ["https://example.com/1.jpg"],
    }
    content = normalize_blog_content(post)
    assert content.translation_state == "none"
    assert len(content.blocks) == 3
    assert [b["jp"] for b in content.blocks] == [
        "初めまして！",
        "冨里奈央です。",
        "よろしくお願いします！",
    ]


@pytest.mark.asyncio
async def test_notifier_traditional_push_fallback_has_body():
    """测试传统图文推送（text_and_images）在翻译失败时，正文依然提取到完整日文原文。"""
    from src.platforms.tgbot import TGBot

    post = {
        "group_key": "sakurazaka",
        "group_name": "櫻坂46",
        "author": "森田 ひかる",
        "title": "テスト",
        "date": "2026-10-28 12:00",
        "url": "https://sakurazaka46.com/blog/1",
        "body": "こんにちは！森田です。\n\nよろしくね。",
        "body_html": "<p>こんにちは！森田です。</p><p>よろしくね。</p>",
        "images": [],
        "translation": "",
        "content_json": "",
        "translation_status": "failed",
    }

    bot = TGBot(name="test", token="", target_chat="123", push_blog=True, blog_card_mode="text_and_images")
    bot._bot = AsyncMock()
    sent_bodies = []

    async def mock_send_translation_tg(pairs):
        sent_bodies.append(pairs)
        return True

    bot._post_message = AsyncMock(return_value=True)
    bot.send_translation_tg = AsyncMock(side_effect=mock_send_translation_tg)

    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", False), \
         patch("config.config.ENABLE_TG_BOT", True), \
         patch("src.platforms.tgbot.get_configured_bots", return_value=[bot]), \
         patch("src.blog_card_renderer.render_blog_card", new_callable=AsyncMock, return_value=None):
        success = await send_blog_post(post)
        assert success is True
        assert len(sent_bodies) == 1
        pairs = sent_bodies[0]
        # 验证提取的 pairs 中包含日文原文
        ja_texts = [p[0] for p in pairs if isinstance(p, tuple)]
        assert any("こんにちは！森田です。" in t for t in ja_texts)
        assert any("よろしくね。" in t for t in ja_texts)


@pytest.mark.asyncio
async def test_render_blog_card_exception_isolation():
    """渲染长图遇到未捕获异常时，安全返回 None 并降级，不中断后续操作。"""
    bad_post = {
        "group_key": "invalid_group",
        "author": "none",
        "title": "none",
    }
    with patch("src.blog_card_renderer._generate_html", side_effect=ValueError("mock render crash")):
        res = await render_blog_card(bad_post)
        assert res is None
