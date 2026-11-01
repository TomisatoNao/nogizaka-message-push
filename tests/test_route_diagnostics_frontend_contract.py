"""Static frontend contracts for the admin member route diagnostics panel."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "webui_static"


def test_route_diagnostics_panel_is_admin_gated_and_uses_one_script():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "admin_route_diagnostics.js").read_text(encoding="utf-8")

    script_url = "/static/admin_route_diagnostics.js?v=20261027_1"
    assert page.count(script_url) == 1
    assert page.count('/static/admin_route_diagnostics.css?v=20261102_2') == 1
    assert 'id="routeDiagnosticsPanel" hidden' in page
    assert 'window.webAdminRole === "admin"' in script
    assert 'window.addEventListener("webadminauthready", syncAuthorization' in script
    assert 'if (!authorized || requestInFlight) return;' in script
    assert 'version !== requestVersion' in script


def test_route_diagnostics_uses_read_only_admin_endpoint_and_explicit_states():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "admin_route_diagnostics.js").read_text(encoding="utf-8")

    assert script.count('"/api/route-diagnostics"') == 1
    assert 'method: "GET"' in script
    assert 'cache: "no-store"' in script
    for label in (
        "抓取账号关联", "订阅缓存", "未知 / 未同步", "不能据此判断为未订阅",
        "符合条件", "未满足", "已载入", "暂时无法加载", "重试加载诊断",
        "不代表已尝试推送或已送达",
    ):
        assert label in page + script
    assert 'id="btnRefreshRouteDiagnostics"' in page
    assert 'id="btnRetryRouteDiagnostics"' in page


def test_route_diagnostics_does_not_render_targets_bot_names_or_raw_config():
    script = (STATIC / "admin_route_diagnostics.js").read_text(encoding="utf-8")

    # 隐私与安全边界：严禁暴露目标 OpenID、群号、chat_id、密钥或原始配置结构
    for private_field in (
        "target_openid", "group_openid", "target_chat", "client_secret",
        "raw config", "raw_config", "napcat_api_token",
    ):
        assert private_field not in script
    assert "innerHTML" not in script
    assert "delivery_status" not in script

    # 安全标签消费：仅由白名单正则转换为去歧义的通用中文标签
    for safe_label in ("QQ 官方 Bot", "OneBot 配置", "Telegram Bot"):
        assert safe_label in script


def test_route_diagnostics_mobile_toolbar_geometry_across_viewports_and_themes():
    import json
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit
    import pytest

    playwright = pytest.importorskip("playwright.sync_api")

    class _AdminStaticHandler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            route = urlsplit(path).path
            if route == "/" or route == "/index.html":
                return str(STATIC / "index.html")
            if route.startswith("/static/"):
                route = route[len("/static/"):]
            return str(STATIC / route.lstrip("/"))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _AdminStaticHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]

    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for width in (320, 360, 390, 402, 430):
                for theme in ("dark", "light"):
                    ctx = browser.new_context(
                        viewport={"width": width, "height": 844},
                        color_scheme=theme,
                    )
                    page = ctx.new_page()

                    def mock_api(route):
                        url = route.request.url
                        if "/api/auth/me" in url:
                            route.fulfill(
                                status=200,
                                content_type="application/json",
                                body=json.dumps({"ok": True, "auth_enabled": True, "user": {"username": "admin", "role": "admin"}}),
                            )
                        elif "/api/status" in url:
                            route.fulfill(
                                status=200,
                                content_type="application/json",
                                body=json.dumps({"ok": True, "now_epoch": 1789000000, "startup": {"state": "READY", "reasons": []}, "channels": {}, "delivery_backlog": {"routes": 0, "messages": 0, "members": 0}, "errors": []}),
                            )
                        elif "/api/route-diagnostics" in url:
                            route.fulfill(
                                status=200,
                                content_type="application/json",
                                body=json.dumps({
                                    "ok": True,
                                    "diagnostics": [
                                        {"group": "nogizaka", "member_id": "m1", "member_name": "井上和", "avatar": "", "is_subscribed": True, "delivery_enabled": True, "routes": []},
                                        {"group": "nogizaka", "member_id": "m2", "member_name": "一ノ瀬美空", "avatar": "", "is_subscribed": False, "delivery_enabled": False, "routes": []},
                                    ],
                                }),
                            )
                        else:
                            route.fulfill(status=200, content_type="application/json", body="{}")

                    page.route("**/api/**", mock_api)
                    page.goto(f"http://127.0.0.1:{port}/#tab=status")
                    page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
                    page.wait_for_selector("#routeDiagnosticsPanel:not([hidden])")
                    page.wait_for_selector(".admin-route-member")

                    wrap_box = page.locator(".admin-route-search-wrap").bounding_box()
                    input_box = page.locator("#routeDiagnosticsSearch").bounding_box()
                    filter_box = page.locator(".admin-route-filter-group").bounding_box()
                    toolbar_box = page.locator("#routeDiagnosticsToolbar").bounding_box()

                    # 1. 窄屏搜索容器必须是自然高度，禁止保留 200px 造成空隙
                    assert wrap_box["height"] <= 52, f"[{width}px {theme}] Search wrap height must be <=52px, got {wrap_box['height']}"
                    # 2. 搜索框触控高度必须达标（>=44px）
                    assert input_box["height"] >= 44, f"[{width}px {theme}] Search input height must be >=44px, got {input_box['height']}"
                    # 3. 搜索框底部到筛选组的纵向间隙必须 <=16px（严禁出现 159px 留白）
                    gap = filter_box["y"] - (input_box["y"] + input_box["height"])
                    assert 0 <= gap <= 16, f"[{width}px {theme}] Gap between search and filter should be <=16px, got {gap}"
                    # 4. 工具栏整体高度紧凑合理，建议 <=160px
                    assert toolbar_box["height"] <= 160, f"[{width}px {theme}] Toolbar height should be <=160px, got {toolbar_box['height']}"
                    # 5. 无全页横向溢出
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    ctx.close()

            # 6. 验证桌面端（1440px）保持横排弹性伸缩
            ctx_desktop = browser.new_context(viewport={"width": 1440, "height": 900})
            page_d = ctx_desktop.new_page()
            page_d.route("**/api/**", mock_api)
            page_d.goto(f"http://127.0.0.1:{port}/#tab=status")
            page_d.wait_for_selector("#routeDiagnosticsPanel:not([hidden])")
            d_wrap = page_d.locator(".admin-route-search-wrap").bounding_box()
            d_toolbar = page_d.locator("#routeDiagnosticsToolbar").bounding_box()
            d_filter = page_d.locator(".admin-route-filter-group").bounding_box()
            assert d_toolbar["height"] <= 60, f"Desktop toolbar should be single row (<=60px), got {d_toolbar['height']}"
            assert abs(d_wrap["y"] - d_filter["y"]) < 10, "Desktop search and filter should be in the same row"
            assert page_d.evaluate("document.documentElement.scrollWidth <= innerWidth")
            ctx_desktop.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
