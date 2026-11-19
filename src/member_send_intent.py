"""Durable member send intents, shared with the existing protected-lane reader."""
import sqlite3
import time
from uuid import uuid4

from src.delivery_state import DeliveryStateError, _conn


def begin_send_intent(group, member, message, route, message_time):
    """Commit an exclusive send intent, or return None if the lane cannot send."""
    try:
        conn = _conn()
        if conn is None:
            raise DeliveryStateError("投递数据库不可用")
        key = (group, member, message, route)
        now = time.time()
        token = uuid4().hex
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                """SELECT 1 FROM delivered_routes
                   WHERE group_type=? AND member_id=? AND message_id=? AND route_id=?""",
                key,
            ).fetchone():
                return None
            if conn.execute(
                """SELECT 1 FROM delivery_pending_routes
                   WHERE group_type=? AND member_id=? AND route_id=?
                     AND (route_state NOT IN ('pending', 'suspended')
                       OR (route_state='pending' AND (message_time, message_id) < (?, ?)))""",
                (group, member, route, message_time, message),
            ).fetchone():
                return None
            claimed = conn.execute(
                """INSERT INTO delivery_pending_routes
                   (group_type, member_id, message_id, route_id, message_time,
                    first_seen_at, updated_at, route_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'in_flight')
                   ON CONFLICT(group_type, member_id, message_id, route_id)
                   DO UPDATE SET route_state='in_flight', updated_at=excluded.updated_at
                   WHERE delivery_pending_routes.route_state='pending'
                     AND delivery_pending_routes.message_time=excluded.message_time""",
                (*key, message_time, now, now),
            ).rowcount
            if not claimed:
                return None
            conn.execute(
                """CREATE TABLE IF NOT EXISTS member_send_intents (
                   token TEXT PRIMARY KEY, group_type TEXT NOT NULL, member_id TEXT NOT NULL,
                   message_id TEXT NOT NULL, route_id TEXT NOT NULL,
                   started_at REAL NOT NULL, outcome TEXT, finished_at REAL)"""
            )
            conn.execute(
                "INSERT INTO member_send_intents VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (token, *key, now),
            )
        return token
    except (sqlite3.Error, OSError) as exc:
        raise DeliveryStateError("无法持久化发送意图") from exc


def finish_send_intent(token, outcome):
    """Commit a receipt, proven non-submission, or a hold under the same token."""
    if outcome not in {"confirmed", "not_submitted", "unconfirmed"}:
        raise ValueError("Invalid send outcome")
    try:
        conn = _conn()
        if conn is None:
            raise DeliveryStateError("投递数据库不可用")
        now = time.time()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            key = conn.execute(
                """SELECT group_type, member_id, message_id, route_id
                   FROM member_send_intents WHERE token=? AND outcome IS NULL""",
                (token,),
            ).fetchone()
            if key is None:
                return False
            if outcome == "confirmed":
                changed = conn.execute(
                    """DELETE FROM delivery_pending_routes WHERE group_type=?
                       AND member_id=? AND message_id=? AND route_id=? AND route_state='in_flight'""",
                    tuple(key),
                ).rowcount
                if changed:
                    conn.execute("INSERT OR IGNORE INTO delivered_routes VALUES (?, ?, ?, ?, ?)", (*key, now))
            else:
                state = "pending" if outcome == "not_submitted" else "held"
                error = "delivery_unconfirmed" if outcome == "unconfirmed" else None
                changed = conn.execute(
                    """UPDATE delivery_pending_routes SET route_state=?, last_error=?, updated_at=?
                       WHERE group_type=? AND member_id=? AND message_id=? AND route_id=?
                         AND route_state='in_flight'""",
                    (state, error, now, *key),
                ).rowcount
            if not changed:
                return False
            conn.execute(
                "UPDATE member_send_intents SET outcome=?, finished_at=? WHERE token=?",
                (outcome, now, token),
            )
        return True
    except (sqlite3.Error, OSError) as exc:
        raise DeliveryStateError("无法持久化发送结果") from exc


def resolve_held_intent(token, action, *, actor, reason):
    """Resolve a held attempt and record the decision in the same transaction."""
    if action not in {"confirm_delivered", "authorize_resend"} or not actor.strip() or not reason.strip() or len(reason) > 500:
        raise ValueError("Invalid resolution")
    try:
        conn = _conn()
        if conn is None:
            raise DeliveryStateError("投递数据库不可用")
        now = time.time()
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='member_send_intents'").fetchone():
                return False
            key = conn.execute(
                """SELECT group_type, member_id, message_id, route_id
                   FROM member_send_intents WHERE token=? AND outcome='unconfirmed'""",
                (token,),
            ).fetchone()
            if key is None:
                return False
            where = """WHERE group_type=? AND member_id=? AND message_id=? AND route_id=?
                       AND route_state='held'"""
            if action == "confirm_delivered":
                changed = conn.execute("DELETE FROM delivery_pending_routes " + where, tuple(key)).rowcount
                if changed:
                    conn.execute("INSERT OR IGNORE INTO delivered_routes VALUES (?, ?, ?, ?, ?)", (*key, now))
            else:
                changed = conn.execute(
                    """UPDATE delivery_pending_routes SET route_state='pending',
                       last_error=NULL, updated_at=? """ + where,
                    (now, *key),
                ).rowcount
            if not changed:
                return False
            conn.execute(
                """CREATE TABLE IF NOT EXISTS member_send_resolutions (
                   token TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
                   reason TEXT NOT NULL, resolved_at REAL NOT NULL)"""
            )
            conn.execute("INSERT INTO member_send_resolutions VALUES (?, ?, ?, ?, ?)", (token, actor, action, reason.strip(), now))
            conn.execute("UPDATE member_send_intents SET outcome='resolved' WHERE token=?", (token,))
        return True
    except (sqlite3.Error, OSError) as exc:
        raise DeliveryStateError("无法保存投递核实结果") from exc


def list_protected_send_intents(limit=100):
    """Return actionable intent metadata without message content or credentials."""
    try:
        conn = _conn()
        if conn is None:
            raise DeliveryStateError("投递数据库不可用")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='member_send_intents'").fetchone():
            return []
        rows = conn.execute(
            """SELECT i.token, i.group_type, i.member_id, i.message_id, i.route_id,
                      i.started_at, p.route_state
               FROM member_send_intents i JOIN delivery_pending_routes p
                 USING (group_type, member_id, message_id, route_id)
               WHERE (i.outcome IS NULL AND p.route_state='in_flight')
                  OR (i.outcome='unconfirmed' AND p.route_state='held')
               ORDER BY i.started_at, i.token LIMIT ?""",
            (max(1, min(500, limit)),),
        )
        fields = ("token", "group_type", "member_id", "message_id", "route_id", "started_at", "state")
        return [dict(zip(fields, row)) for row in rows]
    except (sqlite3.Error, OSError) as exc:
        raise DeliveryStateError("无法读取待核实投递") from exc


def recover_interrupted_send_intents():
    """Run after exclusive process ownership, before starting send workers."""
    try:
        conn = _conn()
        if conn is None:
            raise DeliveryStateError("投递数据库不可用")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='member_send_intents'").fetchone():
                return 0
            rows = conn.execute(
                """SELECT token, group_type, member_id, message_id, route_id
                   FROM member_send_intents WHERE outcome IS NULL"""
            ).fetchall()
            recovered = 0
            for token, group, member, message, route in rows:
                changed = conn.execute(
                    """UPDATE delivery_pending_routes
                       SET route_state='held', last_error='delivery_unconfirmed', updated_at=?
                       WHERE group_type=? AND member_id=? AND message_id=? AND route_id=?
                         AND route_state='in_flight'""",
                    (time.time(), group, member, message, route),
                ).rowcount
                if changed:
                    conn.execute(
                        """UPDATE member_send_intents SET outcome='unconfirmed', finished_at=?
                           WHERE token=? AND outcome IS NULL""",
                        (time.time(), token),
                    )
                    recovered += 1
        return recovered
    except (sqlite3.Error, OSError) as exc:
        raise DeliveryStateError("无法恢复中断的发送意图") from exc
