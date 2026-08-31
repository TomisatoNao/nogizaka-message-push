from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools import audit_logs


def test_audit_codebase_logs(tmp_path: Path):
    sample_file = tmp_path / "sample.py"
    sample_file.write_text(
        "try:\n"
        "    x = 1 / 0\n"
        "except ZeroDivisionError:\n"
        "    pass\n\n"
        "print('hello world')\n",
        encoding="utf-8",
    )

    res = audit_logs.audit_codebase_logs(tmp_path)
    assert res["files_scanned"] == 1
    assert res["total_try_blocks"] == 1
    assert len(res["silent_excepts"]) == 1
    assert res["silent_excepts"][0]["exc_type"] == "ZeroDivisionError"


def test_audit_log_files(tmp_path: Path):
    log_file = tmp_path / "test.log"
    log_file.write_text(
        "[2026-08-31 12:00:00] [INFO] System started\n"
        "[2026-08-31 12:00:01] [ERROR] Something failed\n"
        "[2026-08-31 12:00:02] [DEBUG] Bearer ***HIDDEN*** auth ok\n",
        encoding="utf-8",
    )

    res = audit_logs.audit_log_files(tmp_path)
    assert len(res["log_files"]) == 1
    assert res["level_counts"]["INFO"] == 1
    assert res["level_counts"]["ERROR"] == 1
    assert res["sensitive_leaks"] == []
