"""F2 compatibility reads: old senders must respect protected route states."""
from __future__ import annotations

import asyncio
import sqlite3

import pytest


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """Use a real, reopenable SQLite file without touching the application DB."""
    from src import archive, delivery_state

    db_path = tmp_path / "delivery-state.sqlite3"
    holder = {"path": db_path, "conn": sqlite3.connect(db_path)}
    monkeypatch.setattr(archive, "init_db", lambda: holder["conn"])
    delivery_state._conn()
    try:
        yield holder
    finally:
        holder["conn"].close()


def _seed_protected_route(
    conn: sqlite3.Connection,
    *,
    group_type: str,
    member_id: str,
    message_id: str,
    route_id: str,
    route_state: str = "held",
) -> None:
    """Seed future states directly; production has no F2 state writer yet."""
    conn.execute(
        """
        INSERT INTO delivery_pending_routes (
            group_type, member_id, message_id, route_id, message_time,
            last_error, attempts, first_seen_at, updated_at, route_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            group_type,
            member_id,
            message_id,
            route_id,
            "2026-11-15T10:00:00Z",
            "intent_uncertain",
            4,
            10.0,
            20.0,
            route_state,
        ),
    )
    conn.execute(
        """
        INSERT INTO delivery_lane_state (
            group_type, member_id, route_id, last_success_message_id,
            last_success_time, blocked_message_id, blocked_message_time,
            last_error, consecutive_failures, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            group_type,
            member_id,
            route_id,
            "previously-confirmed",
            "2026-11-15T09:00:00Z",
            message_id,
            "2026-11-15T10:00:00Z",
            "intent_uncertain",
            7,
            30.0,
        ),
    )
    conn.commit()


def _protected_snapshot(conn: sqlite3.Connection, group_type: str, member_id: str, route_id: str):
    pending = conn.execute(
        """
        SELECT message_id, route_state, message_time, last_error, attempts,
               first_seen_at, updated_at
        FROM delivery_pending_routes
        WHERE group_type=? AND member_id=? AND route_id=?
        ORDER BY message_id
        """,
        (group_type, member_id, route_id),
    ).fetchall()
    lane = conn.execute(
        """
        SELECT last_success_message_id, last_success_time, blocked_message_id,
               blocked_message_time, last_error, consecutive_failures, updated_at
        FROM delivery_lane_state
        WHERE group_type=? AND member_id=? AND route_id=?
        """,
        (group_type, member_id, route_id),
    ).fetchone()
    return pending, lane


def test_held_member_routes_reads_protected_states_across_groups(state_db):
    from src import delivery_state

    conn = state_db["conn"]
    _seed_protected_route(
        conn,
        group_type="group-a",
        member_id="shared-id",
        message_id="held-message",
        route_id="napcat:123",
        route_state="held",
    )
    _seed_protected_route(
        conn,
        group_type="group-b",
        member_id="shared-id",
        message_id="flight-message",
        route_id="tg:main",
        route_state="in_flight",
    )
    _seed_protected_route(
        conn,
        group_type="group-c",
        member_id="shared-id",
        message_id="future-message",
        route_id="official:future",
        route_state="future_unknown",
    )
    conn.execute(
        """
        INSERT INTO delivery_pending_routes (
            group_type, member_id, message_id, route_id, message_time,
            attempts, first_seen_at, updated_at, route_state
        ) VALUES ('group-a', 'shared-id', 'pending-message', 'tg:pending',
                  '2026-11-15T11:00:00Z', 0, 1, 1, 'pending')
        """
    )
    conn.execute(
        """
        INSERT INTO delivery_pending_routes (
            group_type, member_id, message_id, route_id, message_time,
            attempts, first_seen_at, updated_at, route_state
        ) VALUES ('group-a', 'shared-id', 'suspended-message', 'tg:suspended',
                  '2026-11-15T11:00:00Z', 0, 1, 1, 'suspended')
        """
    )
    conn.commit()

    assert delivery_state.held_member_routes("group-a", "shared-id") == {"napcat:123"}
    # An absent group deliberately searches all matching member IDs.
    assert delivery_state.held_member_routes(None, "shared-id") == {
        "napcat:123",
        "tg:main",
        "official:future",
    }
    assert delivery_state.held_member_routes(None, "no-such-member") == set()


def test_held_state_survives_reopen_and_route_disable_enable(state_db):
    from src import delivery_state

    conn = state_db["conn"]
    _seed_protected_route(
        conn,
        group_type="group-a",
        member_id="member-1",
        message_id="message-1",
        route_id="napcat:123",
        route_state="held",
    )
    before = _protected_snapshot(conn, "group-a", "member-1", "napcat:123")

    assert delivery_state.reconcile_pending_routes("group-a", "member-1", set()) == {
        "suspended": 0,
        "resumed": 0,
    }
    assert _protected_snapshot(conn, "group-a", "member-1", "napcat:123") == before

    conn.close()
    state_db["conn"] = sqlite3.connect(state_db["path"])
    reopened = state_db["conn"]
    assert delivery_state.held_member_routes("group-a", "member-1") == {"napcat:123"}

    assert delivery_state.reconcile_pending_routes(
        "group-a", "member-1", {"napcat:123"}
    ) == {"suspended": 0, "resumed": 0}
    assert _protected_snapshot(reopened, "group-a", "member-1", "napcat:123") == before


def test_mark_pending_does_not_rewrite_protected_route_or_lane(state_db):
    from src import delivery_state

    conn = state_db["conn"]
    _seed_protected_route(
        conn,
        group_type="group-a",
        member_id="member-1",
        message_id="message-1",
        route_id="napcat:123",
        route_state="held",
    )
    before = _protected_snapshot(conn, "group-a", "member-1", "napcat:123")

    delivery_state.mark_pending_routes(
        "group-a",
        "member-1",
        "message-1",
        "2026-11-15T12:00:00Z",
        {"napcat:123"},
        errors={"napcat:123": "delivery_failed"},
        attempted_route_ids={"napcat:123"},
    )
    # A later ordinary failure on the lane must not replace its held marker,
    # error, blocked pointer, or failure count either.
    delivery_state.mark_pending_routes(
        "group-a",
        "member-1",
        "message-2",
        "2026-11-15T13:00:00Z",
        {"napcat:123"},
        errors={"napcat:123": "another_failure"},
        attempted_route_ids={"napcat:123"},
    )

    pending, lane = _protected_snapshot(conn, "group-a", "member-1", "napcat:123")
    assert pending[0] == before[0][0]
    assert lane == before[1]


def test_mark_successful_rejects_any_protected_message_on_lane(state_db):
    from src import delivery_state

    conn = state_db["conn"]
    _seed_protected_route(
        conn,
        group_type="group-a",
        member_id="member-1",
        message_id="older-held-message",
        route_id="napcat:123",
        route_state="held",
    )
    conn.execute(
        "INSERT INTO delivered_routes VALUES (?, ?, ?, ?, ?)",
        ("group-a", "member-1", "previously-confirmed", "napcat:123", 1.0),
    )
    conn.commit()
    before = _protected_snapshot(conn, "group-a", "member-1", "napcat:123")

    with pytest.raises(delivery_state.DeliveryStateError):
        delivery_state.mark_successful_routes(
            "group-a", "member-1", "newer-message", {"napcat:123"}
        )

    assert conn.execute(
        "SELECT message_id FROM delivered_routes WHERE group_type=? AND member_id=? AND route_id=? ORDER BY message_id",
        ("group-a", "member-1", "napcat:123"),
    ).fetchall() == [("previously-confirmed",)]
    assert _protected_snapshot(conn, "group-a", "member-1", "napcat:123") == before


def test_held_member_routes_reports_required_state_read_failure(monkeypatch):
    from src import delivery_state

    monkeypatch.setattr(delivery_state, "_conn", lambda: None)
    with pytest.raises(delivery_state.DeliveryStateError):
        delivery_state.held_member_routes("group-a", "member-1")


class _OfficialBot:
    name = "official-test"
    remark = "official test"
    target_openid = "official-openid"
    group_openid = ""
    push_message = True
    member_filter: list[str] = []

    def __init__(self, calls):
        self.calls = calls

    async def send_message_chain(self, *_args, **_kwargs):
        self.calls["official_send"] += 1
        return True


class _TelegramBot:
    name = "main"
    remark = "main"
    target_chat = "-100123"
    push_message = True
    member_filter: list[str] = []

    async def send_member_message(self, _message_chain):
        self.calls["telegram_send"] += 1
        first_text = _message_chain[0].get("data", {}).get("text", "")
        self.messages.append(first_text)
        return True

    def __init__(self, calls):
        self.calls = calls
        self.messages = []


def _configure_channels(monkeypatch):
    from src import health, notifier

    calls = {
        "napcat_send": 0,
        "media_prepare": 0,
        "official_send": 0,
        "telegram_send": 0,
    }
    tracker = health.HealthTracker()
    tracker.initialize()
    monkeypatch.setattr(notifier.health, "get_tracker", lambda: tracker)
    monkeypatch.setattr(notifier.cfg, "ENABLE_NAPCAT_QQ", True)
    monkeypatch.setattr(notifier.cfg, "NAPCAT_ROUTES", [
        {"group_id": 123, "push_message": True},
    ])
    monkeypatch.setattr(notifier.cfg, "ENABLE_QQ_OFFICIAL_BOT", True)
    monkeypatch.setattr(notifier.cfg, "ENABLE_TG_BOT", True)
    official = _OfficialBot(calls)
    telegram = _TelegramBot(calls)
    monkeypatch.setattr(notifier, "get_configured_bots", lambda: [official])
    monkeypatch.setattr(notifier.tgbot, "get_configured_bots", lambda: [telegram])

    async def napcat_send(*_args, **_kwargs):
        calls["napcat_send"] += 1
        return True

    async def prepare_media(*_args, **_kwargs):
        calls["media_prepare"] += 1
        return []

    monkeypatch.setattr(notifier, "send_qq_message", napcat_send)
    monkeypatch.setattr(notifier.qq_official, "download_media_payloads", prepare_media)
    return notifier, calls


@pytest.mark.asyncio
async def test_detailed_sender_holds_old_routes_before_network_or_media_prep(
    state_db, monkeypatch
):
    notifier, calls = _configure_channels(monkeypatch)
    _seed_protected_route(
        state_db["conn"],
        group_type="old-group",
        member_id="shared-id",
        message_id="message-1",
        route_id="napcat:123",
        route_state="held",
    )
    _seed_protected_route(
        state_db["conn"],
        group_type="old-group",
        member_id="shared-id",
        message_id="message-0",
        route_id="official:official-test:private",
        route_state="in_flight",
    )

    report = await notifier.send_member_message_detailed(
        {"m_name": "Test member", "m_id": "shared-id", "group_type": None},
        [{"type": "image", "data": {"file": "https://example.test/image.jpg"}}],
    )

    attempts = {attempt.route_id: attempt for attempt in report.attempts}
    assert report.matched_route_ids == (
        "napcat:123",
        "official:official-test:private",
        "tg:main",
    )
    assert attempts["napcat:123"].error_code == "delivery_held"
    assert attempts["official:official-test:private"].error_code == "delivery_held"
    assert attempts["tg:main"].ok is True
    assert report.attempted_count == 1
    assert calls == {
        "napcat_send": 0,
        "media_prepare": 0,
        "official_send": 0,
        "telegram_send": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["unavailable", "oserror"])
async def test_unavailable_state_fails_every_candidate_without_side_effects(
    monkeypatch, failure_kind
):
    from src import delivery_state

    notifier, calls = _configure_channels(monkeypatch)

    def unavailable_connection():
        if failure_kind == "oserror":
            raise OSError("database file is unavailable")
        return None

    monkeypatch.setattr(delivery_state, "_conn", unavailable_connection)

    report = await notifier.send_member_message_detailed(
        {"m_name": "Test member", "m_id": "member-1", "group_type": "group-a"},
        [{"type": "image", "data": {"file": "https://example.test/image.jpg"}}],
    )

    assert {attempt.route_id for attempt in report.attempts} == {
        "napcat:123",
        "official:official-test:private",
        "tg:main",
    }
    assert {attempt.error_code for attempt in report.attempts} == {
        "delivery_state_unavailable"
    }
    assert calls == {
        "napcat_send": 0,
        "media_prepare": 0,
        "official_send": 0,
        "telegram_send": 0,
    }


@pytest.mark.asyncio
async def test_missing_delivery_identity_fails_closed_before_side_effects(monkeypatch):
    notifier, calls = _configure_channels(monkeypatch)

    report = await notifier.send_member_message_detailed(
        {"m_name": "Test member", "group_type": "group-a"},
        [{"type": "image", "data": {"file": "https://example.test/image.jpg"}}],
    )

    assert {attempt.error_code for attempt in report.attempts} == {
        "delivery_identity_missing"
    }
    assert calls == {
        "napcat_send": 0,
        "media_prepare": 0,
        "official_send": 0,
        "telegram_send": 0,
    }


@pytest.mark.asyncio
async def test_legacy_bool_is_false_when_all_matched_routes_are_held(state_db, monkeypatch):
    notifier, calls = _configure_channels(monkeypatch)
    monkeypatch.setattr(notifier.cfg, "ENABLE_QQ_OFFICIAL_BOT", False)
    monkeypatch.setattr(notifier.cfg, "ENABLE_TG_BOT", False)
    _seed_protected_route(
        state_db["conn"],
        group_type="group-a",
        member_id="member-1",
        message_id="message-1",
        route_id="napcat:123",
        route_state="held",
    )

    ok = await notifier.send_member_message(
        {"m_name": "Test member", "m_id": "member-1", "group_type": "group-a"},
        [{"type": "text", "data": {"text": "hello"}}],
    )

    assert ok is False
    assert calls["napcat_send"] == 0


@pytest.mark.asyncio
async def test_explicit_skip_route_ids_still_suppress_candidates(state_db, monkeypatch):
    notifier, calls = _configure_channels(monkeypatch)
    _seed_protected_route(
        state_db["conn"],
        group_type="group-a",
        member_id="member-1",
        message_id="message-1",
        route_id="napcat:123",
        route_state="held",
    )

    report = await notifier.send_member_message_detailed(
        {"m_name": "Test member", "m_id": "member-1", "group_type": "group-a"},
        [{"type": "text", "data": {"text": "hello"}}],
        skip_route_ids={"napcat:123", "official:official-test:private", "tg:main"},
    )

    assert report.matched_route_ids == (
        "napcat:123",
        "official:official-test:private",
        "tg:main",
    )
    assert report.attempts == ()
    assert calls == {
        "napcat_send": 0,
        "media_prepare": 0,
        "official_send": 0,
        "telegram_send": 0,
    }


@pytest.mark.asyncio
async def test_blank_group_and_padded_member_id_use_wildcard_lookup(state_db, monkeypatch):
    notifier, calls = _configure_channels(monkeypatch)
    monkeypatch.setattr(notifier.cfg, "ENABLE_QQ_OFFICIAL_BOT", False)
    monkeypatch.setattr(notifier.cfg, "ENABLE_TG_BOT", False)
    _seed_protected_route(
        state_db["conn"],
        group_type="group-a",
        member_id="shared-id",
        message_id="message-1",
        route_id="napcat:123",
        route_state="in_flight",
    )

    report = await notifier.send_member_message_detailed(
        {"m_name": "Test member", "m_id": " shared-id ", "group_type": "   "},
        [{"type": "text", "data": {"text": "hello"}}],
    )

    assert len(report.attempts) == 1
    assert report.attempts[0].route_id == "napcat:123"
    assert report.attempts[0].error_code == "delivery_held"
    assert calls["napcat_send"] == 0


@pytest.mark.asyncio
async def test_fetcher_continues_healthy_lane_in_order_while_held_qq_stays_idle(
    state_db, monkeypatch
):
    from src import archive, delivery_state, fetcher

    notifier, calls = _configure_channels(monkeypatch)
    telegram = notifier.tgbot.get_configured_bots()[0]
    monkeypatch.setattr(notifier.cfg, "ENABLE_QQ_OFFICIAL_BOT", False)
    monkeypatch.setattr(fetcher.cfg, "QQ_SEND_INTERVAL", 0, raising=False)
    monkeypatch.setattr(fetcher, "translate_text_with_model", lambda *_args: ("", ""))

    async def archive_message(*_args, **_kwargs):
        return None

    monkeypatch.setattr(archive, "archive_message", archive_message)
    monkeypatch.setattr(archive, "set_timeline_watermark", lambda *_args: None)
    saved_ids = []
    monkeypatch.setattr(
        fetcher,
        "save_sent_id",
        lambda _g, _m, message_id, *_args: saved_ids.append(str(message_id)),
    )
    monkeypatch.setattr(fetcher, "write_time_record", lambda *_args: asyncio.sleep(0))
    monkeypatch.setattr(fetcher.cfg, "ENABLE_TRANSLATION", False, raising=False)
    member = {
        "m_name": "Test member",
        "m_id": "member-1",
        "group_type": "group-a",
    }
    _seed_protected_route(
        state_db["conn"],
        group_type="group-a",
        member_id="member-1",
        message_id="message-0",
        route_id="napcat:123",
        route_state="future_unknown",
    )
    messages = [
        {"id": "message-1", "updated_at": "2026-11-15T10:01:00Z", "text": "one"},
        {"id": "message-2", "updated_at": "2026-11-15T10:02:00Z", "text": "two"},
    ]

    result = await fetcher._push_member_messages(
        member, messages, [], set(), ["2026-11-15T10:00:00Z"], "", None
    )

    assert result is False
    assert calls["napcat_send"] == 0
    assert calls["telegram_send"] == 2
    assert [message.partition("\n")[2] for message in telegram.messages] == ["one", "two"]
    assert saved_ids == []
    assert state_db["conn"].execute(
        "SELECT message_id, route_state, attempts FROM delivery_pending_routes "
        "WHERE group_type='group-a' AND member_id='member-1' AND route_id='napcat:123' "
        "ORDER BY message_id"
    ).fetchall() == [
        ("message-0", "future_unknown", 4),
        ("message-1", "pending", 0),
        ("message-2", "pending", 0),
    ]
    assert delivery_state.successful_routes("group-a", "member-1", "message-1") == {
        "tg:main"
    }
    assert delivery_state.successful_routes("group-a", "member-1", "message-2") == {
        "tg:main"
    }


def test_send_intent_survives_reopen_and_blocks_newer_messages(state_db):
    from src.delivery_state import held_member_routes, successful_routes
    from src.member_send_intent import begin_send_intent, finish_send_intent

    first = ("group", "member", "1", "napcat:123", "2026-11-19T00:00:00Z")
    later = ("group", "member", "2", "napcat:123", "2026-11-19T00:01:00Z")
    token = begin_send_intent(*first)
    assert token
    state_db["conn"].close()
    state_db["conn"] = sqlite3.connect(state_db["path"])
    assert held_member_routes("group", "member") == {"napcat:123"}
    assert begin_send_intent(*first) is None
    assert begin_send_intent(*later) is None
    assert finish_send_intent(token, "not_submitted")
    assert begin_send_intent(*later) is None
    retry = begin_send_intent(*first)
    assert retry and retry != token
    assert not finish_send_intent(token, "confirmed")
    assert finish_send_intent(retry, "confirmed")
    assert successful_routes("group", "member", "1") == {"napcat:123"}
    assert begin_send_intent(*first) is None
    token = begin_send_intent(*later)
    assert token and finish_send_intent(token, "unconfirmed")
    assert begin_send_intent(*later) is None


def test_receipt_write_failure_preserves_send_intent(state_db):
    from src.delivery_state import DeliveryStateError, held_member_routes
    from src.member_send_intent import begin_send_intent, finish_send_intent

    token = begin_send_intent("group", "member", "1", "napcat:123", "2026-11-19T00:00:00Z")
    state_db["conn"].execute("""
        CREATE TRIGGER reject_receipt BEFORE INSERT ON delivered_routes
        BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END
    """)
    with pytest.raises(DeliveryStateError):
        finish_send_intent(token, "confirmed")
    assert held_member_routes("group", "member") == {"napcat:123"}

@pytest.mark.parametrize("behavior", ["cancel", "timeout", "success"])
@pytest.mark.asyncio
async def test_member_notifier_persists_intent_before_sending(state_db, monkeypatch, behavior):
    from src.delivery_state import held_member_routes, successful_routes

    notifier, calls = _configure_channels(monkeypatch)
    monkeypatch.setattr(notifier.cfg, "ENABLE_QQ_OFFICIAL_BOT", False)
    monkeypatch.setattr(notifier.cfg, "ENABLE_TG_BOT", False)
    member = {"group_type": "group", "m_id": "member", "m_name": "Member"}
    identity = ("group", "member", "1", "2026-11-19T00:00:00Z")

    async def send(*args):
        calls["napcat_send"] += 1
        assert held_member_routes("group", "member") == {"napcat:123"}
        if behavior == "cancel":
            raise asyncio.CancelledError()
        if behavior == "timeout":
            raise asyncio.TimeoutError()
        return True

    monkeypatch.setattr(notifier, "send_qq_message", send)
    if behavior == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await notifier.send_member_message_detailed(member, [], intent_identity=identity)
    else:
        report = await notifier.send_member_message_detailed(member, [], intent_identity=identity)
        assert report.ok == (behavior == "success")
        if behavior == "timeout":
            assert report.attempts[0].error_code == "delivery_unconfirmed"
    state_db["conn"].close()
    state_db["conn"] = sqlite3.connect(state_db["path"])
    await notifier.send_member_message_detailed(member, [], intent_identity=identity)
    assert calls["napcat_send"] == 1
    assert bool(successful_routes("group", "member", "1")) == (behavior == "success")

@pytest.mark.parametrize("action", ["confirm_delivered", "authorize_resend"])
def test_admin_resolution_is_audited_and_cannot_repeat(state_db, action):
    from src.delivery_state import successful_routes
    from src.member_send_intent import begin_send_intent, finish_send_intent, resolve_held_intent

    identity = ("group", "member", "message", "napcat:123", "2026-11-19T00:00:00Z")
    token = begin_send_intent(*identity)
    assert not resolve_held_intent(token, action, actor="admin", reason="Checked group history")
    assert finish_send_intent(token, "unconfirmed")
    assert resolve_held_intent(token, action, actor="admin", reason="Checked group history")
    assert not resolve_held_intent(token, action, actor="admin", reason="Repeated request")
    assert state_db["conn"].execute("SELECT actor, action FROM member_send_resolutions").fetchall() == [
        ("admin", action),
    ]
    assert bool(successful_routes(*identity[:3])) == (action == "confirm_delivered")
    assert bool(begin_send_intent(*identity)) == (action == "authorize_resend")


def test_protected_intents_list_tracks_active_and_resolved_attempts(state_db):
    from src.member_send_intent import begin_send_intent, finish_send_intent
    from src.member_send_intent import list_protected_send_intents, resolve_held_intent

    assert list_protected_send_intents() == []
    token = begin_send_intent("group", "member", "message", "napcat:123", "2026-11-19T00:00:00Z")
    assert list_protected_send_intents()[0]["state"] == "in_flight"
    assert finish_send_intent(token, "unconfirmed")
    assert list_protected_send_intents()[0]["state"] == "held"
    assert resolve_held_intent(token, "confirm_delivered", actor="admin", reason="Verified receipt")
    assert list_protected_send_intents() == []
