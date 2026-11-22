import io
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

from src.webui_modules.archive_handlers import _authorize_archive
from src.webui_modules.media_service import serve_file_range
from tools import backup_data


class DummyHandler:
    def __init__(self, command="GET", headers=None, path="/api/archive/avatar"):
        self.command = command
        self.headers = headers or {}
        self.path = path
        self.responses = []
        self.headers_sent = []
        self.wfile = io.BytesIO()
        self.ended_headers = False

    def send_response(self, code):
        self.responses.append(code)

    def send_header(self, key, value):
        self.headers_sent.append((key, value))

    def end_headers(self):
        self.ended_headers = True


def test_media_head_request_range_and_headers(tmp_path: Path):
    test_media = tmp_path / "sample.mp4"
    test_media.write_bytes(b"0123456789" * 100)  # 1000 bytes

    handler = DummyHandler(command="HEAD", headers={"Range": "bytes=100-199"})
    serve_file_range(handler, test_media)

    assert handler.responses == [206]
    sent_dict = dict(handler.headers_sent)
    assert sent_dict.get("Content-Length") == "100"
    assert sent_dict.get("Content-Range") == "bytes 100-199/1000"
    assert sent_dict.get("Accept-Ranges") == "bytes"
    assert "ETag" in sent_dict
    assert handler.ended_headers is True
    # Crucial A9 assertion: HEAD requests MUST NOT output body bytes
    assert handler.wfile.getvalue() == b""


def test_media_head_request_full_file(tmp_path: Path):
    test_media = tmp_path / "image.jpg"
    test_media.write_bytes(b"abcdefgh" * 50)  # 400 bytes

    handler = DummyHandler(command="HEAD", headers={})
    serve_file_range(handler, test_media)

    assert handler.responses == [200]
    sent_dict = dict(handler.headers_sent)
    assert sent_dict.get("Content-Length") == "400"
    assert sent_dict.get("Accept-Ranges") == "bytes"
    assert handler.wfile.getvalue() == b""


def test_archive_authorize_allows_head():
    guard = MagicMock(return_value=True)

    handler_head = DummyHandler(command="HEAD")
    allowed = _authorize_archive(handler_head, "avatar", guard)
    assert allowed is True

    handler_post_not_allowed = DummyHandler(command="POST")
    allowed_post = _authorize_archive(handler_post_not_allowed, "avatar", guard)
    assert allowed_post is False
    assert handler_post_not_allowed.responses == [405]


def test_backup_sqlite_online_snapshot_consistency(tmp_path: Path):
    root = tmp_path / "live_project"
    db_dir = root / "data"
    db_dir.mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.json").write_text('{"name": "test"}', encoding="utf-8")

    db_path = db_dir / "messages.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT);")
    conn.execute("INSERT INTO posts (title) VALUES ('post1'), ('post2');")
    conn.commit()

    # Verify WAL file exists
    assert (db_dir / "messages.db-wal").exists() or db_path.exists()

    dest = tmp_path / "backups"
    archive, manifest = backup_data.create_backup(root=root, destination=dest, keep=3)

    # A10 checks:
    # 1. Manifest must contain the db and config, but NEVER -wal or -shm files
    paths_in_manifest = [item["path"] for item in manifest["files"]]
    assert "data/messages.db" in paths_in_manifest
    assert not any(p.endswith(("-wal", "-shm")) for p in paths_in_manifest)

    # 2. Verify archive passes internal checksum and structure validation
    ok, errors, checked = backup_data.verify_backup(archive)
    assert ok is True and not errors and checked is not None

    # 3. Restore to a new location and verify sqlite quick_check passes
    restore_target = tmp_path / "restored_project"
    backup_data.restore_backup(archive, root=restore_target, apply=True)

    restored_db = restore_target / "data" / "messages.db"
    assert restored_db.exists()
    restored_conn = sqlite3.connect(restored_db)
    check_row = restored_conn.execute("PRAGMA quick_check;").fetchone()
    assert check_row == ("ok",)

    count_row = restored_conn.execute("SELECT COUNT(*) FROM posts;").fetchone()
    assert count_row == (2,)

    conn.close()
    restored_conn.close()
