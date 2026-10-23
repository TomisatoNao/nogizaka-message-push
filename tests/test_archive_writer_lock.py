"""Cross-process regression coverage for archive month writers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import src.config.config as cfg
from src import archive


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


def test_month_writer_fails_closed_on_corrupt_json(archive_env: Path) -> None:
    month_dir = archive_env / "测试成员" / "2026" / "09"
    month_dir.mkdir(parents=True)
    json_path = month_dir / "messages.json"
    json_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(archive.ArchiveDataError):
        archive._merge_month_records(
            "测试成员", 2026, 9, [{"id": "new-message", "text": "保留现场"}]
        )

    assert json_path.read_text(encoding="utf-8") == "{broken"
    assert not list(month_dir.glob("*.tmp"))


def test_sqlite_failure_is_rebuilt_from_saved_json(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive.init_db()
    save_to_sqlite = archive._save_msgs_to_sqlite
    monkeypatch.setattr(archive, "_save_msgs_to_sqlite", lambda *_args: False)

    with pytest.raises(archive.ArchiveIndexSyncError):
        archive._merge_month_records(
            "测试成员",
            2026,
            9,
            [{"id": "index-recovery", "text": "索引恢复内容", "updated_at": "2026-09-01T00:00:00Z"}],
        )

    marker = archive_env / ".messages-index-dirty"
    assert marker.is_file()
    assert len(archive.load_month("测试成员", 2026, 9)) == 1

    monkeypatch.setattr(archive, "_save_msgs_to_sqlite", save_to_sqlite)
    assert archive.sync_all_to_sqlite() == 1
    assert not marker.exists()
    assert [hit["id"] for hit in archive.search("测试成员", "索引恢复内容")] == ["index-recovery"]


def test_writer_persists_dirty_marker_before_replacing_json(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = archive._atomic_write_json
    marker = archive_env / ".messages-index-dirty"
    observed: list[bool] = []

    def check_marker_first(path: Path, data: list[dict]) -> None:
        observed.append(marker.is_file())
        original_write(path, data)

    monkeypatch.setattr(archive, "_atomic_write_json", check_marker_first)
    archive._merge_month_records(
        "测试成员", 2026, 9, [{"id": "durable-marker", "text": "恢复标记先落盘"}]
    )

    assert observed == [True]
    assert not marker.exists()


def test_writer_refuses_json_change_when_dirty_marker_cannot_be_created(
    archive_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive, "_mark_messages_index_dirty", lambda: False)

    with pytest.raises(archive.ArchiveWriteError):
        archive._merge_month_records(
            "测试成员", 2026, 9, [{"id": "no-marker", "text": "不应写入"}]
        )

    assert not (archive_env / "测试成员" / "2026" / "09" / "messages.json").exists()


def test_atomic_json_replace_fsyncs_parent_directory(archive_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(archive, "_fsync_directory", synced.append)
    json_path = archive_env / "测试成员" / "2026" / "09" / "messages.json"

    archive._atomic_write_json(json_path, [{"id": "durable-json"}])

    assert json_path.is_file()
    assert synced[-1] == json_path.parent


def test_full_index_rebuild_removes_records_missing_from_json(archive_env: Path) -> None:
    assert archive._save_msgs_to_sqlite(
        "测试成员",
        2026,
        9,
        [{"id": "removed-record", "text": "索引中残留的记录", "updated_at": "2026-09-01T00:00:00Z"}],
    )
    assert archive.search("测试成员", "索引中残留的记录")

    assert archive.sync_all_to_sqlite(force=True) == 0
    assert archive.search("测试成员", "索引中残留的记录") == []


def test_month_writer_preserves_legacy_unpadded_month_path(archive_env: Path) -> None:
    legacy_dir = archive_env / "测试成员" / "2026" / "9"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "messages.json"
    legacy_path.write_text(json.dumps([{"id": "old", "text": "旧记录"}], ensure_ascii=False), encoding="utf-8")

    archive._merge_month_records("测试成员", 2026, 9, [{"id": "new", "text": "新记录"}])

    assert {item["id"] for item in json.loads(legacy_path.read_text(encoding="utf-8"))} == {"old", "new"}
    assert not (archive_env / "测试成员" / "2026" / "09" / "messages.json").exists()


def test_timezone_realign_recovers_after_destination_write(archive_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    member_dir = archive_env / "测试成员"
    source_month = member_dir / "2026" / "09"
    source_media = source_month / "images" / "message.png"
    source_media.parent.mkdir(parents=True)
    source_media.write_bytes(b"image")
    source_json = source_month / "messages.json"
    source_json.write_text(
        json.dumps(
            [{
                "id": "jst-month-boundary",
                "type": "image",
                "published_at": "2026-09-30T16:30:00Z",
                "updated_at": "2026-09-30T16:30:00Z",
                "_local_file": "2026/09/images/message.png",
            }]
        ),
        encoding="utf-8",
    )

    original_write = archive._atomic_write_json

    def fail_source_replace(path: Path, data: list[dict]) -> None:
        if path == source_json:
            raise OSError("simulated interruption before source cleanup")
        original_write(path, data)

    monkeypatch.setattr(archive, "_atomic_write_json", fail_source_replace)
    with pytest.raises(archive.ArchiveWriteError):
        archive.realign_archive_timezones(force=True)

    dirty_marker = archive_env / ".messages-index-dirty"
    target_json = member_dir / "2026" / "10" / "messages.json"
    target_media = member_dir / "2026" / "10" / "images" / "message.png"
    assert dirty_marker.is_file()
    assert source_json.is_file()
    assert target_json.is_file()
    assert target_media.read_bytes() == b"image"

    monkeypatch.setattr(archive, "_atomic_write_json", original_write)
    assert archive.realign_archive_timezones(force=True) == 1
    assert not source_json.exists()
    assert archive.sync_all_to_sqlite() == 1
    assert not dirty_marker.exists()
    assert archive.load_month("测试成员", 2026, 10)[0]["_local_file"] == "2026/10/images/message.png"


def test_media_header_resolution_uses_member_monitor_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config import credentials

    monkeypatch.setattr(
        cfg,
        "MONITOR_LIST",
        [{"m_name": "测试成员", "account_id": "account-1", "group_type": "nogizaka46"}],
    )
    monkeypatch.setattr(
        credentials,
        "get_source_headers_for_account",
        lambda account_id, group_type: {"authorization": f"{account_id}:{group_type}"},
    )

    assert archive._media_headers_for_message("测试成员", {}) == {
        "authorization": "account-1:nogizaka46"
    }


def test_independent_processes_merge_same_month_without_losing_fields(archive_env: Path, tmp_path: Path) -> None:
    """Two service processes updating the same month must retain both deltas."""
    ready = tmp_path / "start-writers"
    script = """
import time
from pathlib import Path
import src.config.config as cfg
cfg.ARCHIVE_DIR = __ARCHIVE_ROOT__
from src import archive
start = Path(__START_FILE__)
while not start.exists():
    time.sleep(0.005)
worker = "__WORKER__"
for index in range(40):
    record = {"id": str(index), "updated_at": "2026-09-01T00:00:00Z"}
    if worker == "content":
        record.update({"text": f"message-{index}", "_translation": f"translation-{index}"})
    else:
        record["_custom_tags"] = "reviewed"
    archive._merge_month_records("测试成员", 2026, 9, [record])
"""
    script = (
        script.replace("__ARCHIVE_ROOT__", repr(str(archive_env)))
        .replace("__START_FILE__", repr(str(ready)))
        .replace("__WORKER__", "content")
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script.replace('worker = "content"', f"worker = {worker!r}")],
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for worker in ("content", "tags")
    ]
    ready.touch()
    try:
        results = [process.communicate(timeout=90) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    errors = [stderr for (_, stderr), process in zip(results, processes, strict=True) if process.returncode]
    assert not errors, "\n".join(errors)

    messages = archive.load_month("测试成员", 2026, 9)
    by_id = {str(message["id"]): message for message in messages}
    assert len(by_id) == 40
    assert all(
        by_id[str(index)].get("text") == f"message-{index}"
        and by_id[str(index)].get("_translation") == f"translation-{index}"
        and by_id[str(index)].get("_custom_tags") == "reviewed"
        for index in range(40)
    )
    json_path = archive_env / "测试成员" / "2026" / "09" / "messages.json"
    assert len(json.loads(json_path.read_text(encoding="utf-8"))) == 40
