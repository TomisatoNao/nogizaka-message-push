"""Tests for blog delivery receipts and breakpoint resume (A3)."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.app_modules.blog_worker import BlogWorker
from src.delivery_receipts import durable_delivery_receipts
from src.blog_fetcher import (
    get_blog_delivery_receipts,
    init_blog_db,
)
from src.notifier import send_blog_post


@pytest.fixture(autouse=True)
def _isolate_blog_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_blogs.db"
    monkeypatch.setattr("src.blog_fetcher.BLOG_DB_PATH", test_db)


@pytest.fixture(autouse=True)
def _enable_durable_blog_receipts():
    with durable_delivery_receipts():
        yield


@pytest.mark.asyncio
async def test_blog_receipts_recorded_on_success():
    blog_url = "https://example.test/blog/100"
    post = {
        "group_key": "nogizaka",
        "author": "贺喜遥香",
        "title": "测试博客",
        "body": "正文内容",
        "url": blog_url,
    }
    sender = AsyncMock(return_value=True)
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_TG_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", True), \
         patch("config.config.NAPCAT_ROUTES", [{"group_id": "123", "push_blog": True, "blog_card_mode": "card_only"}]), \
         patch("src.platforms.napcat.send_qq_message", sender), \
         patch("src.blog_card_renderer.render_blog_card", AsyncMock(return_value=None)), \
         patch("src.notifier.asyncio.sleep", AsyncMock()):
        ok = await send_blog_post(post)
        assert ok is True

    receipts = get_blog_delivery_receipts(blog_url, "napcat:123")
    assert "header" in receipts
    assert "card_or_body" in receipts


@pytest.mark.asyncio
async def test_blog_breakpoint_resume_skips_delivered_segments():
    blog_url = "https://example.test/blog/101"
    post = {
        "group_key": "nogizaka",
        "author": "井上和",
        "title": "带图写真博客",
        "body": "正文内容",
        "url": blog_url,
        "images": ["https://example.test/img1.jpg", "https://example.test/img2.jpg"],
    }
    # Round 1: header ok, card_or_body ok, images fail
    # Calls: 1 = header, 2 = body_txt, 3 = raw_chain (fail)
    sender_r1 = AsyncMock(side_effect=[True, True, False])
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_TG_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", True), \
         patch("config.config.NAPCAT_ROUTES", [{"group_id": "123", "push_blog": True, "blog_card_mode": "text_and_images"}]), \
         patch("src.platforms.napcat.send_qq_message", sender_r1), \
         patch("src.blog_card_renderer.render_blog_card", AsyncMock(return_value=None)), \
         patch("src.notifier.asyncio.sleep", AsyncMock()):
        ok1 = await send_blog_post(post)
        assert ok1 is False

    assert sender_r1.await_count == 3
    receipts_r1 = get_blog_delivery_receipts(blog_url, "napcat:123")
    assert "header" in receipts_r1
    assert "card_or_body" in receipts_r1
    assert "images" not in receipts_r1

    # Round 2: retry. Header and card_or_body should be SKIPPED!
    # Only images should be sent.
    sender_r2 = AsyncMock(return_value=True)
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_TG_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", True), \
         patch("config.config.NAPCAT_ROUTES", [{"group_id": "123", "push_blog": True, "blog_card_mode": "text_and_images"}]), \
         patch("src.platforms.napcat.send_qq_message", sender_r2), \
         patch("src.blog_card_renderer.render_blog_card", AsyncMock(return_value=None)), \
         patch("src.notifier.asyncio.sleep", AsyncMock()):
        ok2 = await send_blog_post(post)
        assert ok2 is True

    # Sender in Round 2 should only be called ONCE (for images)!
    assert sender_r2.await_count == 1
    call_args = sender_r2.await_args.args[1]
    assert call_args[0]["type"] == "image"

    receipts_r2 = get_blog_delivery_receipts(blog_url, "napcat:123")
    assert "images" in receipts_r2


@pytest.mark.asyncio
async def test_blog_multi_route_independent_resume():
    blog_url = "https://example.test/blog/102"
    post = {
        "group_key": "nogizaka",
        "author": "远藤樱",
        "title": "双群推送",
        "body": "正文",
        "url": blog_url,
        "images": ["https://example.test/img1.jpg"],
    }
    routes = [
        {"group_id": "1001", "push_blog": True, "blog_card_mode": "card_only"},
        {"group_id": "1002", "push_blog": True, "blog_card_mode": "card_only"},
    ]

    sent_calls = []

    async def mock_send(gid, msg):
        sent_calls.append((gid, msg))
        if gid == 1002 and len([c for c in sent_calls if c[0] == 1002]) == 2:
            return False  # group 1002 fails on 2nd message (body)
        return True

    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_TG_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", True), \
         patch("config.config.NAPCAT_ROUTES", routes), \
         patch("src.platforms.napcat.send_qq_message", mock_send), \
         patch("src.blog_card_renderer.render_blog_card", AsyncMock(return_value=None)), \
         patch("src.notifier.asyncio.sleep", AsyncMock()):
        ok1 = await send_blog_post(post)
        assert ok1 is False

    # Group 1001 should be completely delivered
    assert "header" in get_blog_delivery_receipts(blog_url, "napcat:1001")
    assert "card_or_body" in get_blog_delivery_receipts(blog_url, "napcat:1001")

    # Group 1002 should have header delivered but not body
    assert "header" in get_blog_delivery_receipts(blog_url, "napcat:1002")
    assert "card_or_body" not in get_blog_delivery_receipts(blog_url, "napcat:1002")

    # Round 2: retry. Group 1001 must NOT receive anything. Group 1002 only receives body!
    sent_calls.clear()
    with patch("config.config.ENABLE_QQ_OFFICIAL_BOT", False), \
         patch("config.config.ENABLE_TG_BOT", False), \
         patch("config.config.ENABLE_NAPCAT_QQ", True), \
         patch("config.config.NAPCAT_ROUTES", routes), \
         patch("src.platforms.napcat.send_qq_message", mock_send), \
         patch("src.blog_card_renderer.render_blog_card", AsyncMock(return_value=None)), \
         patch("src.notifier.asyncio.sleep", AsyncMock()):
        ok2 = await send_blog_post(post)
        assert ok2 is True

    # 1001 received 0 messages in round 2!
    assert [c for c in sent_calls if c[0] == 1001] == []
    # 1002 received only 1 message (the missing body)!
    assert len([c for c in sent_calls if c[0] == 1002]) == 1


@pytest.mark.asyncio
async def test_blog_worker_auto_retry_and_heal():
    db = init_blog_db()
    blog_url = "https://example.test/blog/103"
    post = {"url": blog_url, "title": "待补发博文", "group_key": "nogizaka"}

    cycle_runner = AsyncMock(side_effect=[[post], []])
    send_mock = AsyncMock(side_effect=[False, True])

    worker = BlogWorker(
        client=None,
        db=db,
        config_getter=lambda: {"blog_monitor": {"enabled": True}, "blog_records": {}},
        stop_event=asyncio.Event(),
        cycle_runner=cycle_runner,
        send_post=send_mock,
    )

    # Cycle 1: new post fetched, but send fails -> saved in worker's pending
    await worker._run_cycle(worker.config)
    assert len(worker._pending_posts) == 1
    assert worker._pending_posts[0]["url"] == blog_url

    # Cycle 2: no new post, but worker retries pending post -> succeeds -> removed
    await worker._run_cycle(worker.config)
    assert len(worker._pending_posts) == 0
    assert send_mock.await_count == 2
