import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from src.webui_modules.config_service import _replace_or_inplace_write
from tools import deploy_release


def test_replace_or_inplace_write_normal(tmp_path: Path):
    target = tmp_path / "config.json"
    target.write_text('{"initial": 1}', encoding="utf-8")
    tmp = tmp_path / "config.json.tmp"

    _replace_or_inplace_write(target, '{"updated": 2}', tmp_path=tmp)
    assert target.read_text(encoding="utf-8") == '{"updated": 2}'
    assert not tmp.exists()


def test_replace_or_inplace_write_ebusy_fallback_truncates_cleanly(tmp_path: Path):
    target = tmp_path / "mounted_config.json"
    old_content = "A" * 500  # 500 bytes long
    target.write_text(old_content, encoding="utf-8")
    tmp = tmp_path / "mounted_config.json.tmp"

    new_content = '{"short": 1}'  # Much shorter than 500 bytes

    def mock_replace(src, dst):
        raise OSError(errno.EBUSY, "Device or resource busy")

    with patch("os.replace", side_effect=mock_replace):
        _replace_or_inplace_write(target, new_content, tmp_path=tmp)

    # Must be exactly new_content, no trailing 'A's left over
    assert target.read_text(encoding="utf-8") == new_content
    assert not tmp.exists()
    # Temporary backup should be cleaned up after successful write
    assert not (tmp_path / "mounted_config.json.bak").exists()


def test_replace_or_inplace_write_bytes_ebusy_fallback(tmp_path: Path):
    target = tmp_path / "test.bin"
    target.write_bytes(b"initial_long_content_bytes")
    tmp = tmp_path / "test.bin.tmp"
    new_bytes = b"short"

    def mock_replace(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    with patch("os.replace", side_effect=mock_replace):
        _replace_or_inplace_write(target, new_bytes, tmp_path=tmp)

    assert target.read_bytes() == new_bytes
    assert not tmp.exists()


def test_replace_or_inplace_write_restores_from_bak_on_error(tmp_path: Path):
    target = tmp_path / "protected.json"
    original_text = '{"important": "safe"}'
    target.write_text(original_text, encoding="utf-8")
    tmp = tmp_path / "protected.json.tmp"

    def mock_replace(src, dst):
        raise OSError(errno.EBUSY, "Device or resource busy")

    # Simulate an error while writing target
    original_open = open

    def mock_open(file, *args, **kwargs):
        if str(file) == str(target) and "r+b" in str(args):
            raise IOError("Disk write fault")
        return original_open(file, *args, **kwargs)

    with patch("os.replace", side_effect=mock_replace):
        with patch("builtins.open", side_effect=mock_open):
            with pytest.raises(IOError, match="Disk write fault"):
                _replace_or_inplace_write(target, '{"corrupt": true}', tmp_path=tmp)

    # Original content should remain intact
    assert target.read_text(encoding="utf-8") == original_text


def test_deploy_release_lock_and_stage_resilience():
    script = deploy_release.REMOTE_DEPLOY_SCRIPT
    # A7 check: mutex lock metadata, timeout recovery, and PID tracking
    assert "LOCK_FILE=.deploy-lock" in script
    assert "LOCK_META=.deploy-lock.meta" in script
    assert "LOCK_TIMEOUT=300" in script
    assert "DEPLOY_ID=" in script
    assert "HOLDER_PID=" in script
    assert "STALE_HEALED=" in script
    assert "kill -TERM" in script
    assert "kill -KILL" in script

    # A7 check: explicit stage markers
    assert "DEPLOY_STAGE=" in script
    assert "prepared" in script
    assert "replacing" in script
    assert "replaced" in script
    assert "verified" in script
    assert "rolled_back" in script
    assert "failed_pull" in script
