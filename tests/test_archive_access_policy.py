"""Exercise archive access at the HTTP dispatch boundary, before business work."""

from unittest.mock import Mock

import pytest

import src.config.config as cfg
from src.webui import _Handler
from src.webui_modules import archive_handlers, auth_handlers
from src.webui_modules.archive import messages


READ_ROUTES = (
    "members", "months", "messages", "calendar", "search", "blog_groups",
    "blog_calendar", "blog_authors", "blogs", "gallery", "gallery_members",
    "gallery_years", "media/member/2026/01/images/a.jpg", "blog_media/a.jpg",
)
ADMIN_ROUTES = (
    ("letters", "GET"), ("letters_sync", "POST"),
    ("letters/favorite", "POST"), ("tags", "POST"),
    ("retry_download", "POST"), ("messages/backfill", "POST"),
    ("blogs/archive_groups", "POST"), ("blogs/archive_member", "POST"),
    ("blogs/translate", "POST"), ("blogs/delete_translation", "POST"),
)


@pytest.fixture(autouse=True)
def capture_guard_response(monkeypatch):
    monkeypatch.setattr(auth_handlers, "send_json", lambda h, payload, code=200: h._send_json(payload, code))


def handler(path, method="GET", headers=None):
    h = _Handler.__new__(_Handler)
    h.path, h.command, h.headers = path, method, headers or {}
    h._check_host = lambda: True
    h._check_origin = lambda: True
    h._send_json = Mock()
    h._send_404 = lambda: h._send_json({"ok": False}, 404)
    h._read_body_json = Mock(return_value={})
    return h


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_handlers._auth, "has_users", lambda: True)
    monkeypatch.delenv("WEB_ADMIN_TOKEN", raising=False)

    def set_identity(role, public):
        monkeypatch.setattr(cfg, "AUTH_ARCHIVE_PUBLIC", public)
        user = {"username": "fixture", "role": role} if role else None
        monkeypatch.setattr(auth_handlers, "current_user", lambda _: user)
    return set_identity


@pytest.mark.parametrize("sub,allowed,admin", [
    *((sub, "GET", False) for sub in READ_ROUTES),
    *((sub, method, True) for sub, method in ADMIN_ROUTES),
])
@pytest.mark.parametrize("role", [None, "viewer", "admin"])
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_http_route_role_method_matrix(monkeypatch, identity, sub, allowed, admin, role, public, method):
    identity(role, public)
    reached = Mock()
    monkeypatch.setattr(archive_handlers, "_handle_archive_impl", reached)
    h = handler("/api/archive/" + sub, method)
    getattr(h, "do_" + method)()
    authorized = role == "admin" if admin else bool(role or public)
    if authorized and method == allowed:
        reached.assert_called_once()
    else:
        reached.assert_not_called()
        code = h._send_json.call_args.args[1]
        assert code == (405 if authorized else (403 if role else 401))
        if authorized:
            assert ("Allow", allowed) in h._pending_headers
        h._read_body_json.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_private_home_denied_before_cache(monkeypatch, identity, method):
    identity(None, False)
    cache = Mock(side_effect=AssertionError("Unauthorized cache read"))
    monkeypatch.setattr(archive_handlers._home, "_acquire_home_cache", cache)
    h = handler("/api/archive/home", method)
    getattr(h, "do_" + method)()
    assert h._send_json.call_args.args[1] == 401
    cache.assert_not_called()


@pytest.mark.parametrize("prefix", ["/api/archive/media/", "/media/"])
@pytest.mark.parametrize("rel", ["letters/a.jpg", "%6cetters/a.jpg", "2026/../letters/a.jpg",
                                  "letters%5Ca.jpg", "letters/letters.json"])
@pytest.mark.parametrize("headers,query", [({}, ""), ({"Range": "bytes=0-3"}, ""),
                                          ({"If-None-Match": '"old"'}, "?thumb=1")])
@pytest.mark.parametrize("role", [None, "viewer", "admin"])
@pytest.mark.parametrize("public", [False, True])
def test_letter_media_resource_policy(monkeypatch, identity, tmp_path, prefix, rel, headers, query, role, public):
    identity(role, public)
    member_root = tmp_path / "member"
    (member_root / "letters").mkdir(parents=True)
    (member_root / "letters/a.jpg").write_bytes(b"letter-image")
    (member_root / "letters/letters.json").write_text("[]")
    monkeypatch.setattr(messages._archive, "archive_root", lambda: tmp_path)
    monkeypatch.setattr(messages._archive, "list_members", lambda: ["member"])
    served = Mock()
    monkeypatch.setattr(messages, "serve_file_range", served)
    from src.webui_modules.archive import thumbnails
    thumb = Mock(return_value=member_root / "letters/a.jpg")
    monkeypatch.setattr(thumbnails, "get_or_create_thumbnail", thumb)
    h = handler(prefix + "member/" + rel + query, headers=headers)
    h.do_GET()
    if role == "admin":
        served.assert_called_once()
        assert served.call_args.kwargs["cache_control"] == "private, no-store"
    else:
        served.assert_not_called()
        thumb.assert_not_called()
        assert h._send_json.call_args.args[1] == (403 if role else 401)


@pytest.mark.parametrize("token,provided", [("", ""), ("fixture-token", ""),
                                           ("fixture-token", "fixture-token")])
def test_static_token_and_local_modes(monkeypatch, token, provided):
    monkeypatch.setattr(cfg, "AUTH_ENABLED", False)
    monkeypatch.setenv("WEB_ADMIN_TOKEN", token)
    monkeypatch.setattr(auth_handlers, "current_user", lambda _: None)
    reached = Mock()
    monkeypatch.setattr(archive_handlers, "_handle_archive_impl", reached)
    h = handler("/api/archive/months", headers={"X-Auth-Token": provided})
    h.do_GET()
    assert reached.called == (not token or token == provided)


def test_unknown_archive_endpoint_does_not_dispatch(monkeypatch, identity):
    identity("admin", False)
    reached = Mock()
    monkeypatch.setattr(archive_handlers, "_handle_archive_impl", reached)
    h = handler("/api/archive/not-a-route", "POST")
    h.do_POST()
    reached.assert_not_called()
    assert h._send_json.call_args.args[1] == 404


def test_avatar_alias_keeps_existing_public_contract(monkeypatch, identity):
    identity(None, False)
    reached = Mock()
    monkeypatch.setattr(archive_handlers, "_handle_archive_impl", reached)
    handler("/avatar/?name=fixture").do_GET()
    reached.assert_called_once()
    reached.reset_mock()
    h = handler("/api/archive/avatar?name=fixture")
    h.do_GET()
    reached.assert_not_called()
    assert h._send_json.call_args.args[1] == 401


@pytest.mark.parametrize("sub", ["months", "retry_download"])
def test_token_session_cookie_preserves_access(monkeypatch, sub):
    monkeypatch.setattr(cfg, "AUTH_ENABLED", False)
    monkeypatch.setenv("WEB_ADMIN_TOKEN", "fixture-token")
    monkeypatch.setattr(auth_handlers, "current_user", lambda _: {
        "username": auth_handlers.API_TOKEN_SESSION_USER, "role": "admin",
    })
    reached = Mock()
    monkeypatch.setattr(archive_handlers, "_handle_archive_impl", reached)
    h = handler("/api/archive/" + sub, "GET" if sub == "months" else "POST")
    getattr(h, "do_" + h.command)()
    reached.assert_called_once()


def test_http_wire_permissions_and_method_headers(monkeypatch, tmp_path):
    """Use real serialization/HTTP dispatch and static-token verification."""
    from http.server import ThreadingHTTPServer
    import threading
    import urllib.error
    import urllib.request

    monkeypatch.setattr(cfg, "AUTH_ENABLED", False)
    monkeypatch.setenv("WEB_ADMIN_TOKEN", "fixture-token")
    monkeypatch.setattr(auth_handlers, "current_user", lambda _: None)
    # Override the autouse capture only here so the client sees real HTTP replies.
    from src.webui_modules.static_handler import send_json
    monkeypatch.setattr(auth_handlers, "send_json", send_json)
    monkeypatch.setattr(messages._archive, "list_members", lambda: ["member"])
    monkeypatch.setattr(messages._archive, "list_months", lambda *args, **kwargs: [])
    monkeypatch.setattr(messages._archive, "archive_root", lambda: tmp_path)
    (tmp_path / "member/letters").mkdir(parents=True)
    (tmp_path / "member/letters/a.jpg").write_bytes(b"private-letter")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, method="GET", headers=None):
        req = urllib.request.Request(base + path, method=method, headers=headers or {})
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    try:
        for method in ("GET", "POST", "PUT", "DELETE"):
            assert request("/api/archive/months?member=member", method)[0] == 401
        token = {"X-Auth-Token": "fixture-token"}
        for method in ("POST", "PUT", "DELETE"):
            code, headers, _ = request("/api/archive/months?member=member", method, token)
            assert code == 405
            assert headers["Allow"] == "GET"
        assert request("/api/archive/months?member=member", headers=token)[0] == 200
        for prefix in ("/api/archive/media/", "/media/"):
            path = prefix + "member/letters/a.jpg"
            assert request(path, headers={"Range": "bytes=0-3"})[0] == 401
            code, headers, data = request(path, headers={**token, "Range": "bytes=0-3"})
            assert code == 206
            assert data == b"priv"
            assert headers["Cache-Control"] == "private, no-store"
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()
