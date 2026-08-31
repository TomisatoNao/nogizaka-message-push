"""M4 运维能力：审计日志与备份恢复。"""

from __future__ import annotations

import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config.config as cfg  # noqa: E402
from src.audit import record_event  # noqa: E402
from tools import backup_data  # noqa: E402


def test_audit_event_is_structured_and_redacts_secrets(tmp_path: Path):
    original = getattr(cfg, "AUDIT_LOG_FILE", None)
    cfg.AUDIT_LOG_FILE = str(tmp_path / "audit.jsonl")
    try:
        record_event(
            "config.update", outcome="success", actor="admin", source_ip="127.0.0.1",
            details={"keys": "TG_BOT_TOKEN", "token": "must-not-appear", "password": "also-hidden"},
        )
        row = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
        assert row["event"] == "config.update"
        assert row["actor"] == "admin"
        assert row["details"]["keys"] == "TG_BOT_TOKEN"
        assert row["details"]["token"] == "***HIDDEN***"
        assert row["details"]["password"] == "***HIDDEN***"
    finally:
        if original is None:
            delattr(cfg, "AUDIT_LOG_FILE")
        else:
            cfg.AUDIT_LOG_FILE = original


def test_backup_verify_and_explicit_restore(tmp_path: Path):
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "config" / "config.json").write_text('{"name":"before"}', encoding="utf-8")
    (root / "data" / "auth.db").write_bytes(b"auth-before")
    (root / "data" / "app.pid").write_text("123", encoding="utf-8")

    archive, manifest = backup_data.create_backup(root=root, destination=root / "backups", keep=2)
    assert len(manifest["files"]) == 2
    ok, errors, checked_manifest = backup_data.verify_backup(archive)
    assert ok and not errors and checked_manifest is not None

    (root / "config" / "config.json").write_text('{"name":"after"}', encoding="utf-8")
    (root / "data" / "auth.db").write_bytes(b"auth-after")
    (root / "data" / "app.pid").unlink()
    dry_run = backup_data.restore_backup(archive, root=root, apply=False)
    assert not dry_run["applied"]
    assert (root / "data" / "auth.db").read_bytes() == b"auth-after"

    restored = backup_data.restore_backup(archive, root=root, apply=True)
    assert restored["applied"]
    assert (root / "config" / "config.json").read_text(encoding="utf-8") == '{"name":"before"}'
    assert (root / "data" / "auth.db").read_bytes() == b"auth-before"
    assert not (root / "data" / "app.pid").exists()
