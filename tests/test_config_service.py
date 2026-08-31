import json
from pathlib import Path
import sys
import json5

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.webui_modules import config_service

SAMPLE_CONFIG = {
    "channels": {"napcat": True, "tg": True, "qq_official": False},
    "napcat_api": "http://127.0.0.1:3000/send_group_msg",
    "web_admin": {"enabled": True, "host": "127.0.0.1", "port": 46046},
    "accounts": {
        "nogi_acc": {"auth": "web", "group": "nogizaka46"},
        "mobile_acc": {"auth": "mobile", "group": "nogizaka46"},
    },
    "monitor": [
        {"id": "1", "name": "冨里奈央", "account": "nogi_acc", "groups": [12345]},
        {"id": "2", "name": "五百城茉央", "account": "mobile_acc", "groups": [12345]},
    ],
    "day_interval": [120, 180],
    "night_interval": [1500, 1800],
    "sleep_hours": [2, 7],
    "alert_cooldown": 3600,
}


def test_validate_config(tmp_path: Path):
    errors = config_service.validate_config(SAMPLE_CONFIG)
    assert errors == []

    # Invalid account reference
    bad_acc = json.loads(json.dumps(SAMPLE_CONFIG))
    bad_acc["monitor"][0]["account"] = "non_existent_account"
    errs = config_service.validate_config(bad_acc)
    assert any("未定义的账号" in e for e in errs)

    # Empty id
    bad_id = json.loads(json.dumps(SAMPLE_CONFIG))
    bad_id["monitor"][0]["id"] = ""
    errs = config_service.validate_config(bad_id)
    assert any("id 为空" in e for e in errs)

    # Duplicate id under same account
    dup_id = json.loads(json.dumps(SAMPLE_CONFIG))
    dup_id["monitor"].append({"id": "1", "name": "重复成员", "account": "nogi_acc", "groups": []})
    errs = config_service.validate_config(dup_id)
    assert any("重复" in e for e in errs)


def test_serialize_config():
    serialized = config_service.serialize_config(SAMPLE_CONFIG)
    assert "// ── 推送通道 ──" in serialized
    assert "// ── 账号池 ──" in serialized
    reparsed = json5.loads(serialized)
    assert reparsed == SAMPLE_CONFIG


def test_config_history_and_save(tmp_path: Path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(SAMPLE_CONFIG), encoding="utf-8")

    # Save new version
    updated = json.loads(json.dumps(SAMPLE_CONFIG))
    updated["alert_cooldown"] = 7200
    config_service.save_config(updated, path=config_file)

    assert json5.loads(config_file.read_text(encoding="utf-8")) == updated
    history = config_service.list_config_history(path=config_file)
    assert len(history) >= 1
    assert "config-" in history[0]["name"]


def test_validate_secret_values():
    valid = {"NOGI_ACC_TOKEN": "secret_token_123", "GEMINI_API_KEY": "AIzaSy..."}
    assert config_service.validate_secret_values(valid) == []

    # Forbidden key
    forbidden = {"WEB_ADMIN_TOKEN": "should_be_forbidden"}
    errs = config_service.validate_secret_values(forbidden)
    assert any("不允许通过网页写入" in e for e in errs)

    # Empty value
    empty = {"NOGI_ACC_TOKEN": ""}
    errs = config_service.validate_secret_values(empty)
    assert any("值为空" in e for e in errs)

    # Invalid characters (newline injection)
    injection = {"NOGI_ACC_TOKEN": "line1\nline2"}
    errs = config_service.validate_secret_values(injection)
    assert any("非法字符" in e for e in errs)


def test_update_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("INITIAL_VAR='hello'\nOLD_KEY='to_remove'\n", encoding="utf-8")

    config_service.update_env_file(
        values={"INITIAL_VAR": "world", "NEW_VAR": "value with spaces"},
        path=env_file,
        remove=["OLD_KEY"],
    )

    content = env_file.read_text(encoding="utf-8")
    assert "INITIAL_VAR='world'" in content
    assert "OLD_KEY" not in content
    assert "NEW_VAR='value with spaces'" in content


def test_cred_and_bot_status():
    status = config_service._cred_status(SAMPLE_CONFIG)
    assert "nogi_acc" in status
    assert "mobile_acc" in status
    assert status["nogi_acc"]["expected"] == ["NOGI_ACC_TOKEN", "NOGI_ACC_COOKIE"]
    assert status["mobile_acc"]["expected"] == ["MOBILE_ACC_REFRESH_TOKEN"]

    # Declared bots
    config_with_bot = json.loads(json.dumps(SAMPLE_CONFIG))
    config_with_bot["qq_official_bots"] = [{"name": "bot_alpha", "app_id": "1001"}]
    bot_status = config_service._qq_bot_status(config_with_bot)
    assert len(bot_status) == 1
    assert bot_status[0]["name"] == "bot_alpha"
    assert bot_status[0]["declared"] is True
