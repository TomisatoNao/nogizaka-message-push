"""Telegram Bot 专属凭证模型回归测试。"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.config as cfg  # noqa: E402
from src.webui_modules import config_service, system_handlers  # noqa: E402
from src.platforms import tgbot  # noqa: E402


def test_tg_token_env_key_is_stable_and_safe() -> None:
    assert cfg.tg_token_env_key("tg_bot1") == "TG_BOT1_TOKEN"
    assert cfg.tg_token_env_key("tg-bot 1") == "TG_BOT_1_TOKEN"
    assert cfg.tg_token_env_key("tg_bot") == "TG_BOT_INSTANCE_TOKEN"
    assert cfg.tg_token_env_key("  ") == ""


def test_build_tg_bots_never_falls_back_to_legacy_token(monkeypatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-token-must-not-be-used")
    monkeypatch.setenv("TG_BOT1_TOKEN", "dedicated-token-1")
    monkeypatch.delenv("TG_BOT2_TOKEN", raising=False)

    built = cfg._build_tg_bots({
        "enable_tg_bot": True,
        "tg_bots": [
            {"name": "tg_bot1", "target_chat": "-1001"},
            {"name": "tg_bot2", "target_chat": "-1002"},
        ],
    })

    first, second = built["tg_bots"]
    assert first["token"] == "dedicated-token-1"
    assert first["token_env"] == "TG_BOT1_TOKEN"
    assert first["token_configured"] is True
    assert second["token"] == ""
    assert second["token_env"] == "TG_BOT2_TOKEN"
    assert second["token_configured"] is False


def test_env_status_reports_per_bot_tokens_without_global_status(monkeypatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-token")
    monkeypatch.setenv("TG_BOT1_TOKEN", "dedicated-token-1")
    monkeypatch.delenv("TG_BOT2_TOKEN", raising=False)

    status = system_handlers.env_status({
        "tg_bots": [{"name": "tg_bot1"}, {"name": "tg_bot2"}]
    })

    assert "TG_BOT_TOKEN" not in status
    assert status["legacy_tg_token"] is True
    assert status["tg_bots"]["tg_bot1"] == {
        "env_key": "TG_BOT1_TOKEN", "configured": True
    }
    assert status["tg_bots"]["tg_bot2"] == {
        "env_key": "TG_BOT2_TOKEN", "configured": False
    }


def test_initialize_skips_missing_dedicated_token(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(tgbot.cfg, "ENABLE_TG_BOT", True)
    monkeypatch.setattr(tgbot.cfg, "TG_BOTS", [{
        "name": "tg_bot2", "token": "", "token_env": "TG_BOT2_TOKEN"
    }])
    monkeypatch.setenv("TG_BOT_TOKEN", "legacy-token")
    monkeypatch.delenv("TG_BOT2_TOKEN", raising=False)
    monkeypatch.setattr(tgbot, "log_all", lambda message, **_kwargs: events.append(message))

    tgbot.initialize()

    assert tgbot.get_configured_bots() == []
    assert any("TG_BOT2_TOKEN" in message and "已跳过" in message for message in events)


def test_validate_config_rejects_tg_env_key_collisions() -> None:
    raw = {
        "channels": {"tg": True},
        "accounts": {"demo": {"auth": "web", "group": "hinatazaka46"}},
        "monitor": [],
        "tg_bots": [
            {"name": "tg-bot1", "target_chat": "-1001"},
            {"name": "tg_bot1", "target_chat": "-1002"},
        ],
    }

    errors = config_service.validate_config(raw)
    assert any("映射到同一凭证变量" in error for error in errors)


def test_validate_config_rejects_unmappable_tg_name() -> None:
    raw = {
        "channels": {"tg": True},
        "accounts": {"demo": {"auth": "web", "group": "hinatazaka46"}},
        "monitor": [],
        "tg_bots": [{"name": "机器人", "target_chat": "-1001"}],
    }

    errors = config_service.validate_config(raw)
    assert any("无法生成有效凭证变量" in error for error in errors)


def test_web_secret_validation_rejects_legacy_global_tg_token() -> None:
    errors = config_service.validate_secret_values({"TG_BOT_TOKEN": "legacy"})
    assert any("不允许通过网页写入" in error for error in errors)
    assert config_service.validate_secret_values({"TG_BOT1_TOKEN": "dedicated"}) == []
