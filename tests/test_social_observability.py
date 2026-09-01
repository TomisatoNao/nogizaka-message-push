"""社媒与消息日志可定位性回归测试。"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import config.config as cfg
from src import fetcher
from src.notifier import DeliveryReport
from src.social.fetchers.instagram_fetcher import InstagramFetcher
from src.social.forwarder import SocialForwarder
from src.social.models import Post
from src.social.store import SocialStore


class _StoryDownloader:
    def download_many(self, tasks, referer=""):
        paths = []
        for _url, target in tasks:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake-image")
            paths.append(str(path))
        return paths

    def download_via_ytdlp(self, *args, **kwargs):
        return []


def _story_fetcher(tmp_path: Path, entries: list[dict]) -> InstagramFetcher:
    config = {
        "platforms": {
            "instagram": {
                "enabled": True,
                "accounts": ["test_account"],
                "download_dir": str(tmp_path / "media"),
            }
        }
    }
    store = SocialStore(str(tmp_path / "social.db"))
    fetcher_instance = InstagramFetcher(config, store, _StoryDownloader())
    fetcher_instance._session.cookies.set("sessionid", "present")
    fetcher_instance._warm_session = lambda: None
    fetcher_instance._api_story_entries = lambda account: entries
    return fetcher_instance


def test_story_scan_reports_dedup_without_claiming_new_push(tmp_path: Path, caplog):
    fetcher_instance = _story_fetcher(
        tmp_path,
        [{"id": "already-sent", "media": [{"type": "image", "url": "https://example.com/a.jpg"}]}],
    )
    fetcher_instance._store.mark_sent("instagram", "instagram_story_already-sent")

    caplog.set_level(logging.INFO, logger="collink")
    result = fetcher_instance._fetch_stories("test_account")

    assert result == []
    assert "API返回 1" in caplog.text
    assert "已推送/去重 1" in caplog.text
    assert "待处理 0" in caplog.text
    assert "不进入推送" in caplog.text
    assert "开始下载 Story" not in caplog.text


def test_story_scan_reports_downloaded_items_waiting_for_forward(tmp_path: Path, caplog):
    fetcher_instance = _story_fetcher(
        tmp_path,
        [{"id": "new-story", "media": [{"type": "image", "url": "https://example.com/a.jpg"}]}],
    )

    caplog.set_level(logging.INFO, logger="collink")
    result = fetcher_instance._fetch_stories("test_account")

    assert len(result) == 1
    assert result[0].post_id == "instagram_story_new-story"
    assert "API返回 1" in caplog.text
    assert "待处理 1" in caplog.text
    assert "下载成功 1" in caplog.text
    assert "待转发 1" in caplog.text


def test_forwarder_reports_no_route_explicitly(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cfg, "ENABLE_NAPCAT_QQ", False)
    monkeypatch.setattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False)
    monkeypatch.setattr(cfg, "ENABLE_TG_BOT", False)
    forwarder = SocialForwarder(
        {"social": {"translate": False}},
        store=SocialStore(str(tmp_path / "social.db")),
    )
    post = Post(
        platform="instagram",
        post_id="story-no-route",
        author="test_account",
        extra={"account": "test_account", "_skip_translate": True},
    )
    archive_stub = SimpleNamespace(add_post=lambda _post: True)
    monkeypatch.setattr("src.social.archive.get_archive", lambda: archive_stub)
    logs: list[str] = []
    monkeypatch.setattr("src.social.forwarder.log_all", lambda content, **_kwargs: logs.append(str(content)))

    assert forwarder.forward_post(post) is True
    assert forwarder.last_delivery_result is not None
    assert forwarder.last_delivery_result.outcome == "no_route"
    assert forwarder.last_delivery_result.matched_routes == 0
    assert any("无匹配路由" in line for line in logs)


def test_forwarder_distinguishes_partial_success_and_keeps_retry_semantics(
    tmp_path: Path, monkeypatch
):
    routes = [
        {"group_id": 111, "push_instagram": True},
        {"group_id": 222, "push_instagram": True},
    ]
    monkeypatch.setattr(cfg, "ENABLE_NAPCAT_QQ", True)
    monkeypatch.setattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False)
    monkeypatch.setattr(cfg, "ENABLE_TG_BOT", False)
    monkeypatch.setattr(cfg, "NAPCAT_ROUTES", routes)

    async def _send(group_id, _chain):
        return group_id == 111

    monkeypatch.setattr("src.social.forwarder.napcat.send_qq_message", _send)
    archive_stub = SimpleNamespace(add_post=lambda _post: True)
    monkeypatch.setattr("src.social.archive.get_archive", lambda: archive_stub)
    logs: list[str] = []
    monkeypatch.setattr("src.social.forwarder.log_all", lambda content, **_kwargs: logs.append(str(content)))

    store = SocialStore(str(tmp_path / "social.db"))
    forwarder = SocialForwarder(
        {"social": {"translate": False}},
        store=store,
    )
    post = Post(
        platform="instagram",
        post_id="story-partial",
        author="test_account",
        extra={"account": "test_account", "_skip_translate": True},
    )

    # 部分成功不能提前将整条内容标记为 sent，否则失败路由无法补偿。
    assert forwarder.forward_post(post) is False
    report = forwarder.last_delivery_result
    assert report is not None
    assert report.outcome == "partial"
    assert report.matched_routes == 2
    assert report.success_routes == 1
    assert report.failed_routes == 1
    assert store.delivered_routes("instagram", "story-partial") == {"napcat:111"}
    assert any("部分成功" in line for line in logs)
    assert any("下轮仅重试失败路由" in line for line in logs)


@pytest.mark.asyncio
async def test_message_translation_log_contains_metadata_only(monkeypatch):
    logs: list[str] = []
    monkeypatch.setattr(cfg, "ENABLE_TRANSLATION", True)
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0)
    monkeypatch.setattr(fetcher, "log_all", lambda content, **_kwargs: logs.append(str(content)))
    monkeypatch.setattr(
        fetcher,
        "translate_text_with_model",
        lambda *args, **kwargs: _translated_result(),
    )
    monkeypatch.setattr(fetcher.archive, "archive_message", _async_noop)
    monkeypatch.setattr(fetcher, "send_member_message_detailed", _empty_delivery)
    monkeypatch.setattr(fetcher, "successful_routes", lambda *args: set())
    monkeypatch.setattr(fetcher, "mark_successful_routes", lambda *args: None)
    monkeypatch.setattr(fetcher, "save_sent_id", lambda *args: None)

    member = {"m_name": "测试成员", "group_type": "nogizaka46", "m_id": "1"}
    message = {"id": "message-1", "updated_at": "2026-09-01T00:00:00Z", "text": "原始日文文本"}
    result = await fetcher._handle_message(member, message, [], set(), [""])

    assert result is True
    joined = "\n".join(logs)
    assert "翻译完成" in joined
    assert "模型: test-model" in joined
    assert "原文: 6 字" in joined
    assert "译文: 12 字" in joined
    assert "私密翻译正文" not in joined
    assert "第二行正文" not in joined


async def _translated_result():
    return "私密翻译正文\n第二行正文", "test-model"


async def _async_noop(*args, **kwargs):
    return None


async def _empty_delivery(*args, **kwargs):
    return DeliveryReport(())
