"""Regression coverage for favorite filtering before the bounded archive search cap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urlencode

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import src.config.config as cfg  # noqa: E402
from src import archive, archive_query  # noqa: E402
from src.webui_modules.archive import messages  # noqa: E402


@pytest.fixture
def archive_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    archive.close_db()
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(archive_dir))
    monkeypatch.setattr(archive, "_sqlite_conn", None)
    monkeypatch.setattr(archive, "_schema_initialized", False)
    yield archive_dir
    archive.close_db()


def _seed_search_hits(archive_dir: Path) -> list[dict]:
    member_dir = archive_dir / "测试成员"
    month_dir = member_dir / "2026" / "09"
    month_dir.mkdir(parents=True)

    oldest = datetime(2025, 1, 1, tzinfo=timezone.utc)
    hits = [{
        "id": "favorite-tail",
        "type": "video",
        "text": "needle hit",
        "updated_at": (oldest - timedelta(seconds=1)).isoformat(),
    }]
    for index in range(520):
        hits.append({
            "id": f"nonfavorite-{index:03d}",
            "type": "video",
            "text": "needle hit",
            "updated_at": (oldest + timedelta(seconds=index)).isoformat(),
        })
    hits.append({
        "id": "matching-other-type",
        "type": "text",
        "text": "needle hit",
        "updated_at": (oldest + timedelta(seconds=1000)).isoformat(),
    })

    (month_dir / "messages.json").write_text(
        json.dumps(hits, ensure_ascii=False), encoding="utf-8"
    )
    assert archive._save_msgs_to_sqlite("测试成员", 2026, 9, hits)
    return hits


class _SearchHandler:
    def __init__(self, path: str):
        self.path = path
        self.payload = None
        self.status = None

    def _send_json(self, payload, status=200):
        self.payload = payload
        self.status = status


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_favorite_search_filters_before_cap_and_is_user_scoped(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    _seed_search_hits(archive_env)
    if backend == "json":
        monkeypatch.setattr(archive_query, "_get_init_db", lambda: None)

    # Without an ID filter, the oldest favorite falls beyond the normal 500-hit cap.
    unfiltered = archive.search("测试成员", "needle", type_filter={"video"}, order="desc")
    assert len(unfiltered) == 500
    assert "favorite-tail" not in {message["id"] for message in unfiltered}

    favorite_ids_by_user = {
        "alice": {"favorite-tail"},
        "bob": {"nonfavorite-001"},
    }
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)
    monkeypatch.setattr(messages, "current_user", lambda _handler: {"username": current_username, "role": "viewer"})
    monkeypatch.setattr(
        messages._auth,
        "get_user_favorite_message_ids",
        lambda username, member: favorite_ids_by_user[username] if member == "测试成员" else set(),
    )
    monkeypatch.setattr(messages._auth, "get_user_message_interactions", lambda _username, _ids, *args, **kwargs: {})

    payloads = {}
    for current_username in ("alice", "bob"):
        query = urlencode({
            "member": "测试成员",
            "q": "needle",
            "type": "video",
            "favorite": "1",
            "order": "desc",
            "page": "1",
            "per_page": "50",
        })
        handler = _SearchHandler(f"/api/archive/search?{query}")
        assert messages.handle_messages(handler, "search", lambda **_: True, lambda: {})
        assert handler.status == 200
        payloads[current_username] = handler.payload

    assert payloads["alice"]["total"] == 1
    assert [item["id"] for item in payloads["alice"]["messages"]] == ["favorite-tail"]
    assert payloads["bob"]["total"] == 1
    assert [item["id"] for item in payloads["bob"]["messages"]] == ["nonfavorite-001"]

    favorite_order = {"favorite-tail", "nonfavorite-001", "nonfavorite-300"}
    desc_ids = [
        item["id"] for item in archive.search(
            "测试成员", "needle", type_filter={"video"}, order="desc", id_filter=favorite_order
        )
    ]
    asc_ids = [
        item["id"] for item in archive.search(
            "测试成员", "needle", type_filter={"video"}, order="asc", id_filter=favorite_order
        )
    ]
    assert desc_ids == ["nonfavorite-300", "nonfavorite-001", "favorite-tail"]
    assert asc_ids == list(reversed(desc_ids))


def test_empty_favorite_filter_returns_before_archive_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        pytest.fail("empty favorite filters must return without opening or scanning the archive")

    monkeypatch.setattr(archive_query, "_get_init_db", fail_if_called)
    monkeypatch.setattr(archive, "list_months", fail_if_called)
    monkeypatch.setattr(archive, "load_month", fail_if_called)

    assert archive.search("测试成员", "needle", id_filter=set()) == []
