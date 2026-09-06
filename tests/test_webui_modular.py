from pathlib import Path
import sys
import json
from io import BytesIO
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.webui_modules.archive import common as archive_common, home as archive_home  # noqa: E402
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


def test_static_handler_uses_long_cache_for_versioned_assets():
    handler = _ResponseHandler("gzip")
    handler.path = "/static/archive.js?v=20260901_2"
    static_handler.send_static(handler, "archive.js")
    headers = dict(handler.sent_headers)
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"

    handler.sent_headers.clear()
    handler.path = "/static/archive.js"
    static_handler.send_static(handler, "archive.js")
    headers = dict(handler.sent_headers)
    assert headers["Cache-Control"] == "public, max-age=120, must-revalidate"


def test_system_handlers_env():
    status = system_handlers.env_status()
    assert isinstance(status, dict)
    assert "GEMINI_API_KEY" in status
    assert "ZHIPU_API_KEY" in status


def test_member_handler_uses_current_directory_api(monkeypatch):
    """成员选择器必须调用当前 fetch_member_directory，而不是已删除的旧符号。"""
    import config.credentials as credentials
    from src import member_directory

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_fetch(client, account_id):
        assert isinstance(client, FakeClient)
        assert account_id == "demo"
        return ([
            {
                "id": "55",
                "name": "测试成员",
                "state": "open",
                "tags": ["乃木坂46"],
                "subscription": {
                    "state": "active", "type": "monthly",
                    "start_at": "2026-01-01", "end_at": "2026-12-31",
                    "auto_renewing": True,
                },
            },
            {"id": "56", "name": "离线成员", "state": "closed",
             "subscription": {"state": "expired", "type": "monthly"}},
        ], None)

    monkeypatch.setattr(credentials, "load_all_accounts", lambda: None)
    monkeypatch.setattr(credentials, "is_account_fetch_available", lambda _account: (True, ""))
    monkeypatch.setattr(credentials, "validate_account_cred", lambda _account: (True, ""))
    monkeypatch.setattr(system_handlers.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(member_directory, "fetch_member_directory", fake_fetch)

    handler = _ResponseHandler()
    handler.path = "/api/members?account=demo"
    system_handlers.handle_members(handler, lambda: {"accounts": {"demo": {}}})
    payload = json.loads(handler.wfile.getvalue())

    assert payload["ok"]
    assert payload["total"] == 2
    assert payload["subscribed_count"] == 1
    assert payload["past_subscribed_count"] == 1
    assert payload["open_count"] == 1
    assert payload["members"][0]["is_subscribed"]
    assert payload["members"][1]["is_past_subscribed"]


def test_test_push_normalizes_legacy_official_channel():
    """旧版前端的 official 仍应路由到统一的 qq_official 通道。"""
    calls = []

    def callback(channel, target, text):
        calls.append((channel, target, text))
        return True, ""

    handler = _ResponseHandler()
    system_handlers.handle_test_push(
        handler,
        {"channel": "official", "target": "bot1|private", "text": "hello"},
        callback,
    )
    payload = json.loads(handler.wfile.getvalue())

    assert payload["ok"]
    assert calls == [("qq_official", "bot1|private", "hello")]


def test_admin_frontend_config_and_dark_select_contracts():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")
    assert "const configLabel =" in html
    assert 'msg("已载入 " + configLabel, "ok")' in html
    assert 'opt.style.color = "#000"' not in html
    assert 'channel === "qq_official" || channel === "official"' in html
    assert 'enabled.push(["qq_official", "QQ 官方 Bot"])' in html
    assert "color-scheme: dark" in html
    # 完整 Web 会话必须从 signin 登录响应提取；后续 timeline/profile 请求没有 Set-Cookie。
    assert "右键 signin 请求" in html
    assert "任意 timeline 请求" not in html
    assert "同一个 timeline 请求的 Request Headers" not in html
    assert "必须包含 URL 与请求体" in html
    assert "Request Headers 文本" not in html


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


def test_message_media_totals_do_not_fall_back_to_total_messages():
    members = [
        {"stats": {"total": 10, "pictures": 3, "videos": 2, "voices": 1}},
        {"stats": {"total": 7, "pictures": 0, "videos": 1, "voices": 0}},
    ]
    assert archive_handlers._message_media_totals(members) == {
        "pictures": 3,
        "videos": 3,
        "voices": 1,
        "total": 7,
    }


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
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
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


def test_blog_list_endpoint_returns_summary_dto_without_full_body(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY, group_key TEXT, author TEXT, title TEXT, url TEXT,
            date TEXT, body_html TEXT, body_text TEXT, translation TEXT,
            content_json TEXT, translation_model TEXT, images_json TEXT,
            image_paths_json TEXT, raw_json TEXT
        )"""
    )
    db.execute(
        """INSERT INTO blog_posts
           (id, group_key, author, title, url, date, body_html, body_text,
            translation, content_json, translation_model, images_json, image_paths_json, raw_json)
           VALUES (1, 'nogizaka', '冨里 奈央', '夏のブログ', 'https://example.test/1',
                   '2026-08-01 10:00', '<p>正文</p>', ?, '译文', '[]', '', ?, ?, '{}')""",
        ("正文 " * 400, '["https://cdn.test/cover.jpg"]', '["nogizaka/1/cover.jpg"]'),
    )
    db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
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

    handler = Handler("/api/archive/blogs?group=nogizaka&page=1&per_page=24")
    archive_handlers.handle_archive(handler, "blogs", lambda **_: True, lambda: None)

    assert handler.code == 200
    post = handler.payload["posts"][0]
    assert post["id"] == 1
    assert post["cover"] == "/api/archive/blog_media/nogizaka/1/cover.jpg"
    assert post["cover_original"] == "https://cdn.test/cover.jpg"
    assert post["has_translation"] is True
    assert len(post["excerpt"]) <= 261
    assert "body_html" not in post
    assert "raw_json" not in post
    assert "images_json" not in post

    detail = Handler("/api/archive/blogs?id=1")
    archive_handlers.handle_archive(detail, "blogs", lambda **_: True, lambda: None)
    assert detail.code == 200
    assert detail.payload["post"]["body_html"] == "<p>正文</p>"


def test_blog_delete_translation_endpoint_clears_translation(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        """CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY, author TEXT, title TEXT,
            translation TEXT, content_json TEXT, translation_model TEXT
        )"""
    )
    db.execute(
        """INSERT INTO blog_posts
           (id, author, title, translation, content_json, translation_model)
           VALUES (1, '冨里 奈央', '夏のブログ', '译文', '[{"zh":"翻译"}]', 'gemini-test')"""
    )
    db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "record_event", lambda *args, **kwargs: None)

    class Handler(_ResponseHandler):
        def __init__(self, command="POST"):
            super().__init__()
            self.command = command
            self.path = "/api/archive/blogs/delete_translation"
            self.payload = None
            self.code = None

        def _send_json(self, payload, code=200):
            self.payload = payload
            self.code = code

    handler = Handler()
    archive_handlers.handle_archive(
        handler,
        "blogs/delete_translation",
        lambda **kwargs: kwargs.get("need_admin") is True,
        lambda: {"id": 1},
    )
    assert handler.code == 200
    assert handler.payload == {"ok": True, "id": 1, "msg": "已清除该博客的翻译"}
    row = db.execute(
        "SELECT translation, content_json, translation_model FROM blog_posts WHERE id=1"
    ).fetchone()
    assert row == (None, None, None)

    missing = Handler()
    archive_handlers.handle_archive(
        missing,
        "blogs/delete_translation",
        lambda **_: True,
        lambda: {"id": 999},
    )
    assert missing.code == 404
    assert missing.payload["msg"] == "未找到该博客"

    invalid = Handler()
    archive_handlers.handle_archive(
        invalid,
        "blogs/delete_translation",
        lambda **_: True,
        lambda: {"id": "not-a-number"},
    )
    assert invalid.code == 400
    assert "正整数" in invalid.payload["msg"]


def test_blog_translate_endpoint_persists_structured_translation(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY, group_key TEXT, author TEXT, title TEXT,
            body_html TEXT, translation TEXT, content_json TEXT, translation_model TEXT
        )"""
    )
    db.execute(
        """INSERT INTO blog_posts
           (id, group_key, author, title, body_html, translation, content_json, translation_model)
           VALUES (1, 'nogizaka', '冨里 奈央', '夏のブログ', '<p>こんにちは</p>', NULL, NULL, NULL)"""
    )
    db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)

    async def fake_translate(*_args, **_kwargs):
        return ([{"type": "text", "jp": "こんにちは", "zh": "你好"}], "test-model")

    from src import translator
    monkeypatch.setattr(translator, "translate_blog_structured", fake_translate)

    class Handler(_ResponseHandler):
        command = "POST"
        path = "/api/archive/blogs/translate"

        def __init__(self):
            super().__init__()
            self.payload = None
            self.code = None

        def _send_json(self, payload, code=200):
            self.payload = payload
            self.code = code

    handler = Handler()
    archive_handlers.handle_archive(
        handler,
        "blogs/translate",
        lambda **_: True,
        lambda: {"id": 1},
    )

    assert handler.code == 200
    assert handler.payload["ok"] is True
    assert handler.payload["translation_model"] == "test-model"
    row = db.execute(
        "SELECT translation, content_json, translation_model FROM blog_posts WHERE id=1"
    ).fetchone()
    assert row[0] == "<em>こんにちは</em><br><span>你好</span>"
    assert '"zh": "你好"' in row[1]
    assert row[2] == "test-model"


def test_archive_home_cache_is_single_flight(monkeypatch):
    import threading

    cache_key = (1.0, 2.0, "2026-09-01")
    monkeypatch.setattr(archive_home, "_home_cache", None)
    monkeypatch.setattr(archive_home, "_home_cache_key", None)
    monkeypatch.setattr(archive_home, "_home_cache_building", False)
    monkeypatch.setattr(archive_home, "_home_cache_key_for_request", lambda: cache_key)

    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_impl(handler, _sub, _guard_fn, _read_body_json_fn):
        calls.append(handler)
        started.set()
        if release.wait(2):
            archive_home._home_cache = {"ok": True, "source": "builder"}
            archive_home._home_cache_key = cache_key
            handler._send_json(archive_home._home_cache)

    monkeypatch.setattr(archive_handlers, "_handle_archive_impl", fake_impl)

    class Handler:
        def __init__(self):
            self.payload = None

        def _send_json(self, payload, _code=200):
            self.payload = payload

    first, second = Handler(), Handler()
    t1 = threading.Thread(target=archive_handlers.handle_archive, args=(first, "home", None, None))
    t2 = threading.Thread(target=archive_handlers.handle_archive, args=(second, "home", None, None))
    t1.start()
    assert started.wait(1)
    t2.start()
    release.set()
    t1.join(2)
    t2.join(2)

    assert len(calls) == 1
    assert first.payload == second.payload == {"ok": True, "source": "builder"}
    assert archive_home._home_cache_building is False


def test_archive_home_latest_messages_use_one_window_query():
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE messages (id TEXT, member_dir TEXT, type TEXT, text TEXT, translation TEXT, published_at TEXT, updated_at TEXT)"
    )
    db.executemany(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("a-old", "a", "text", "old", "", "2026-08-01 10:00", "2026-08-01 10:00"),
            ("a-new", "a", "text", "new", "译", "2026-09-01 10:00", "2026-09-01 10:00"),
            ("a-mid", "a", "text", "mid", "", "2026-08-20 10:00", "2026-08-20 10:00"),
            ("b-new", "b", "text", "b-new", "", "2026-09-01 11:00", "2026-09-01 11:00"),
            ("ignored", "a", "picture", "image", "", "2026-09-01 12:00", "2026-09-01 12:00"),
        ],
    )
    rows = archive_handlers._load_latest_text_by_member(db, ["a", "b"], limit=2)

    assert [item["id"] for item in rows["a"]] == ["a-new", "a-mid"]
    assert [item["id"] for item in rows["b"]] == ["b-new"]


def test_archive_home_warmup_uses_noop_handler(monkeypatch):
    def fake_handle(handler, sub, _guard_fn, _read_body_json_fn):
        assert sub == "home"
        handler._send_json({"ok": True})

    monkeypatch.setattr(archive_handlers, "handle_archive", fake_handle)
    assert archive_handlers.warm_home_cache() is True


def test_archive_home_boot_starts_home_request_in_parallel():
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    assert "const homePromise = initialHome ? showHome() : null;" in script
    assert "await Promise.all([authPromise, membersPromise, homePromise].filter(Boolean));" in script
    assert "requestVersion !== _homeRequestVersion" in script
    assert "const renderSecondary = () => {" in script
    assert "const renderTertiary = () => {" in script
    assert "window.requestIdleCallback" in script


def test_archive_blog_route_and_request_guards_are_present():
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    assert "let blogPageVersion = 0" in script
    assert "if (version !== blogPageVersion || curMode !== \"blog\") return;" in script
    assert "function syncBlogHash(pageNum = page)" in script
    assert "const author = p.has(\"author\")" in script
    assert "blogReaderReturnHash" in script
    assert "message_media_total" in script
    assert 'switchMainTab("blog", true)' not in script


def test_archive_home_static_asset_version_bumped():
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    perf = (_ROOT / "tools" / "measure_archive_performance.py").read_text(encoding="utf-8")
    assert "/static/archive.js?v=20260906_1" in html
    assert "/static/archive.css?v=20260905_1" in html
    assert "/static/archive.js?v=20260906_1" in perf
    assert "/static/archive.css?v=20260905_1" in perf


def test_archive_home_omits_duplicate_history_section():
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    styles = (_ROOT / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")
    handler = (_ROOT / "src" / "webui_modules" / "archive_handlers.py").read_text(encoding="utf-8")

    for content in (html, script, styles):
        assert "homeTimeTunnel" not in content
        assert "portal-tunnel-grid" not in content
        assert "hmc-tunnel-badge" not in content
    assert "往昔时光 · 历史回顾" not in html
    assert '"time_tunnel"' not in handler


def test_shared_header_sticky_is_not_disabled_by_root_overflow_container():
    theme = (_ROOT / "src" / "webui_static" / "theme.css").read_text(encoding="utf-8")
    archive = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    admin = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    # html/body 的 hidden overflow 会创建一个额外的滚动容器，使 sticky header
    # 跟随 body 一起离开视口；clip 只裁剪横向溢出，不改变 sticky 的参照物。
    assert "header.app-header" in theme
    assert "position: sticky;" in theme
    assert "top: 0;" in theme
    assert "html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; overflow-x: clip;" in theme
    assert "body { min-height: 100vh; min-height: 100dvh; overflow-x: clip;" in theme
    assert "html, body {\n    /* clip 不会创建额外的滚动容器" in theme
    assert "/static/theme.css?v=20260906_1" in archive
    assert "/static/theme.css?v=20260906_1" in admin


def test_mobile_header_2row_layout_and_actions_guard():
    theme = (_ROOT / "src" / "webui_static" / "theme.css").read_text(encoding="utf-8")

    # 杜绝移动端 actions 换行后因旧版 calc(100vw - 125px) 导致左侧残留死区黑块（遮罩现象）
    assert "calc(100vw - 125px)" not in theme

    # 保证移动端统一为两行布局：
    # 第一行：Brand 居左（order: 1），Actions 紧凑并排居右（order: 2, flex: 1 1 0, safe flex-end）
    # 第二行：Nav 标签栏独占全宽（order: 3, flex: 0 0 100%）
    assert "order: 1;" in theme
    assert "order: 2;" in theme
    assert "order: 3;" in theme
    assert "flex: 1 1 0;" in theme
    assert "justify-content: safe flex-end;" in theme
    assert "flex: 0 0 100%;" in theme

    archive = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    admin = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    # 用户相关操作（改密、登出）紧密相邻，跨系统跳转按钮置于最右侧
    assert archive.index('id="changePwBtn"') < archive.index('id="logoutBtn"') < archive.index('id="adminLink"')
    assert admin.index('id="btnChangePw"') < admin.index('id="btnLogout"') < admin.index('id="archiveLink"')


def test_admin_mobile_member_and_openid_layout_guards_are_present():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    # 姓名列必须保留足够宽度；否则移动端 table auto layout 会把 input 压成空白窄框。
    assert '<table class="member-table">' in html
    assert ".member-table { min-width: 900px; }" in html
    assert ".member-table td:nth-child(2) input { width: 130px; min-width: 130px; max-width: 130px; }" in html

    # OpenID 卡片的信息区与操作区必须可被移动端 CSS 独立换行，防止按钮覆盖长 ID。
    assert 'left.className = "cmd-openid-main"' in html
    assert 'code.className = "cmd-openid-code"' in html
    assert 'right.className = "cmd-openid-actions"' in html
    assert ".cmd-openid-card > .cmd-openid-actions" in html
    assert "width: 100%; flex: 1 1 100%; justify-content: flex-start" in html


def test_admin_restart_and_reload_controls_relocated():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")
    theme = (_ROOT / "src" / "webui_static" / "theme.css").read_text(encoding="utf-8")

    # 顶栏 Actions 不再堆砌管理业务按钮，仅保留主题、用户和消息归档
    header_actions = html.split('<div class="header-actions">')[1].split('</header>')[0]
    assert 'id="btnReloadFile"' not in header_actions
    assert 'id="btnRestart"' not in header_actions

    # 重启主程序按钮移至状态卡片头部，且在移动端允许折行、不再被强制隐藏
    assert 'class="status-actions"' in html
    assert 'id="btnRestart"' in html
    assert '#btnRestart { display: none !important; }' not in theme

    # 放弃修改并重新载入按钮移至固底 savebar 操作区
    assert '<div class="savebar-actions">' in html
    assert 'id="btnReloadFile"' in html
    assert "放弃修改并重新载入" in html


def test_archive_message_order_controls_and_route_state():
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    assert 'let messageOrder' in script
    assert 'localStorage.getItem("archive_message_order")' in script
    assert '&order=" + messageOrder' in script
    assert 'p.set("order", messageOrder)' in script
    assert 'setMessageOrder(requestedOrder' in script
    assert 'id="messageOrderToggle"' in html
    assert 'data-order="desc"' in html and 'data-order="asc"' in html


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


def test_admin_log_view_controls_and_selection_guards():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")
    assert 'id="btnPauseLog"' in html
    assert 'id="btnCopyLog"' in html
    assert "hasLogSelection" in html
    assert "isUserBusyWithLog" in html
    assert "appendLiveEntries" in html
    assert "user-select: text" in html


def test_archive_retry_download_validation(monkeypatch):
    from src.webui_modules.archive import messages

    handler = _ResponseHandler()
    handler.path = "/api/archive/retry_download?member=test_member"
    monkeypatch.setattr(messages._archive, "list_members", lambda: ["test_member"])
    sent_resps = []
    monkeypatch.setattr(messages, "_send_json_resp", lambda h, data, code=200: sent_resps.append((data, code)))

    # 1. Non-dict body
    assert messages.handle_messages(handler, "retry_download", lambda **_: True, lambda: [])
    assert sent_resps[-1][1] == 400

    # 2. Invalid year/month
    assert messages.handle_messages(handler, "retry_download", lambda **_: True, lambda: {"id": "1", "year": "bad", "month": 5})
    assert sent_resps[-1][1] == 400

    # 3. Message not found
    monkeypatch.setattr(messages._archive, "load_month", lambda *a: [])
    assert messages.handle_messages(handler, "retry_download", lambda **_: True, lambda: {"id": "999", "year": 2026, "month": 5})
    assert sent_resps[-1][1] == 404


def test_accounts_verify_route(monkeypatch):
    from unittest.mock import MagicMock
    from src.webui import _Handler
    import config.credentials as creds

    handler = _Handler.__new__(_Handler)
    handler.path = "/api/accounts/verify"
    handler.command = "POST"
    handler.headers = {}
    handler._check_host = MagicMock(return_value=True)
    handler._check_origin = MagicMock(return_value=True)
    handler._check_auth = MagicMock(return_value=True)
    sent_resps = []
    handler._send_json = lambda data, code=200: sent_resps.append((data, code))

    # Missing account
    handler._read_body_json = MagicMock(return_value={})
    handler.do_POST()
    assert sent_resps[-1][1] == 400

    # Successful verify
    async def mock_verify(acc):
        return True, "握手成功", {"plan": "active"}

    monkeypatch.setattr(creds, "verify_and_handshake_account", mock_verify)
    handler._read_body_json = MagicMock(return_value={"account": "acc1"})
    handler.do_POST()
    assert sent_resps[-1][0]["ok"] is True
    assert sent_resps[-1][0]["msg"] == "握手成功"


def test_accounts_smart_parse_route(monkeypatch):
    from unittest.mock import MagicMock
    from src.webui import _Handler

    handler = _Handler.__new__(_Handler)
    handler.path = "/api/accounts/smart_parse"
    handler.command = "POST"
    handler.headers = {}
    handler._check_host = MagicMock(return_value=True)
    handler._check_origin = MagicMock(return_value=True)
    handler._check_auth = MagicMock(return_value=True)
    sent_resps = []
    handler._send_json = lambda data, code=200: sent_resps.append((data, code))

    # Missing raw text
    handler._read_body_json = MagicMock(return_value={})
    handler.do_POST()
    assert sent_resps[-1][1] == 400

    # Successful parse
    async def mock_parse(raw, acc=""):
        return {"token": "tok123", "cookie": "c=1", "refresh_token": "rt123", "extracted": []}

    handler._smart_parse_credentials_text = mock_parse
    handler._read_body_json = MagicMock(return_value={"raw": "curl ...", "account": "acc1"})
    handler.do_POST()
    assert sent_resps[-1][0]["ok"] is True
    assert sent_resps[-1][0]["token"] == "tok123"


def test_accounts_rename_route(monkeypatch):
    from unittest.mock import MagicMock
    from src.webui import _Handler
    import config.credentials as creds

    handler = _Handler.__new__(_Handler)
    handler.path = "/api/accounts/rename"
    handler.command = "POST"
    handler.headers = {}
    handler._check_host = MagicMock(return_value=True)
    handler._check_origin = MagicMock(return_value=True)
    handler._check_auth = MagicMock(return_value=True)
    sent_resps = []
    handler._send_json = lambda data, code=200: sent_resps.append((data, code))

    # Missing params
    handler._read_body_json = MagicMock(return_value={"old_id": "a"})
    handler.do_POST()
    assert sent_resps[-1][1] == 400

    # Successful rename
    renamed = []
    monkeypatch.setattr(creds, "rename_account", lambda old_id, new_id: renamed.append((old_id, new_id)))
    handler._read_body_json = MagicMock(return_value={"old_id": "acc_old", "new_id": "acc_new"})
    handler.do_POST()
    assert sent_resps[-1][0]["ok"] is True
    assert renamed == [("acc_old", "acc_new")]

