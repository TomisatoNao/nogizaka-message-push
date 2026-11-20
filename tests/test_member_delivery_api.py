"""Permission and explicit-decision contracts for held member deliveries."""
from unittest.mock import Mock

import pytest

from src import member_send_intent
from src.delivery_state import DeliveryStateError
from src.webui import _Handler


def handler(method="POST", admin=True, actor="admin"):
    h = _Handler.__new__(_Handler)
    h.path = "/api/member-delivery/resolve" if method == "POST" else "/api/member-delivery/intents"
    h.command = method
    h.headers = {"Host": "127.0.0.1"}
    h._pending_headers = []
    h._send_json = Mock()
    h._check_host = Mock(return_value=True)
    h._check_origin = Mock(return_value=True)
    h._guard = Mock(return_value=admin)
    h._current_user = Mock(return_value={"username": actor} if actor else None)
    h._read_body_json = Mock(return_value={"token": "token", "action": "confirm_delivered",
                                         "reason": "verified", "actor": "forged"})
    return h


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("admin,actor", [(False, "user"), (True, "")])
def test_permission_rejection_never_accesses_intents(monkeypatch, method, admin, actor):
    read = Mock(side_effect=AssertionError("unauthorized read"))
    resolve = Mock(side_effect=AssertionError("unauthorized resolution"))
    monkeypatch.setattr(member_send_intent, "list_protected_send_intents", read)
    monkeypatch.setattr(member_send_intent, "resolve_held_intent", resolve)
    h = handler(method, admin, actor)
    getattr(h, "do_" + method)()
    read.assert_not_called()
    resolve.assert_not_called()
    h._guard.assert_called_once_with(need_admin=True)


@pytest.mark.parametrize("gate", ["_check_host", "_check_origin"])
def test_rejected_origin_or_host_cannot_resolve(monkeypatch, gate):
    resolve = Mock()
    monkeypatch.setattr(member_send_intent, "resolve_held_intent", resolve)
    h = handler()
    getattr(h, gate).return_value = False
    h.do_POST()
    resolve.assert_not_called()


@pytest.mark.parametrize("action,ack,expected", [
    ("authorize_resend", False, 400), ("authorize_resend", True, 200),
    ("confirm_delivered", False, 200),
])
def test_resolution_requires_ack_and_uses_session_actor(monkeypatch, action, ack, expected):
    resolve = Mock(return_value=True)
    monkeypatch.setattr(member_send_intent, "resolve_held_intent", resolve)
    h = handler()
    body = h._read_body_json.return_value
    body.update(action=action, acknowledge_duplicate_risk=ack)
    h.do_POST()
    assert h._send_json.call_args.args[1] == expected
    if expected == 200:
        resolve.assert_called_once_with("token", action, actor="admin", reason="verified")
    else:
        resolve.assert_not_called()


@pytest.mark.parametrize("result,code", [(False, 409), (DeliveryStateError("unavailable"), 503)])
def test_stale_or_failed_resolution_is_not_success(monkeypatch, result, code):
    resolve = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    monkeypatch.setattr(member_send_intent, "resolve_held_intent", resolve)
    h = handler()
    h.do_POST()
    assert h._send_json.call_args.args[1] == code
