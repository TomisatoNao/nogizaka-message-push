"""Focused tests for read-only member route diagnostics."""

from types import SimpleNamespace

import pytest

from src import route_diagnostics as diagnostics

_DEFAULT_SUBSCRIPTION_READER = diagnostics._subscription


@pytest.fixture
def clean_config(monkeypatch):
    monkeypatch.setattr(diagnostics.cfg, "ACCOUNTS", {"fetch-a": {"group_type": "nogizaka46"}}, raising=False)
    monkeypatch.setattr(diagnostics.cfg, "MONITOR_LIST", [], raising=False)
    monkeypatch.setattr(diagnostics.cfg, "ENABLE_NAPCAT_QQ", True, raising=False)
    monkeypatch.setattr(diagnostics.cfg, "ENABLE_QQ_OFFICIAL_BOT", True, raising=False)
    monkeypatch.setattr(diagnostics.cfg, "ENABLE_TG_BOT", True, raising=False)
    monkeypatch.setattr(diagnostics.cfg, "NAPCAT_ROUTES", [], raising=False)
    monkeypatch.setattr(diagnostics, "_configured_official_bots", lambda: [])
    monkeypatch.setattr(diagnostics, "_configured_telegram_bots", lambda: [])
    monkeypatch.setattr(diagnostics, "_subscription", lambda _account, _member: None)


def _report(**member):
    return diagnostics.diagnose_member_route({"m_name": "冨里 奈央", "m_id": "55", "account_id": "fetch-a", **member})


def test_empty_filter_and_legacy_name_or_id_filters_match_sender_semantics(clean_config, monkeypatch):
    monkeypatch.setattr(
        diagnostics.cfg,
        "NAPCAT_ROUTES",
        [
            {"group_id": "hidden-target-1"},  # legacy route: missing push_message defaults to enabled
            {"group_id": "hidden-target-2", "member_filter": ["冨里奈央"]},
            {"group_id": "hidden-target-3", "member_filter": ["55"]},
        ],
    )
    report = _report()

    napcat_routes = [route for route in report["routes"] if route["channel"] == "napcat"]
    assert [route["status"] for route in napcat_routes] == ["applicable"] * 3
    assert all(route["filter_status"] == "match" for route in napcat_routes)
    assert report["route_summary"]["delivery_status"] == "not_evaluated"


def test_config_variations_report_disabled_channel_route_and_missing_target(clean_config, monkeypatch):
    monkeypatch.setattr(diagnostics.cfg, "ENABLE_NAPCAT_QQ", False)
    monkeypatch.setattr(diagnostics.cfg, "NAPCAT_ROUTES", [
        {"group_id": "hidden-target", "push_message": False},
        {"group_id": "", "push_message": True},
    ])
    report = _report()

    assert [route["reason"] for route in report["routes"]] == ["channel_disabled", "channel_disabled"]
    assert "channel_disabled" in report["route_summary"]["reasons"]
    assert report["route_summary"]["status"] == "no_applicable_route"

    monkeypatch.setattr(diagnostics.cfg, "ENABLE_NAPCAT_QQ", True)
    report = _report()
    assert [route["reason"] for route in report["routes"]] == ["route_disabled", "no_target"]


def test_member_filter_miss_and_no_applicable_route_are_reported(clean_config, monkeypatch):
    monkeypatch.setattr(diagnostics.cfg, "NAPCAT_ROUTES", [{
        "group_id": "hidden-target",
        "member_filter": ["someone else"],
    }])

    report = _report()

    napcat = report["routes"][0]
    assert napcat["reason"] == "member_filter_miss"
    assert napcat["filter_status"] == "miss"
    assert report["route_summary"]["status"] == "no_applicable_route"
    assert "no_applicable_route" in report["route_summary"]["reasons"]


def test_fetch_account_and_member_id_reasons_are_separate_from_routes(clean_config, monkeypatch):
    read_calls = []
    monkeypatch.setattr(diagnostics, "_subscription", lambda *args: read_calls.append(args))
    report = diagnostics.diagnose_member_route({"m_name": "未知成员"})

    assert report["fetch_account"] == {"status": "missing", "account_id": None}
    assert report["member_id_state"] == "unknown"
    assert "missing_fetch_account" in report["route_summary"]["reasons"]
    assert "unknown_member_id" in report["route_summary"]["reasons"]
    assert read_calls == []


def test_missing_or_failed_subscription_read_is_unknown_not_unsubscribed(clean_config, monkeypatch):
    monkeypatch.setattr(diagnostics, "_subscription", lambda _account, _member: None)
    missing = _report()
    assert missing["subscription"] == {
        "state": "unknown",
        "reason": "subscription_not_synced_or_unavailable",
    }

    def failed_read(_account, _member):
        raise OSError("private database detail")

    monkeypatch.setattr(diagnostics, "_subscription", failed_read)
    failed = _report()
    assert failed["subscription"] == {"state": "unknown", "reason": "subscription_read_failed"}
    assert "unsubscribed" not in str(missing["subscription"])
    assert "unsubscribed" not in str(failed["subscription"])


def test_known_subscription_state_uses_existing_active_semantics(clean_config, monkeypatch):
    monkeypatch.setattr(diagnostics, "_subscription", lambda _account, _member: {"state": "expired"})
    report = _report()
    assert report["subscription"] == {"state": "inactive", "reason": "subscription_inactive"}

    monkeypatch.setattr(diagnostics, "_subscription", lambda _account, _member: {"state": "active"})
    report = _report()
    assert report["subscription"] == {"state": "active", "reason": "subscription_active"}


def test_route_diagnostics_are_read_only_and_redact_targets_and_secrets(clean_config, monkeypatch):
    side_effects = []
    monkeypatch.setattr(diagnostics, "_subscription", lambda *_args: side_effects.append("read") or None)
    monkeypatch.setattr(diagnostics.cfg, "NAPCAT_ROUTES", [{"group_id": "NAPCAT_TARGET_CANARY"}])
    monkeypatch.setattr(diagnostics, "_configured_official_bots", lambda: [SimpleNamespace(
        name="BOT_NAME_CANARY",
        remark="BOT_REMARK_CANARY",
        target_openid="OPENID_CANARY",
        group_openid="GROUP_OPENID_CANARY",
        push_message=True,
        member_filter=[],
        client_secret="CLIENT_SECRET_CANARY",
    )])
    monkeypatch.setattr(diagnostics, "_configured_telegram_bots", lambda: [SimpleNamespace(
        name="TG_BOT_NAME_CANARY",
        remark="TG_REMARK_CANARY",
        target_chat="TG_TARGET_CANARY",
        token="TG_TOKEN_CANARY",
        push_message=True,
        member_filter=[],
    )])
    report = _report(token="MEMBER_TOKEN_CANARY", cookies={"session": "COOKIE_CANARY"})
    report_text = str(report)

    assert side_effects == ["read"]
    for secret in (
        "NAPCAT_TARGET_CANARY",
        "OPENID_CANARY",
        "GROUP_OPENID_CANARY",
        "TG_TARGET_CANARY",
        "BOT_NAME_CANARY",
        "BOT_REMARK_CANARY",
        "CLIENT_SECRET_CANARY",
        "TG_TOKEN_CANARY",
        "MEMBER_TOKEN_CANARY",
        "COOKIE_CANARY",
    ):
        assert secret not in report_text
    assert all(route["delivery_status"] == "not_evaluated" for route in report["routes"])


def test_empty_channel_configuration_has_no_applicable_route(clean_config):
    report = _report()
    assert report["routes"] == []
    assert report["route_summary"]["status"] == "no_applicable_route"
    assert report["route_summary"]["applicable_route_count"] == 0


def test_diagnose_monitored_members_uses_supplied_list_or_current_monitor(clean_config, monkeypatch):
    members = [{"name": "One", "id": "1", "account": "fetch-a"}]
    assert len(diagnostics.diagnose_monitored_members(members)) == 1

    monkeypatch.setattr(diagnostics.cfg, "MONITOR_LIST", members)
    assert len(diagnostics.diagnose_monitored_members()) == 1


def test_default_subscription_read_does_not_create_auth_storage_or_migrate_users(clean_config, monkeypatch, tmp_path):
    from src import auth

    db_path = tmp_path / "missing" / "nested" / "auth.db"
    legacy_path = tmp_path / "legacy" / "users.json"
    legacy_path.parent.mkdir()
    legacy_content = b'{"legacy-admin": {"role": "admin"}}'
    legacy_path.write_bytes(legacy_content)
    monkeypatch.setattr(auth, "AUTH_DB_PATH", db_path)
    monkeypatch.setattr(auth, "LEGACY_USERS_JSON", legacy_path)

    monkeypatch.setattr(diagnostics, "_subscription", _DEFAULT_SUBSCRIPTION_READER)
    report = _report()

    assert report["subscription"]["state"] == "unknown"
    assert report["subscription"]["reason"] == "subscription_not_synced_or_unavailable"
    assert not db_path.parent.exists()
    assert not db_path.exists()
    assert legacy_path.read_bytes() == legacy_content


def test_default_subscription_read_opens_existing_database_read_only(clean_config, monkeypatch, tmp_path):
    import sqlite3
    from src import auth

    db_path = tmp_path / "data" / "auth.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE member_subscriptions (
            account_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            state TEXT NOT NULL,
            sub_type TEXT,
            start_at TEXT,
            end_at TEXT,
            auto_renewing INTEGER DEFAULT 0,
            updated_at REAL NOT NULL,
            member_name TEXT
        )
    """)
    conn.execute(
        "INSERT INTO member_subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("fetch-a", "55", "active", "paid", None, None, 1, 1.0, "Member"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(auth, "AUTH_DB_PATH", db_path)

    original_connect = sqlite3.connect
    opened = []

    def read_only_connect(database, *args, **kwargs):
        opened.append((database, kwargs.get("uri")))
        assert kwargs.get("uri") is True
        assert "mode=ro" in database
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(diagnostics.sqlite3, "connect", read_only_connect)
    files_before = {path.name for path in db_path.parent.iterdir()}

    monkeypatch.setattr(diagnostics, "_subscription", _DEFAULT_SUBSCRIPTION_READER)
    report = _report()

    files_after = {path.name for path in db_path.parent.iterdir()}
    assert report["subscription"]["state"] == "active"
    assert report["subscription"]["reason"] == "subscription_active"
    assert opened and opened[0][1] is True
    assert files_after == files_before


@pytest.mark.parametrize("database_state", ["missing_table", "corrupt"])
def test_default_subscription_read_failures_are_unknown(clean_config, monkeypatch, tmp_path, database_state):
    import sqlite3
    from src import auth

    db_path = tmp_path / "data" / "auth.db"
    db_path.parent.mkdir()
    if database_state == "missing_table":
        sqlite3.connect(db_path).close()
    else:
        db_path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(auth, "AUTH_DB_PATH", db_path)

    monkeypatch.setattr(diagnostics, "_subscription", _DEFAULT_SUBSCRIPTION_READER)
    report = _report()

    assert report["subscription"]["state"] == "unknown"
