from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, quote, unquote, urlsplit
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = REPO_ROOT / "src" / "webui_static"

MOBILE_VIEWPORTS = {
    "320": {"width": 320, "height": 844},
    "360": {"width": 360, "height": 844},
    "390": {"width": 390, "height": 844},
    "402": {"width": 402, "height": 874},
    "430": {"width": 430, "height": 932},
}


class _ArchiveStaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        relative = urlsplit(path).path
        if relative in {"", "/"}:
            relative = "/archive.html"
        elif relative.startswith("/static/"):
            relative = relative[len("/static") :]
        return str(STATIC_ROOT / unquote(relative.lstrip("/")))

    def log_message(self, *_args: object) -> None:
        return


class _AdminStaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        relative = urlsplit(path).path
        if relative in {"", "/"}:
            relative = "/index.html"
        elif relative.startswith("/static/"):
            relative = relative[len("/static") :]
        return str(STATIC_ROOT / unquote(relative.lstrip("/")))

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture(scope="module")
def archive_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveStaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.fixture(scope="module")
def admin_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AdminStaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize("vp_name,vp_size", list(MOBILE_VIEWPORTS.items()))
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_mobile_message_actions_touch_geometry_and_accessibility(archive_server, vp_name, vp_size, theme):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport=vp_size,
            color_scheme=theme,
            locale="zh-CN",
            device_scale_factor=1,
            service_workers="block",
        )
        context.add_init_script(f"localStorage.setItem('sakamichiTheme', '{theme}');")

        fav_state = {"is_fav": False}

        def handle_api(route):
            request = route.request
            parsed = urlsplit(request.url)
            query = parse_qs(parsed.query)
            path = parsed.path
            if path == "/api/archive/messages":
                requested_page = int(query.get("page", ["1"])[0])
                payload = {
                    "ok": True,
                    "member": "示例成员",
                    "group": "nogizaka",
                    "year": 2026,
                    "month": 9,
                    "total": 1,
                    "page": requested_page,
                    "total_pages": 1,
                    "order": "desc",
                    "messages": [{
                        "id": "m001",
                        "type": "text",
                        "group": "nogizaka",
                        "text": "测试消息文本用于验证触控区与可访问性",
                        "translation": "测试翻译文本",
                        "year": 2026,
                        "month": 9,
                        "published_at": "2026-09-20T12:00:00Z",
                        "upload_at": "",
                        "media_url": None,
                        "tags": "",
                        "custom_tags": "",
                        "is_favorite": fav_state["is_fav"],
                    }],
                }
            elif path == "/api/archive/message/interaction":
                body = json.loads(request.post_data or "{}")
                if body.get("action") == "favorite":
                    fav_state["is_fav"] = bool(body.get("value"))
                payload = {"ok": True, "is_favorite": fav_state["is_fav"]}
            elif path == "/api/auth/me":
                payload = {"ok": True, "auth_enabled": False}
            elif path == "/api/archive/members":
                payload = {
                    "ok": True,
                    "members": [{
                        "name": "示例成员",
                        "display": "示例成员",
                        "group": "nogizaka",
                        "total": 1,
                        "stats": {"total": 1, "months": 1},
                    }],
                    "monitor_members": [],
                }
            elif path == "/api/archive/months":
                payload = {"ok": True, "months": [{"year": 2026, "month": 9, "count": 1}]}
            elif path == "/api/archive/calendar":
                payload = {"ok": True, "days": {"2026-09-20": 1}}
            elif path == "/api/archive/blog_groups":
                payload = {"ok": True, "groups": []}
            else:
                payload = {"ok": True, "members": [], "groups": [], "months": [], "days": {}}
            route.fulfill(status=200, content_type="application/json; charset=utf-8", body=json.dumps(payload, ensure_ascii=False))

        context.route("**/api/**", handle_api)
        page = context.new_page()
        url = f"{archive_server}/archive.html#member={quote('示例成员')}&y=2026&m=9&fav=0"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("loadedMessagePages.has(1) && document.querySelector('[data-msg-id=\"m001\"] .fav-btn') !== null")
        page.add_style_tag(content="*{animation:none!important;transition:none!important}")

        fav_btn = page.locator(".bubble .fav-btn").first
        copy_btn = page.locator(".bubble .copy-btn").first

        fav_box = fav_btn.bounding_box()
        copy_box = copy_btn.bounding_box()

        assert fav_box is not None, f"fav_box missing at {vp_name}px/{theme}"
        assert copy_box is not None, f"copy_box missing at {vp_name}px/{theme}"

        # 检查初始无障碍属性（未收藏）
        assert fav_btn.get_attribute("aria-pressed") == "false", f"initial aria-pressed should be false at {vp_name}px/{theme}"
        assert fav_btn.get_attribute("aria-label") == "收藏"

        # 断言触控区尺寸：移动端下高度和宽度必须达到触控规范（>= 40px，标准 44px）
        assert fav_box["height"] >= 40, f"fav button height {fav_box['height']}px < 40px at {vp_name}px/{theme}"
        assert fav_box["width"] >= 40, f"fav button width {fav_box['width']}px < 40px at {vp_name}px/{theme}"
        assert copy_box["height"] >= 40, f"copy button height {copy_box['height']}px < 40px at {vp_name}px/{theme}"

        # 点击切换收藏，验证状态与 aria-pressed 同步变为 true
        fav_btn.click()
        page.wait_for_function("document.querySelector('[data-msg-id=\"m001\"] .fav-btn').classList.contains('active')")
        assert fav_btn.get_attribute("aria-pressed") == "true", f"toggled aria-pressed should be true at {vp_name}px/{theme}"
        assert fav_btn.get_attribute("aria-label") == "取消收藏"

        browser.close()


@pytest.mark.parametrize("vp_name,vp_size", list(MOBILE_VIEWPORTS.items()))
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_mobile_admin_filter_pill_and_dialog_geometry(admin_server, vp_name, vp_size, theme):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport=vp_size,
            color_scheme=theme,
            locale="zh-CN",
            device_scale_factor=1,
            service_workers="block",
        )
        context.add_init_script(f"localStorage.setItem('sakamichiTheme', '{theme}');")

        def handle_api(route):
            path = urlsplit(route.request.url).path
            if path == "/api/auth/me":
                route.fulfill(status=200, content_type="application/json; charset=utf-8", body=json.dumps({"ok": True, "auth_enabled": False, "user": {"role": "admin"}}, ensure_ascii=False))
                return
            if path == "/api/route-diagnostics":
                route.fulfill(status=200, content_type="application/json; charset=utf-8", body=json.dumps({
                    "ok": True,
                    "items": [{
                        "member_name": "秋元真夏",
                        "member_dir": "akimotomanatsu",
                        "channel": "message",
                        "status": "issues",
                        "issues": ["测试未配置推送通道"],
                        "last_check": "2026-11-01T12:00:00Z"
                    }]
                }, ensure_ascii=False))
                return
            route.fulfill(status=200, content_type="application/json; charset=utf-8", body=json.dumps({"ok": True}, ensure_ascii=False))

        context.route("**/api/**", handle_api)
        page = context.new_page()
        page.goto(f"{admin_server}/#tab=status", wait_until="domcontentloaded")
        page.add_style_tag(content="*{animation:none!important;transition:none!important}")

        # 检查诊断筛选胶囊触控高度
        pill = page.locator(".admin-task-filter-pill").first
        pill.wait_for(state="visible", timeout=10000)
        pill_box = pill.bounding_box()
        assert pill_box is not None, f"pill_box missing at {vp_name}px/{theme}"
        assert pill_box["height"] >= 36, f"filter pill height {pill_box['height']}px < 36px at {vp_name}px/{theme}"

        # 检查视口不溢出
        doc_scroll_width = page.evaluate("document.documentElement.scrollWidth")
        win_inner_width = page.evaluate("window.innerWidth")
        assert doc_scroll_width <= win_inner_width, f"horizontal overflow at {vp_name}px/{theme}: scrollWidth {doc_scroll_width} > innerWidth {win_inner_width}"

        browser.close()
