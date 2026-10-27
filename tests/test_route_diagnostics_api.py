"""API contract tests for the admin-only, read-only route diagnostics endpoint."""

from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

from src import route_diagnostics
from src.webui import _Handler
from src.webui_modules import system_handlers


def _handler(method: str, *, admin: bool = True):
    handler = _Handler.__new__(_Handler)
    handler.path = "/api/route-diagnostics"
    handler.command = method
    handler.headers = {"Host": "127.0.0.1"}
    handler._pending_headers = []
    handler._send_json = Mock()
    handler._send_404 = Mock()
    handler._read_body_json = Mock(return_value={})
    handler._check_host = Mock(return_value=True)
    handler._check_origin = Mock(return_value=True)

    def guard(*, need_admin=True, **_kwargs):
        assert need_admin is True
        if admin:
            return True
        handler._send_json({"ok": False, "error": "forbidden"}, 403)
        return False

    handler._guard = Mock(side_effect=guard)
    return handler


def test_route_diagnostics_get_is_admin_only(monkeypatch):
    monkeypatch.setattr(system_handlers, "send_json", lambda h, body, code=200: h._send_json(body, code))
    diagnose = Mock(side_effect=AssertionError("unauthorized route must not read diagnostics"))
    monkeypatch.setattr(route_diagnostics, "diagnose_monitored_members", diagnose)

    handler = _handler("GET", admin=False)
    handler.do_GET()

    handler._guard.assert_called_once_with(need_admin=True)
    assert handler._send_json.call_args.args == ({"ok": False, "error": "forbidden"}, 403)
    diagnose.assert_not_called()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_route_diagnostics_rejects_other_defined_methods(monkeypatch, method):
    diagnose = Mock(side_effect=AssertionError("wrong method must not read diagnostics"))
    monkeypatch.setattr(route_diagnostics, "diagnose_monitored_members", diagnose)
    handler = _handler(method)

    getattr(handler, f"do_{method}")()

    handler._guard.assert_called_once_with(need_admin=True)
    assert handler._send_json.call_args.args[1] == 405
    assert ("Allow", "GET") in handler._pending_headers
    handler._read_body_json.assert_not_called()
    diagnose.assert_not_called()


def test_admin_api_returns_generic_route_labels_without_targets_or_side_effects(monkeypatch):
    cfg = route_diagnostics.cfg
    monkeypatch.setattr(cfg, "MONITOR_LIST", [
        {"name": "Member Canary", "id": "55", "account": "fetch-a"},
    ], raising=False)
    monkeypatch.setattr(cfg, "ACCOUNTS", {"fetch-a": {"group_type": "nogizaka46"}}, raising=False)
    monkeypatch.setattr(cfg, "ENABLE_NAPCAT_QQ", True, raising=False)
    monkeypatch.setattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", True, raising=False)
    monkeypatch.setattr(cfg, "ENABLE_TG_BOT", True, raising=False)
    monkeypatch.setattr(cfg, "NAPCAT_ROUTES", [{"group_id": "NAPCAT_TARGET_CANARY"}], raising=False)
    monkeypatch.setattr(route_diagnostics, "_configured_official_bots", lambda: [SimpleNamespace(
        name="OFFICIAL_BOT_CANARY",
        remark="OFFICIAL_REMARK_CANARY",
        target_openid="OFFICIAL_TARGET_CANARY",
        group_openid="OFFICIAL_GROUP_CANARY",
        client_secret="OFFICIAL_SECRET_CANARY",
        push_message=True,
        member_filter=[],
    )])
    monkeypatch.setattr(route_diagnostics, "_configured_telegram_bots", lambda: [SimpleNamespace(
        name="TELEGRAM_BOT_CANARY",
        remark="TELEGRAM_REMARK_CANARY",
        target_chat="TELEGRAM_TARGET_CANARY",
        token="TELEGRAM_SECRET_CANARY",
        push_message=True,
        member_filter=[],
    )])
    subscription_reads = []
    monkeypatch.setattr(
        route_diagnostics,
        "_subscription",
        lambda account, member: subscription_reads.append((account, member)) or None,
    )

    network_calls = []
    monkeypatch.setattr(
        system_handlers.httpx,
        "AsyncClient",
        lambda *args, **kwargs: network_calls.append((args, kwargs)),
    )
    forbidden = Mock(side_effect=AssertionError("route diagnostics invoked another operation"))
    from src import webui
    monkeypatch.setattr(webui, "_sys_handle_members", forbidden)
    monkeypatch.setattr(webui, "_social_handle_subscriptions_sync", forbidden)
    monkeypatch.setattr(webui, "_sys_handle_test_push", forbidden)
    monkeypatch.setattr(system_handlers, "send_json", lambda h, body, code=200: h._send_json(body, code))

    handler = _handler("GET")
    handler.do_GET()

    body = handler._send_json.call_args.args[0]
    assert handler._send_json.call_args.args[1] == 200
    assert body["ok"] is True
    assert subscription_reads == [("fetch-a", "55")]
    assert network_calls == []
    forbidden.assert_not_called()

    member = body["diagnostics"][0]
    assert [route["label"] for route in member["routes"]] == [
        "NapCat route 1",
        "Official bot 1 private",
        "Official bot 1 group",
        "Telegram bot 1",
    ]
    encoded = json.dumps(body, ensure_ascii=False)
    for private_value in (
        "NAPCAT_TARGET_CANARY",
        "OFFICIAL_TARGET_CANARY",
        "OFFICIAL_GROUP_CANARY",
        "TELEGRAM_TARGET_CANARY",
        "OFFICIAL_SECRET_CANARY",
        "TELEGRAM_SECRET_CANARY",
    ):
        assert private_value not in encoded

    # Remarks are safely preserved so administrators can distinguish configured bots
    assert member["routes"][1]["remark"] == "OFFICIAL_REMARK_CANARY"
    assert member["routes"][1]["target_type"] == "private"
    assert member["routes"][2]["remark"] == "OFFICIAL_REMARK_CANARY"
    assert member["routes"][2]["target_type"] == "group"
    assert member["routes"][3]["remark"] == "TELEGRAM_REMARK_CANARY"
    assert member["routes"][3]["target_type"] == "chat"
