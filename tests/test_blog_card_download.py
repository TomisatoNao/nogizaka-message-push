"""博客阅读页单张卡片下载的内容、安全边界与路由契约。"""

import asyncio
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
import pytest
from PIL import Image

from src.blog_card_renderer import _generate_html, is_playwright_available
from src.webui_modules.archive import blog_card_download as cards
from src.webui_modules.archive import blogs


def _post(**overrides):
    post = {
        "id": 17,
        "group_key": "hinatazaka",
        "author": "测试成员",
        "title": "测试标题",
        "date": "2026-10-01 12:00",
        "body_html": "<p>原文第一段</p><img src='https://example.invalid/x.jpg'><p>原文第二段</p>",
        "body_text": "原文第一段 原文第二段",
        "content_json": json.dumps([
            {"type": "text", "jp": "原文第一段", "zh": "译文第一段"},
            {"type": "img"},
            {"type": "text", "jp": "原文第二段", "zh": "译文第二段"},
        ], ensure_ascii=False),
        "images_json": '["https://example.invalid/x.jpg"]',
        "image_paths_json": '["photo.jpg"]',
        "translation_model": "test-model",
    }
    post.update(overrides)
    return post


@pytest.fixture
def image_root(tmp_path, monkeypatch):
    root = tmp_path / "blog_images"
    root.mkdir()
    Image.new("RGB", (32, 24), "blue").save(root / "photo.jpg")
    monkeypatch.setattr(cards, "IMAGE_ROOT", root)
    return root


def test_card_export_respects_language_and_escapes_untrusted_content(image_root):
    post = _post(title="<script>alert(1)</script>")
    for mode in ("ja-zh", "ja-only", "zh-only"):
        safe_post, paths, key = cards.prepare_card(post, mode)
        assert paths == [image_root / "photo.jpg"]
        assert len(key.split(":")) == 2
        html = _generate_html(safe_post, [cards._image_data(paths[0])], mode=mode, export=True)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "data:image/jpeg;base64," in html
        assert html.count('class="blog-img"') == 1
        if mode == "ja-only":
            assert "原文第一段" in html and "译文第一段" not in html
        elif mode == "zh-only":
            assert "译文第一段" in html and "原文第一段" not in html
        else:
            assert "原文第一段" in html and "译文第一段" in html


def test_card_export_falls_back_to_japanese_without_translation(image_root):
    safe_post, _, key = cards.prepare_card(_post(content_json=""), "zh-only")
    assert key.endswith(":ja-only")
    blocks = json.loads(safe_post["content_json"])
    assert [item["type"] for item in blocks] == ["text", "img", "text"]
    assert [item.get("jp") for item in blocks if item["type"] == "text"] == ["原文第一段", "原文第二段"]


def test_card_export_rejects_missing_or_escaping_images(image_root):
    with pytest.raises(cards.CardExportError, match="归档不完整"):
        cards.prepare_card(_post(image_paths_json="[]"), "ja-only")
    with pytest.raises(cards.CardExportError, match="归档不完整"):
        cards.prepare_card(_post(image_paths_json='["../outside.jpg"]'), "ja-only")
    with pytest.raises(cards.CardExportError, match="正文中的图片"):
        cards.prepare_card(_post(content_json=json.dumps([{"type": "img"}, {"type": "img"}])), "ja-zh")
    with pytest.raises(cards.CardExportError, match="正文过长"):
        cards.prepare_card(_post(body_html="x" * (cards.MAX_CONTENT_CHARS + 1)), "ja-only")


def test_card_export_uses_verified_source_urls_when_local_archive_is_missing(image_root):
    post = _post(
        images_json='["https://cdn.hinatazaka46.com/images/recover.jpg"]',
        image_paths_json="[]",
    )
    _, sources, _ = cards.prepare_card(post, "ja-only")
    assert sources == ["https://cdn.hinatazaka46.com/images/recover.jpg"]


def test_card_export_rejects_untrusted_remote_image_hosts(image_root):
    post = _post(
        images_json='["http://127.0.0.1/admin"]',
        image_paths_json="[]",
    )
    with pytest.raises(cards.CardExportError, match="来源无法安全校验"):
        cards.prepare_card(post, "ja-only")


@pytest.mark.asyncio
async def test_missing_archived_images_are_downloaded_from_official_cdn(tmp_path, image_root):
    image_bytes = (image_root / "photo.jpg").read_bytes()
    requested = []

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=image_bytes)

    transport = httpx.MockTransport(respond)
    async with httpx.AsyncClient(transport=transport) as client:
        paths = await cards._download_remote_images(
            ["https://cdn.hinatazaka46.com/images/recover.jpg"],
            tmp_path / "recovered",
            "hinatazaka",
            client=client,
        )

    assert requested == ["https://cdn.hinatazaka46.com/images/recover.jpg"]
    assert len(paths) == 1 and paths[0].is_file()
    with Image.open(paths[0]) as recovered:
        assert recovered.size == (32, 24)


@pytest.mark.asyncio
async def test_remote_image_redirect_cannot_escape_official_domain(tmp_path):
    def respond(_request):
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    transport = httpx.MockTransport(respond)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(cards.CardExportError, match="来源无法安全校验"):
            await cards._download_remote_images(
                ["https://cdn.hinatazaka46.com/images/recover.jpg"],
                tmp_path / "recovered",
                "hinatazaka",
                client=client,
            )


def test_card_cache_key_changes_with_translation_and_image(image_root):
    _, _, first = cards.prepare_card(_post(), "ja-zh")
    _, _, second = cards.prepare_card(_post(content_json='[{"type":"text","jp":"A","zh":"B"}]'), "ja-zh")
    assert first != second
    image = image_root / "photo.jpg"
    Image.new("RGB", (40, 24), "red").save(image)
    _, _, third = cards.prepare_card(_post(), "ja-zh")
    assert first != third


def test_card_request_is_singleflight_and_hits_cache(image_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "CARD_DIR", tmp_path / "cards")
    cards._jobs.clear()
    cards._key_jobs.clear()
    cards._recent_requests.clear()
    with patch.object(cards, "is_playwright_available", return_value=True), \
         patch.object(cards, "_executor") as executor:
        first, first_code = cards.submit(_post(), "ja-zh", "client-test")
        second, second_code = cards.submit(_post(), "ja-zh", "client-test")
        assert first_code == second_code == 202
        assert first["job_id"] == second["job_id"]
        executor.submit.assert_called_once()
        key = cards._jobs[first["job_id"]]["key"]
        cards.CARD_DIR.mkdir()
        (cards.CARD_DIR / f"{key}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        cached, code = cards.submit(_post(), "ja-zh", "client-test")
        assert code == 200
        assert cached == {"ok": True, "status": "ready", "job_id": key}
    cards._jobs.clear()
    cards._key_jobs.clear()
    cards._recent_requests.clear()


def test_card_post_route_checks_reader_guard_and_uses_server_row(monkeypatch):
    class Handler:
        command = "POST"
        path = "/api/archive/blogs/card"
        headers = {}
        client_address = ("127.0.0.1", 10000)

        def _send_json(self, result, code=200):
            self.result, self.code = result, code

    handler = Handler()
    with patch.object(blogs, "_guard_card_login", return_value=False):
        assert blogs.handle_blogs(handler, "blogs/card", lambda **_: False, lambda: {"id": 17})
    assert not hasattr(handler, "result")

    class Db:
        def execute(self, query, params):
            assert query == "SELECT * FROM blog_posts WHERE id=?"
            assert params == (17,)
            return self

        def fetchone(self):
            return _post()

    with patch.object(blogs, "_guard_card_login", return_value=True), \
         patch.object(blogs, "_get_db", return_value=Db()), \
         patch.object(cards, "submit", return_value=({"ok": True, "status": "queued", "job_id": "abc"}, 202)) as submit:
        assert blogs.handle_blogs(handler, "blogs/card", lambda **_: True,
                                  lambda: {"id": 17, "mode": "ja-only", "html": "<script>bad</script>"})
    assert handler.code == 202
    assert submit.call_args.args[0]["id"] == 17
    assert submit.call_args.args[1] == "ja-only"
    assert handler.result["status"] == "queued"


def test_all_card_routes_require_logged_in_user():
    class Handler:
        command = "GET"
        path = "/api/archive/blogs/card_status?job_id=bad"

        def _send_json(self, result, code=200):
            self.result, self.code = result, code

    handler = Handler()
    with patch.object(blogs.cfg, "AUTH_ENABLED", True), \
         patch.object(blogs.cfg, "AUTH_ARCHIVE_PUBLIC", True), \
         patch.object(blogs, "current_user", return_value=None):
        assert blogs.handle_blogs(handler, "blogs/card_status", lambda **_: True, lambda: None)
        assert handler.code == 401
        assert blogs.handle_blogs(handler, "blogs/card_file", lambda **_: True, lambda: None)
        assert handler.code == 401


def test_card_file_route_streams_only_ready_jpeg(tmp_path):
    output = tmp_path / "ready.jpg"
    output.write_bytes(b"\xff\xd8\xff\xd9")

    class Handler:
        command = "GET"
        path = "/api/archive/blogs/card_file?job_id=" + "a" * 32
        wfile = BytesIO()
        headers_sent = {}

        def send_response(self, code):
            self.code = code

        def send_header(self, name, value):
            self.headers_sent[name] = value

        def end_headers(self):
            pass

    handler = Handler()
    with patch.object(blogs, "_guard_card_login", return_value=True), \
         patch.object(cards, "ready_file", return_value=output):
        assert blogs.handle_blogs(handler, "blogs/card_file", lambda **_: True, lambda: None)
    assert handler.code == 200
    assert handler.headers_sent["Content-Type"] == "image/jpeg"
    assert "attachment" in handler.headers_sent["Content-Disposition"]
    assert handler.wfile.getvalue() == b"\xff\xd8\xff\xd9"


@pytest.mark.asyncio
async def test_single_jpeg_renderer_preserves_bottom_content(tmp_path, image_root):
    if not is_playwright_available():
        pytest.skip("Playwright unavailable")
    post = _post(content_json=json.dumps([
        {"type": "text", "jp": "起始段落", "zh": ""},
        {"type": "img"},
        {"type": "text", "jp": "结尾段落", "zh": ""},
    ], ensure_ascii=False))
    safe_post, paths, _ = cards.prepare_card(post, "ja-only")
    output = tmp_path / "card.jpg"
    try:
        await asyncio.wait_for(cards._render(safe_post, paths, "ja-only", output), timeout=45)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Chromium unavailable")
        raise
    assert output.is_file()
    with Image.open(output) as img:
        assert img.format == "JPEG"
        assert img.width == 900
        assert img.height > 700
        # 页脚必须实际落在单张图片最底部，防止视口外截图返回黑块。
        bottom = img.getpixel((img.width // 2, img.height - 10))
        assert all(channel > 2 for channel in bottom)


@pytest.mark.parametrize("viewport", [(1440, 900), (768, 900), (390, 844)])
def test_reader_download_button_layout_and_single_download(viewport, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    static_root = Path(__file__).resolve().parents[1] / "src" / "webui_static"

    class StaticHandler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            route = urlsplit(path).path
            if route.startswith("/static/"):
                route = route[len("/static/"):]
            return str(static_root / route.lstrip("/"))

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip("Chromium unavailable")
                raise
            try:
                page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]}, accept_downloads=True)

                def mock_api(route):
                    url = route.request.url
                    if "/api/archive/blogs/card_file" in url:
                        route.fulfill(status=200, content_type="image/jpeg", body=b"\xff\xd8\xff\xd9")
                        return
                    if "/api/auth/me" in url:
                        payload = {"ok": True, "auth_enabled": True, "user": {"username": "reader", "role": "user"}}
                    elif "/api/archive/blogs/card_status" in url:
                        payload = {"ok": True, "status": "ready", "job_id": "a" * 32}
                    elif "/api/archive/blogs/card" in url:
                        payload = {"ok": True, "status": "queued", "job_id": "a" * 32}
                    else:
                        payload = {"ok": True, "groups": [], "members": [], "posts": []}
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

                page.route("**/api/**", mock_api)
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/archive.html")
                page.wait_for_function("window._canDownloadBlogCard === true")
                page.evaluate("""() => openBlogReader({
                  id: 17, group_key: 'hinatazaka', author: '成员', title: '标题',
                  date: '2026-10-01', url: 'https://example.invalid/blog',
                  body_html: '<p>正文</p>', images_json: '[]', image_paths_json: '[]'
                })""")
                button = page.locator("#brDownloadCard")
                assert button.is_visible()
                for theme in ("dark", "light"):
                    page.evaluate("value => document.documentElement.setAttribute('data-theme', value)", theme)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(tmp_path / f"reader-{viewport[0]}-{theme}.png"))
                with page.expect_download(timeout=10_000) as info:
                    button.click()
                assert info.value.suggested_filename == "blog-card-17.jpg"
                page.evaluate("""() => {
                  window._isArchiveAdmin = true;
                  openBlogReader({
                    id: 18, group_key: 'hinatazaka', author: '成员', title: '有译文的博客',
                    date: '2026-10-01', url: 'https://example.invalid/blog/18',
                    body_html: '<p>正文</p>', images_json: '[]', image_paths_json: '[]',
                    content_json: '[{"type":"text","jp":"原文","zh":"译文"}]',
                    translation_status: 'succeeded'
                  });
                }""")
                assert page.locator("#brModeSelector").is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.evaluate("document.querySelector('.br-header').getBoundingClientRect().height < 160")
                page.screenshot(path=str(tmp_path / f"reader-{viewport[0]}-translated.png"))
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
