from src.qq_openid import _match_openid_event


def test_group_openid_matching_ignores_c2c_events() -> None:
    c2c = {
        "t": "C2C_MESSAGE_CREATE",
        "d": {"author": {"user_openid": "USER_OPENID", "username": "user"}},
    }
    group = {
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "group_openid": "GROUP_OPENID",
            "author": {"member_openid": "MEMBER_OPENID", "username": "member"},
        },
    }
    assert _match_openid_event(c2c, "group") is None
    assert _match_openid_event(group, "group") == {
        "openid": "GROUP_OPENID",
        "sender": "member",
        "raw": group,
    }


def test_user_openid_matching_ignores_group_events() -> None:
    group = {
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "group_openid": "GROUP_OPENID",
            "author": {"member_openid": "MEMBER_OPENID", "username": "member"},
        },
    }
    c2c = {
        "t": "C2C_MESSAGE_CREATE",
        "d": {"author": {"user_openid": "USER_OPENID", "username": "user"}},
    }
    assert _match_openid_event(group, "user") is None
    assert _match_openid_event(c2c, "user") == {
        "openid": "USER_OPENID",
        "sender": "user",
        "raw": c2c,
    }
