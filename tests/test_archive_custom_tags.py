"""自定义标签与 SQLite 搜索索引的契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config.config as cfg
from src import archive
from src.webui_modules.archive.messages import handle_messages


class _JsonHandler:
    """让归档路由测试直接读取 ``_send_json_resp`` 的结构化结果。"""

    def __init__(self, path: str):
        self.path = path
        self.payload: dict | None = None

    def _send_json(self, payload: dict, _code: int = 200) -> None:
        self.payload = payload


@pytest.fixture
def archive_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """为每个测试提供独立的归档目录和 SQLite 连接。"""

    archive.close_db()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(archive_dir))
    monkeypatch.setattr(archive, "_sqlite_conn", None)
    monkeypatch.setattr(archive, "_schema_initialized", False)
    yield archive_dir
    archive.close_db()


def _write_month(archive_dir: Path, member: str, message: dict) -> None:
    month_dir = archive_dir / member / "2026" / "09"
    month_dir.mkdir(parents=True)
    (month_dir / "messages.json").write_text(
        json.dumps([message], ensure_ascii=False),
        encoding="utf-8",
    )


def test_custom_tag_edit_updates_sqlite_search_index(archive_env: Path):
    """管理端修改标签后，SQLite 优先搜索应立即命中新标签。"""

    member = "测试成员"
    message = {
        "id": "custom-tag-1",
        "type": "text",
        "text": "正文内容",
        "published_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
        "_custom_tags": "",
    }
    _write_month(archive_env, member, message)
    archive.init_db()
    archive.sync_all_to_sqlite(force=True)

    from urllib.parse import quote

    handler = _JsonHandler(f"/api/archive/tags?member={quote(member)}")
    handled = handle_messages(
        handler,
        "tags",
        lambda **_kwargs: True,
        lambda: {
            "id": message["id"],
            "year": 2026,
            "month": 9,
            "custom_tags": "坐车",
        },
    )

    assert handled is True
    assert handler.payload == {"ok": True, "id": message["id"], "custom_tags": "坐车"}
    hits = archive.search(member, "坐车")
    assert [hit["id"] for hit in hits] == [message["id"]]


def test_startup_reindexes_existing_custom_tags_once(archive_env: Path):
    """旧数据库没有索引版本标记时，启动同步会补齐历史 JSON 标签。"""

    member = "历史成员"
    message = {
        "id": "legacy-custom-tag-1",
        "type": "text",
        "text": "旧消息",
        "published_at": "2026-09-02T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "_custom_tags": "坐车",
    }
    _write_month(archive_env, member, message)
    archive.init_db()

    # 模拟升级前的索引：消息存在，但自定义标签仍为空，且没有版本标记。
    stale_message = {**message, "_custom_tags": ""}
    archive._save_msgs_to_sqlite(member, 2026, 9, [stale_message])
    assert archive.search(member, "坐车") == []

    synced = archive.sync_all_to_sqlite()
    assert synced == 1
    hits = archive.search(member, "坐车")
    assert [hit["id"] for hit in hits] == [message["id"]]
    marker = archive.init_db().execute(
        "SELECT value FROM archive_meta WHERE key = ?",
        ("messages_index_version",),
    ).fetchone()
    assert marker == (str(archive._MESSAGE_INDEX_VERSION),)
