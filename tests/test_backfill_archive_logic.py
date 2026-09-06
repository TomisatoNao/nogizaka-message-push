"""Unit tests for intelligent start date, cursor logic, and bounded retries in tools/backfill_archive.py."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock
import pytest

from tools.backfill_archive import (
    DEFAULT_START,
    APP_LAUNCH_DATES,
    AdaptivePacer,
    get_member_earliest_date,
    backfill_member,
)


class TestBackfillArchiveLogic(unittest.TestCase):

    def test_get_member_earliest_date_explicit(self):
        # 5期生 冨里 奈央
        m1 = {"m_name": "冨里 奈央", "group_type": "nogizaka"}
        self.assertEqual(get_member_earliest_date(m1), "2022-10-03T00:00:00Z")

        # 樱坂3期生 的野 美青
        m2 = {"m_name": "的野 美青", "group_type": "sakurazaka"}
        self.assertEqual(get_member_earliest_date(m2), "2023-03-01T00:00:00Z")

        # 日向坂4期生 正源司 陽子
        m3 = {"m_name": "正源司 陽子", "group_type": "hinatazaka"}
        self.assertEqual(get_member_earliest_date(m3), "2023-03-01T00:00:00Z")

    def test_get_member_earliest_date_normalized(self):
        # Name without space
        m1 = {"m_name": "冨里奈央", "group_type": "nogizaka"}
        self.assertEqual(get_member_earliest_date(m1), "2022-10-03T00:00:00Z")

        # Name with fullwidth space
        m2 = {"m_name": "井上　和", "group_type": "nogizaka"}
        self.assertEqual(get_member_earliest_date(m2), "2022-10-03T00:00:00Z")

    def test_get_member_earliest_date_group_fallback(self):
        # Unknown Nogizaka member -> nogizaka app launch date
        m_nogi = {"m_name": "未知成员", "group_type": "nogizaka"}
        self.assertEqual(get_member_earliest_date(m_nogi), APP_LAUNCH_DATES["nogizaka"])

        # Unknown Hinatazaka member -> hinatazaka app launch date
        m_hina = {"m_name": "未知日向成员", "group_type": "hinatazaka"}
        self.assertEqual(get_member_earliest_date(m_hina), APP_LAUNCH_DATES["hinatazaka"])

        # Unknown group -> DEFAULT_START
        m_unk = {"m_name": "神秘人", "group_type": "other_group"}
        self.assertEqual(get_member_earliest_date(m_unk), DEFAULT_START)

    def test_adaptive_pacer(self):
        pacer = AdaptivePacer(base=1.5, floor=0.8, ceil=90.0)
        # on_success decreases delay
        pacer.on_success()
        self.assertLess(pacer.delay, 1.5)
        self.assertGreaterEqual(pacer.delay, 0.8)

        # on_error increases delay
        pacer.on_error(rate_limited=False)
        self.assertGreater(pacer.delay, 1.5)

        # on_error with rate_limited triples delay
        prev = pacer.delay
        pacer.on_error(rate_limited=True)
        self.assertGreater(pacer.delay, prev)


@pytest.mark.asyncio
async def test_backfill_member_fatal_http_status(monkeypatch):
    monkeypatch.setattr("tools.backfill_archive.validate_account_cred", lambda *_: (True, "OK"))
    monkeypatch.setattr("tools.backfill_archive.archive.load_archived_ids", lambda *_: (set(), set()))
    monkeypatch.setattr("tools.backfill_archive.proactive_refresh_if_expiring", AsyncMock(return_value=True))

    client = AsyncMock()
    # past_messages 404, timeline 404
    resp = MagicMock()
    resp.status_code = 404
    resp.text = "Not Found"
    client.get.return_value = resp

    member = {"account_id": "acc1", "m_id": "123", "m_name": "测试", "group_type": "nogizaka"}
    progress = {}

    await backfill_member(client, member, DEFAULT_START, progress, reset_cursor=False)
    # Timeline loop should terminate on first 404
    # 1 call for past_messages, 1 call for timeline
    assert client.get.call_count == 2


@pytest.mark.asyncio
async def test_backfill_member_bounded_net_retries(monkeypatch):
    monkeypatch.setattr("tools.backfill_archive.validate_account_cred", lambda *_: (True, "OK"))
    monkeypatch.setattr("tools.backfill_archive.archive.load_archived_ids", lambda *_: (set(), set()))
    monkeypatch.setattr("tools.backfill_archive.proactive_refresh_if_expiring", AsyncMock(return_value=True))
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    client = AsyncMock()
    import httpx
    # past_messages fails, timeline fails with NetworkError repeatedly
    client.get.side_effect = httpx.RequestError("Network drop")

    member = {"account_id": "acc1", "m_id": "123", "m_name": "测试", "group_type": "nogizaka"}
    progress = {}

    await backfill_member(client, member, DEFAULT_START, progress, reset_cursor=False)
    # 1 call for past_messages + (1 initial + 3 retries = 4) calls for timeline
    # Total calls should be bounded (<= 5)
    assert client.get.call_count <= 5


@pytest.mark.asyncio
async def test_backfill_member_bounded_429_retries(monkeypatch):
    monkeypatch.setattr("tools.backfill_archive.validate_account_cred", lambda *_: (True, "OK"))
    monkeypatch.setattr("tools.backfill_archive.archive.load_archived_ids", lambda *_: (set(), set()))
    monkeypatch.setattr("tools.backfill_archive.proactive_refresh_if_expiring", AsyncMock(return_value=True))
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    client = AsyncMock()
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "1"}
    client.get.return_value = resp_429

    member = {"account_id": "acc1", "m_id": "123", "m_name": "测试", "group_type": "nogizaka"}
    progress = {}

    await backfill_member(client, member, DEFAULT_START, progress, reset_cursor=False)
    # 1 past_messages + (1 initial + 3 retries = 4) timeline
    assert client.get.call_count <= 5


@pytest.mark.asyncio
async def test_backfill_member_cursor_selection(monkeypatch):
    captured_urls = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"messages": []}
        return resp

    client = AsyncMock()
    client.get.side_effect = mock_get

    monkeypatch.setattr("tools.backfill_archive.validate_account_cred", lambda *_: (True, "OK"))
    monkeypatch.setattr("tools.backfill_archive.archive.load_archived_ids", lambda *_: (set(), set()))
    monkeypatch.setattr("tools.backfill_archive.proactive_refresh_if_expiring", AsyncMock(return_value=True))
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    # Case 1: Tomisato Nao with reset_cursor=True -> cursor should be 2022-10-03T00:00:00Z
    member = {"account_id": "acc1", "m_id": "100", "m_name": "冨里 奈央", "group_type": "nogizaka"}
    progress = {}
    await backfill_member(client, member, DEFAULT_START, progress, reset_cursor=True)
    # timeline is second call
    assert any("updated_from=2022-10-03" in u for u in captured_urls)

    # Case 2: User specified start
    captured_urls.clear()
    await backfill_member(client, member, "2024-06-01T00:00:00Z", progress, reset_cursor=False, user_specified_start=True)
    assert any("updated_from=2024-06-01" in u for u in captured_urls)

    # Case 3: Incremental with saved progress
    captured_urls.clear()
    progress["acc1_nogizaka_100"] = "2025-01-01T00:00:00Z"
    await backfill_member(client, member, DEFAULT_START, progress, reset_cursor=False, user_specified_start=False)
    assert any("updated_from=2025-01-01" in u for u in captured_urls)
