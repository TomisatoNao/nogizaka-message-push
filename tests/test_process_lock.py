"""Exercise ownership and crash recovery using independent processes."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.app_modules import process_lock


WORKER = """
import sys
from pathlib import Path
from src.app_modules import process_lock as lock
lock.PID_FILE = Path(sys.argv[1])
lock._acquire_instance_lock()
print('locked', flush=True)
if len(sys.argv) > 2:
    sys.stdin.readline()
lock._release_instance_lock()
"""


def run_worker(path):
    return subprocess.run([sys.executable, "-c", WORKER, str(path)],
                          capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize("crash", [False, True])
def test_instance_lock_excludes_competitor_and_recovers(tmp_path, crash):
    path = tmp_path / "app.pid"
    owner = subprocess.Popen([sys.executable, "-c", WORKER, str(path), "wait"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == "locked"
        result = run_worker(path)
        assert result.returncode != 0
        assert "locked" not in result.stdout
        assert owner.poll() is None
        if crash:
            owner.kill()
            owner.communicate(timeout=15)
        else:
            owner.communicate("stop\n", timeout=15)
        assert run_worker(path).returncode == 0
        assert not path.exists()
        assert path.with_suffix(".pid.lock").exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.communicate(timeout=15)


def test_live_legacy_pid_is_not_killed(tmp_path):
    path = tmp_path / "app.pid"
    path.write_text(str(os.getpid()), encoding="utf-8")
    result = run_worker(path)
    assert result.returncode != 0
    assert "still running" in result.stderr
    assert path.read_text(encoding="utf-8") == str(os.getpid())


def test_metadata_write_failure_releases_os_lock(tmp_path, monkeypatch):
    path = tmp_path / "app.pid"
    monkeypatch.setattr(process_lock, "PID_FILE", path)
    # Other tests may already have imported src.app; honor its compatibility path.
    app = sys.modules.get("src.app")
    if app:
        monkeypatch.setattr(app, "PID_FILE", path)
    original = Path.write_text

    def fail_write(self, *args, **kwargs):
        if self == path:
            raise OSError("simulated storage failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError, match="storage failure"):
        process_lock._acquire_instance_lock()
    assert process_lock._instance_lock_handle is None
    assert run_worker(path).returncode == 0
