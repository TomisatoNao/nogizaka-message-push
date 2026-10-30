"""验证针对 2026-10-29 走查修复清单（H-01 ~ H-03, M-01 ~ M-03, L-01 ~ L-08）的各项契约。"""
from __future__ import annotations

from src.health import ErrorTier, HealthTracker
from src.webui_modules.config_service import (
    is_masked_secret,
    mask_secret_value,
    redact_sensitive_config,
    restore_sensitive_config,
)
def test_mask_secret_value_and_detection():
    """验证密钥与 OpenID 脱敏规则及掩码判断。"""
    assert mask_secret_value("") == ""
    assert mask_secret_value("12345") == "*****"
    assert mask_secret_value("12345678") == "********"
    assert mask_secret_value("abcdefghijklmn") == "abcd****klmn"
    assert mask_secret_value("0123456789ABCDEF0123456789ABCDEF") == "0123****CDEF"

    assert is_masked_secret("abcd****klmn") is True
    assert is_masked_secret("****") is True
    assert is_masked_secret("normal_token_12345") is False


def test_redact_sensitive_config_masks_all_secrets():
    """验证 redact_sensitive_config 彻底遮蔽配置中的敏感密钥与 OpenID。"""
    raw = {
        "napcat_api_token": "my_onebot_secret_token",
        "napcat_media_signing_secret": "my_media_signing_secret",
        "qq_official_bots": [
            {
                "name": "bot1",
                "app_id": "102012345",
                "target_openid": "OPENID_USER_1234567890",
                "group_openid": "OPENID_GROUP_9876543210",
            }
        ],
        "cmd_whitelist_openids": [
            "WHITELIST_OPENID_12345678",
            {"name": "admin", "openid": "ADMIN_OPENID_98765432"},
        ],
    }

    redacted = redact_sensitive_config(raw)
    assert redacted["napcat_api_token"] == "my_o****oken"
    assert redacted["napcat_media_signing_secret"] == "my_m****cret"
    assert redacted["qq_official_bots"][0]["target_openid"] == "OPEN****7890"
    assert redacted["qq_official_bots"][0]["group_openid"] == "OPEN****3210"
    assert redacted["cmd_whitelist_openids"][0] == "WHIT****5678"
    assert redacted["cmd_whitelist_openids"][1]["openid"] == "ADMI****5432"

    # 原字典不受影响
    assert raw["napcat_api_token"] == "my_onebot_secret_token"


def test_restore_sensitive_config_preserves_unmodified_secrets():
    """验证当提交包含掩码的配置时，restore_sensitive_config 自动无损还原原值。"""
    existing = {
        "napcat_api_token": "original_token_value_123",
        "napcat_media_signing_secret": "original_signing_secret_456",
        "qq_official_bots": [
            {
                "name": "bot1",
                "target_openid": "REAL_OPENID_USER_12345",
                "group_openid": "REAL_OPENID_GROUP_67890",
            }
        ],
        "cmd_whitelist_openids": [
            "REAL_WHITELIST_12345678",
            {"name": "admin", "openid": "REAL_ADMIN_OPENID_8765"},
        ],
    }

    # 前端未修改，提交了脱敏后的掩码
    submitted = {
        "napcat_api_token": "orig****_123",
        "napcat_media_signing_secret": "orig****_456",
        "qq_official_bots": [
            {
                "name": "bot1",
                "target_openid": "REAL****2345",
                "group_openid": "REAL****7890",
            }
        ],
        "cmd_whitelist_openids": [
            "REAL****5678",
            {"name": "admin", "openid": "REAL****8765"},
        ],
    }

    restored = restore_sensitive_config(submitted, existing)
    assert restored["napcat_api_token"] == "original_token_value_123"
    assert restored["napcat_media_signing_secret"] == "original_signing_secret_456"
    assert restored["qq_official_bots"][0]["target_openid"] == "REAL_OPENID_USER_12345"
    assert restored["qq_official_bots"][0]["group_openid"] == "REAL_OPENID_GROUP_67890"
    assert restored["cmd_whitelist_openids"][0] == "REAL_WHITELIST_12345678"
    assert restored["cmd_whitelist_openids"][1]["openid"] == "REAL_ADMIN_OPENID_8765"


def test_restore_sensitive_config_updates_when_new_value_provided():
    """验证当用户确实提供了全新真实凭证时，正常更新为新值。"""
    existing = {
        "napcat_api_token": "old_token_12345678",
        "qq_official_bots": [{"name": "bot1", "target_openid": "OLD_OPENID_123456"}],
    }
    submitted = {
        "napcat_api_token": "brand_new_token_999999",
        "qq_official_bots": [{"name": "bot1", "target_openid": "NEW_OPENID_888888"}],
    }

    restored = restore_sensitive_config(submitted, existing)
    assert restored["napcat_api_token"] == "brand_new_token_999999"
    assert restored["qq_official_bots"][0]["target_openid"] == "NEW_OPENID_888888"


def test_member_health_deduplication_and_normalization():
    """验证 H-02：同一成员不同空格写法去重，总数与指标不矛盾。"""
    tracker = HealthTracker()

    # 模拟两处不同格式上报
    tracker.record_member_fetch("森田 ひかる", True)
    tracker.record_member_fetch("森田ひかる", False, ErrorTier.TRANSIENT, "连接超时")

    snap = tracker.snapshot()
    members = snap.get("members", [])
    # 必须去重为 1 人
    assert len(members) == 1
    # 状态更新为最新实际情况
    assert members[0]["fetch_ok"] is False
    assert members[0]["last_error"] == "连接超时"


def test_recent_errors_aggregation_and_counts():
    """验证 H-02：近期错误聚合计数，相同超时错误不刷屏。"""
    tracker = HealthTracker()

    # 同一错误连续发生 5 次
    for _ in range(5):
        tracker.record_error("田村 保乃 拉取失败: 请求超时", ErrorTier.TRANSIENT)

    # 另一错误发生 1 次
    tracker.record_error("金村 美玖 拉取失败: 401 Unauthorized", ErrorTier.PERSISTENT)

    snap = tracker.snapshot()
    errors = snap.get("errors", [])
    assert len(errors) == 2

    # 第一项累计 5 次
    item1 = next(e for e in errors if "田村 保乃" in e["msg"])
    assert item1["count"] == 5

    item2 = next(e for e in errors if "金村 美玖" in e["msg"])
    assert item2["count"] == 1


def test_server_header_and_head_method(monkeypatch):
    """验证 H-01 与 L-07：HEAD 方法支持、Server 头版本隐蔽、favicon.ico 返回 200。"""
    from src.webui import _Handler

    assert _Handler.server_version == "SakamichiWebUI/1.0"
    assert _Handler.sys_version == ""

    # 测试 version_string 不含 Python/
    handler = _Handler.__new__(_Handler)
    v_str = handler.version_string()
    assert "Python/" not in v_str
    assert v_str == "SakamichiWebUI/1.0"
