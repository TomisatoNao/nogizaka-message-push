from pathlib import Path
import sys
import json
from io import BytesIO
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.webui_modules import (  # noqa: E402
    archive_handlers,
    auth_handlers,
    config_service,
    media_service,
    social_handlers,
    static_handler,
    system_handlers,
)


def test_static_handler_mime():
    assert "theme.css" in static_handler._STATIC_MIME
    assert static_handler._STATIC_MIME["theme.css"] == "text/css"


class _ResponseHandler:
    def __init__(self, accept_encoding: str = ""):
        self.headers = {"Accept-Encoding": accept_encoding}
        self.sent_headers: list[tuple[str, str]] = []
        self.wfile = BytesIO()
        self._pending_headers = []
        self._pending_set_cookies = []

    def send_response(self, _code):
        pass

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def test_static_handler_respects_gzip_quality_and_consumes_pending_headers():
    handler = _ResponseHandler("gzip;q=0")
    data, headers = static_handler.compress_if_supported(handler, b"x" * 1024)
    assert data == b"x" * 1024
    assert headers == {}

    handler._pending_headers = [("Clear-Site-Data", '"cache"')]
    handler._pending_set_cookies = ["session=abc; HttpOnly"]
    static_handler.send_json(handler, {"ok": True})
    assert ("Vary", "Accept-Encoding") in handler.sent_headers
    assert ("Clear-Site-Data", '"cache"') in handler.sent_headers
    assert ("Set-Cookie", "session=abc; HttpOnly") in handler.sent_headers

    handler.sent_headers.clear()
    static_handler.send_json(handler, {"ok": True})
    assert not any(key in {"Clear-Site-Data", "Set-Cookie"} for key, _ in handler.sent_headers)


def test_system_handlers_env():
    status = system_handlers.env_status()
    assert isinstance(status, dict)
    assert "GEMINI_API_KEY" in status
    assert "ZHIPU_API_KEY" in status


def test_system_handlers_smart_parse():
    import asyncio
    raw_curl = "curl 'https://api.message.nogizaka46.com/v1/messages' -H 'authorization: Bearer my_jwt_token_12345678901234567890'"
    res = asyncio.run(system_handlers.smart_parse_credentials_text(raw_curl))
    assert res["token"] == "my_jwt_token_12345678901234567890"


def test_auth_handlers_loopback():
    assert "127.0.0.1" in auth_handlers.LOOPBACK_HOSTS
    assert "localhost" in auth_handlers.LOOPBACK_HOSTS


def test_auth_cookie_secure_configuration():
    original = auth_handlers.cfg.AUTH_COOKIE_SECURE
    try:
        auth_handlers.cfg.AUTH_COOKIE_SECURE = True
        assert auth_handlers._cookie("session", "abc", 60).endswith("Secure")
    finally:
        auth_handlers.cfg.AUTH_COOKIE_SECURE = original


def test_auth_origin_auto_detects_reverse_proxy_without_manual_origin():
    class Handler(_ResponseHandler):
        def __init__(self, headers):
            super().__init__()
            self.headers = headers

    original = auth_handlers.cfg.WEB_ADMIN_ORIGIN
    try:
        auth_handlers.cfg.WEB_ADMIN_ORIGIN = ""
        # 代理保留 Host 但未传协议头时，HTTPS Origin 也应能正常登录。
        assert auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "Origin": "https://push.example.com",
        }))
        # 标准代理头存在时仍严格校验协议，避免 HTTPS/HTTP 混淆。
        assert auth_handlers.check_origin(Handler({
            "Host": "internal:46046",
            "X-Forwarded-Host": "push.example.com",
            "X-Forwarded-Proto": "https",
            "Origin": "https://push.example.com",
        }))
        assert not auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "X-Forwarded-Proto": "http",
            "Origin": "https://push.example.com",
        }))
        assert not auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "Origin": "https://evil.example.com",
        }))
        assert not auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "Origin": "https://push.example.com:8443",
        }))

        # 配置了固定 Origin 时，自动模式不应放宽该约束。
        auth_handlers.cfg.WEB_ADMIN_ORIGIN = "https://admin.example.com"
        assert not auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "Origin": "https://push.example.com",
        }))
    finally:
        auth_handlers.cfg.WEB_ADMIN_ORIGIN = original


def test_archive_handlers_and_media_service():
    assert archive_handlers.ARCHIVE_TYPES == frozenset({"text", "picture", "image", "video", "voice"})
    assert callable(media_service.serve_file_range)
    assert callable(config_service.validate_config)


def test_blog_calendar_endpoint_filters_group_author_and_invalid_dates(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE blog_posts (group_key TEXT, author TEXT, date TEXT)")
    db.executemany(
        "INSERT INTO blog_posts (group_key, author, date) VALUES (?, ?, ?)",
        [
            ("nogizaka", "冨里 奈央", "2026-08-01 10:00"),
            ("nogizaka", "冨里奈央", "2026-08-01 12:00"),
            ("nogizaka", "冨里 奈央", "2026-08-02 10:00"),
            ("nogizaka", "池田 瑛紗", "2026-08-01 10:00"),
            ("sakurazaka", "冨里 奈央", "2026-08-01 10:00"),
            ("nogizaka", "冨里 奈央", "not-a-date"),
        ],
    )
    db.commit()
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)

    class Handler(_ResponseHandler):
        def __init__(self, path):
            super().__init__()
            self.path = path
            self.command = "GET"
            self.payload = None
            self.code = None

        def _send_json(self, payload, code=200):
            self.payload = payload
            self.code = code

    groups = Handler("/api/archive/blog_groups")
    archive_handlers.handle_archive(groups, "blog_groups", lambda **_: True, lambda: None)
    assert groups.code == 200
    assert [(item["key"], item["total"]) for item in groups.payload["groups"]] == [
        ("nogizaka", 5),
        ("sakurazaka", 1),
    ]

    all_posts = Handler("/api/archive/blog_calendar?group=nogizaka")
    archive_handlers.handle_archive(all_posts, "blog_calendar", lambda **_: True, lambda: None)
    assert all_posts.code == 200
    assert all_posts.payload["days"] == {"2026-08-01": 3, "2026-08-02": 1}
    assert all_posts.payload["total"] == 4
    assert all_posts.payload["first_date"] == "2026-08-01"
    assert all_posts.payload["last_date"] == "2026-08-02"

    author = Handler("/api/archive/blog_calendar?group=nogizaka&author=" + quote("冨里 奈央"))
    archive_handlers.handle_archive(author, "blog_calendar", lambda **_: True, lambda: None)
    assert author.code == 200
    assert author.payload["days"] == {"2026-08-01": 2, "2026-08-02": 1}
    assert author.payload["total"] == 3


def test_social_handlers_restore_webui_routes():
    assert callable(social_handlers.handle_subscriptions)
    assert callable(social_handlers.handle_subscriptions_sync)
    assert callable(social_handlers.handle_ig_session_status)
    assert callable(social_handlers.handle_ig_session_save)
    assert callable(social_handlers.handle_ig_session_check)
    assert callable(social_handlers.handle_ig_session_clear)


def test_proxy_test_response_matches_webui_contract(monkeypatch):
    class FakeResponse:
        status_code = 204

    class FakeClient:
        seen_urls = []

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def head(self, url):
            self.seen_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr(system_handlers.httpx, "AsyncClient", FakeClient)
    handler = _ResponseHandler()
    system_handlers.handle_proxy_test(handler, {"proxy": "http://proxy.example:7890"})
    payload = json.loads(handler.wfile.getvalue())

    assert payload["ok"] and payload["all_ok"] and payload["any_ok"]
    assert payload["success_count"] == payload["total_count"] == 4
    first = payload["results"][0]
    assert first["name"] == first["target"] == "Google (Gemini)"
    assert first["status_code"] == first["status"] == 204
    assert "https://generativelanguage.googleapis.com/v1beta/models" in FakeClient.seen_urls


def test_proxy_test_frontend_has_legacy_field_fallbacks():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")
    assert "r.name ?? r.target" in html
    assert "r.status_code ?? r.status" in html


def test_proxy_test_classifies_transport_failure(monkeypatch):
    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def head(self, _url):
            raise system_handlers.httpx.ConnectError("simulated failure")

    monkeypatch.setattr(system_handlers.httpx, "AsyncClient", FailingClient)
    handler = _ResponseHandler()
    system_handlers.handle_proxy_test(handler, {})
    payload = json.loads(handler.wfile.getvalue())

    assert not payload["any_ok"] and not payload["all_ok"]
    assert all(item["error_code"] == "network_error" for item in payload["results"])
    assert all(item["name"] and item["target"] for item in payload["results"])


def test_subscription_sync_reports_partial_failure(monkeypatch):
    async def fake_sync(*_args, **_kwargs):
        return {"healthy": 3}, {"expired": "凭证不可用或已过期"}

    import src.member_directory as member_directory
    monkeypatch.setattr(member_directory, "sync_all_accounts_subscriptions", fake_sync)
    monkeypatch.setattr(member_directory, "get_all_subscriptions", lambda: {"healthy:1": {"state": "active"}})
    handler = _ResponseHandler()

    assert social_handlers.handle_subscriptions_sync(handler)
    payload = json.loads(handler.wfile.getvalue())
    assert payload["ok"] and payload["partial"]
    assert payload["warnings"] == {"expired": "凭证不可用或已过期"}
