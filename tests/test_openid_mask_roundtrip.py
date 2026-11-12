import pytest

from src.webui_modules.config_service import redact_sensitive_config, restore_sensitive_config


@pytest.mark.parametrize("reordered", [False, True])
def test_colliding_masks_preserve_user_and_group_openids(reordered):
    existing = {"qq_commands": {"allow_openids": [
        {"openid": "SAME_user_value_TAIL", "type": "user"},
        {"openid": "SAME_group_value_TAIL", "type": "group"},
    ]}}
    submitted = redact_sensitive_config(existing)
    if reordered:
        submitted["qq_commands"]["allow_openids"].reverse()
    result = restore_sensitive_config(submitted, existing)
    assert {item["type"]: item["openid"] for item in result["qq_commands"]["allow_openids"]} == {
        "user": "SAME_user_value_TAIL", "group": "SAME_group_value_TAIL",
    }


def test_ambiguous_mask_does_not_choose_arbitrary_authorization():
    existing = {"qq_commands": {"allow_openids": [
        {"openid": "SAME_first_TAIL", "type": "user"},
        {"openid": "SAME_second_TAIL", "type": "user"},
    ]}}
    submitted = redact_sensitive_config(existing)
    result = restore_sensitive_config(submitted, existing)
    assert all(item["openid"] == "SAME****TAIL" for item in result["qq_commands"]["allow_openids"])


def test_unresolved_authorization_mask_is_rejected():
    from src.webui_modules.config_service import validate_config

    errors = validate_config({"accounts": {}, "monitor": [], "qq_commands": {
        "allow_openids": [{"openid": "SAME****TAIL", "type": "user"}],
    }})
    assert any("无法唯一还原" in error for error in errors)
