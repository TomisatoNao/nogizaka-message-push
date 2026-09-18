"""
tests/test_member_subscription_cancelled.py — 验证取消续订但未到期的成员状态判断与抓取保护
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.member_directory import (
    get_all_subscriptions,
    get_member_subscription,
    is_member_active_subscription,
    is_subscription_active,
    save_account_subscriptions,
)


def test_is_subscription_active():
    assert is_subscription_active(None) is False
    assert is_subscription_active({}) is False

    # 1. 正常 active
    assert is_subscription_active({"state": "active"}) is True
    assert is_subscription_active({"state": "ACTIVE"}) is True

    # 2. cancelled / canceled 但尚未到期 (未到 end_at)
    future_time = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert is_subscription_active({"state": "cancelled", "end_at": future_time}) is True
    assert is_subscription_active({"state": "canceled", "end_at": future_time}) is True

    # 3. cancelled 但已到期 (已过 end_at)
    past_time = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert is_subscription_active({"state": "cancelled", "end_at": past_time}) is False
    assert is_subscription_active({"state": "canceled", "end_at": past_time}) is False

    # 4. cancelled 但缺少 end_at 或畸形时间
    assert is_subscription_active({"state": "cancelled", "end_at": ""}) is False
    assert is_subscription_active({"state": "cancelled", "end_at": "invalid_date"}) is False

    # 5. expired / closed / unsubscribed
    assert is_subscription_active({"state": "expired", "end_at": future_time}) is False
    assert is_subscription_active({"state": "closed"}) is False
    assert is_subscription_active({"state": "unsubscribed"}) is False


def test_db_subscription_lifecycle(tmp_path, monkeypatch):
    """测试 SQLite 数据库写入与读取 cancelled 状态下 is_active 逻辑。"""
    import sqlite3
    db_file = tmp_path / "test_auth.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("""
        CREATE TABLE member_subscriptions (
            account_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            member_name TEXT DEFAULT '',
            state TEXT NOT NULL,
            sub_type TEXT DEFAULT '',
            start_at TEXT DEFAULT '',
            end_at TEXT DEFAULT '',
            auto_renewing INTEGER DEFAULT 0,
            updated_at REAL DEFAULT 0,
            PRIMARY KEY (account_id, member_id)
        );
    """)
    conn.commit()

    monkeypatch.setattr("src.auth.get_auth_db", lambda: conn)

    future_end = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past_end = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_groups = [
        {
            "id": "62",
            "name": "石森 璃花",
            "state": "open",
            "subscription": {
                "state": "cancelled",
                "type": "paid",
                "start_at": "2026-08-18T15:01:48Z",
                "end_at": future_end,
                "auto_renewing": False,
            },
        },
        {
            "id": "99",
            "name": "测试 过期成员",
            "state": "open",
            "subscription": {
                "state": "cancelled",
                "type": "paid",
                "start_at": "2026-07-01T00:00:00Z",
                "end_at": past_end,
                "auto_renewing": False,
            },
        },
        {
            "id": "100",
            "name": "测试 活跃成员",
            "state": "open",
            "subscription": {
                "state": "active",
                "type": "paid",
                "start_at": "2026-08-01T00:00:00Z",
                "end_at": future_end,
                "auto_renewing": True,
            },
        },
    ]

    save_account_subscriptions("sakurazaka_test", mock_groups)

    # 1. 检查石森璃花（未到期 cancelled）
    sub_rika = get_member_subscription("sakurazaka_test", "62")
    assert sub_rika is not None
    assert sub_rika["state"] == "cancelled"
    assert sub_rika["auto_renewing"] is False
    assert sub_rika["is_active"] is True
    assert is_member_active_subscription("sakurazaka_test", "62") is True

    # 2. 检查已过期 cancelled 成员
    sub_expired = get_member_subscription("sakurazaka_test", "99")
    assert sub_expired is not None
    assert sub_expired["state"] == "cancelled"
    assert sub_expired["is_active"] is False
    assert is_member_active_subscription("sakurazaka_test", "99") is False

    # 3. 检查活跃成员
    sub_active = get_member_subscription("sakurazaka_test", "100")
    assert sub_active is not None
    assert sub_active["state"] == "active"
    assert sub_active["is_active"] is True
    assert is_member_active_subscription("sakurazaka_test", "100") is True

    # 4. 检查未缓存成员
    assert is_member_active_subscription("sakurazaka_test", "999") is None

    # 5. 检查 get_all_subscriptions
    all_subs = get_all_subscriptions("sakurazaka_test")
    assert "sakurazaka_test:62" in all_subs
    assert all_subs["sakurazaka_test:62"]["is_active"] is True
    assert all_subs["sakurazaka_test:99"]["is_active"] is False


def test_handle_members_cancelled_active(monkeypatch):
    """测试 /api/members 返回的 is_subscribed 与 is_past_subscribed 状态。"""
    from src.webui_modules.system_handlers import handle_members

    handler = MagicMock()
    handler.path = "/api/members?account=sakura_acc"

    future_end = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_cfg = {
        "accounts": {
            "sakura_acc": {
                "group": "sakurazaka46",
                "auth_method": "web",
            }
        }
    }

    mock_members = [
        {
            "id": 62,
            "name": "石森 璃花",
            "state": "open",
            "tags": ["三期生"],
            "subscription": {
                "state": "cancelled",
                "type": "paid",
                "start_at": "2026-08-18T15:01:48Z",
                "end_at": future_end,
                "auto_renewing": False,
            },
        }
    ]

    sent_data = {}

    def mock_send_json(h, data, code=200):
        nonlocal sent_data
        sent_data = data

    monkeypatch.setattr("src.webui_modules.system_handlers.send_json", mock_send_json)
    monkeypatch.setattr("src.config.credentials.load_all_accounts", lambda: None)
    monkeypatch.setattr("src.config.credentials.is_account_fetch_available", lambda a: (True, ""))
    monkeypatch.setattr("src.config.credentials.validate_account_cred", lambda a: (True, ""))

    async def mock_fetch(client, acc):
        return mock_members, None

    with patch("src.member_directory.fetch_member_directory", side_effect=mock_fetch):
        handle_members(handler, lambda: raw_cfg)

    assert sent_data.get("ok") is True
    members = sent_data.get("members", [])
    assert len(members) == 1
    m = members[0]
    assert m["id"] == "62"
    assert m["name"] == "石森 璃花"
    assert m["is_subscribed"] is True
    assert m["is_past_subscribed"] is False
    assert m["sub_state"] == "cancelled"
    assert m["auto_renewing"] is False
    assert sent_data.get("subscribed_count") == 1
    assert sent_data.get("past_subscribed_count") == 0
