"""三种屏幕尺寸下管理端模块的可重复视觉回归。

测试使用本地静态文件和隔离的 API fixture，不访问真实凭证、数据库或外部网络。
首次建立/有意更新基线时设置 ``UPDATE_ADMIN_SNAPSHOTS=1``。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = ROOT / "src" / "webui_static"
BASELINE_ROOT = ROOT / "tests" / "visual_baselines" / "admin"
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 1000},
    "tablet": {"width": 768, "height": 1000},
    "mobile": {"width": 390, "height": 844},
}
TABS = ("status", "monitors", "channels", "social", "system", "users", "advanced")


class _StaticHandler(SimpleHTTPRequestHandler):
    """Serve index.html as / and the shared static assets as /static/*."""

    def translate_path(self, path: str) -> str:
        relative = urlsplit(path).path
        if relative == "/":
            relative = "/index.html"
        elif relative.startswith("/static/"):
            relative = relative[len("/static") :]
        return str(STATIC_ROOT / unquote(relative.lstrip("/")))

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture(scope="module")
def admin_static_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _fixture_config() -> dict:
    return {
        "accounts": {
            "demo_main": {"group": "nogizaka46", "auth": "web", "app_tag": "demo"},
        },
        "monitor": [
            {
                "id": "55",
                "name": "示例成员",
                "account": "demo_main",
                "social": {"x": ["demo_member"], "instagram": ["demo.member"]},
            },
        ],
        "channels": {"qq_official": True, "napcat": True, "tg": True},
        "qq_official_bots": [
            {
                "name": "official_demo",
                "remark": "示例 Bot",
                "app_id": "123456789",
                "target_openid": "USER_DEMO_OPENID",
                "group_openid": "GROUP_DEMO_OPENID",
                "push_message": True,
            },
        ],
        "napcat_api": "http://napcat:3000/send_group_msg",
        "napcat_media_base_url": "http://sakamichi-push:46046",
        "napcat_routes": [
            {"group_id": 12345678, "remark": "示例群", "push_message": True},
        ],
        "tg_bots": [
            {"name": "tg_demo", "remark": "示例频道", "target_chat": "-1001234567890", "push_message": True},
        ],
        "day_interval": [120, 180],
        "night_interval": [3000, 3600],
        "monitor_schedule": {"day_start_hour": 7, "night_start_hour": 0, "sleep_hours": [2, 7]},
        "message_monitor": {"enabled": True},
        "blog_monitor": {
            "enabled": True,
            "hinatazaka": True,
            "nogizaka": True,
            "sakurazaka": True,
            "day_interval": [60, 120],
            "night_interval": [1650, 1950],
        },
        "platforms": {
            "x": {"enabled": True, "interval_seconds": 60, "night_interval_seconds": 300, "accounts": ["demo_x"]},
            "instagram": {
                "enabled": True,
                "include_feed": True,
                "include_stories": False,
                "interval_range_seconds": [1800, 3600],
                "night_interval_range_seconds": [5400, 10800],
                "accounts": ["demo_ig"],
            },
            "tiktok": {"enabled": False, "interval_seconds": 120, "accounts": ["demo_tt"]},
            "tiktok_live": {"enabled": False, "interval_seconds": 8, "accounts": ["demo_live"]},
        },
        "qq_commands": {"enabled": False, "mode": "configured", "allow_openids": []},
        "archive": {"enabled": True, "media": True},
        "web_admin": {"enabled": True, "host": "127.0.0.1", "port": 46046},
        "daily_summary": {"enabled": False, "hour": 23},
        "auth": {"archive_public": False},
        "translate": False,
        "proxy": "",
    }


def _api_payload(path: str) -> dict:
    if path == "/api/auth/me":
        return {"auth_enabled": True, "user": {"username": "demo_admin", "role": "admin"}}
    if path == "/api/config":
        return {
            "ok": True,
            "config": _fixture_config(),
            "cred_status": {"demo_main": {"ok": True}},
            "qq_bot_status": [],
            "env_status": {
                "GEMINI_API_KEY": False,
                "ZHIPU_API_KEY": False,
                "legacy_tg_token": False,
                "tg_bots": {"tg_demo": {"configured": False}},
            },
            "can_restart": False,
            "can_poll": False,
            "can_test_push": False,
            "config_path": "config.json",
        }
    if path == "/api/subscriptions":
        return {"ok": True, "subscriptions": {}}
    if path == "/api/social/ig_session":
        return {"ok": True, "configured": False}
    if path == "/api/status":
        return {
            "ok": True,
            "now_epoch": 1789000000,
            "next_cycle": {"at_epoch": 1789000120, "tag": "day"},
            "cycle_count": 12,
            "uptime_seconds": 86400,
            "startup": {"state": "READY", "reasons": []},
            "tokens": {},
            "channels": {"napcat": {"success": 12, "total": 12, "healthy": True, "last_error": ""}},
            "napcat_session": {"state": "online", "api_reachable": True},
            "napcat_send": {"state": "ready", "consecutive_failures": 0},
            "delivery_backlog": {"routes": 0, "messages": 0, "members": 0},
            "errors": [],
            "embedded": False,
        }
    if path == "/api/system/storage":
        return {
            "ok": True,
            "storage": {
                "disk": {"total_human": "100 GB", "free_human": "72 GB", "used_percent": 28},
                "app_total": {"human": "8.4 GB"},
                "categories": {
                    "messages": {"name": "Message 归档", "human": "4.2 GB", "count": 1200, "color": "#8b5cf6"},
                    "social_media": {"name": "社媒媒体", "human": "3.1 GB", "count": 350, "color": "#ec4899"},
                },
            },
        }
    if path == "/api/users":
        return {
            "ok": True,
            "min_password_len": 8,
            "users": [
                {"username": "demo_admin", "role": "admin", "created_at": 1788900000, "is_me": True},
                {"username": "demo_viewer", "role": "viewer", "created_at": 1788800000, "is_me": False},
            ],
        }
    return {"ok": True}


def _assert_screenshot(actual, expected, *, max_ratio: float = 0.005) -> None:
    from PIL import Image, ImageChops

    actual_image = Image.open(actual).convert("RGBA")
    expected_image = Image.open(expected).convert("RGBA")
    if actual_image.size != expected_image.size:
        raise AssertionError(f"截图尺寸变化：{actual_image.size} != {expected_image.size}")
    diff = ImageChops.difference(actual_image, expected_image)
    bbox = diff.getbbox()
    if bbox is None:
        return
    changed = sum(1 for pixel in diff.getdata() if pixel != (0, 0, 0, 0))
    ratio = changed / (actual_image.width * actual_image.height)
    if ratio > max_ratio:
        actual_path = Path(actual)
        diff.save(actual_path.with_name(actual_path.stem + ".diff.png"))
        raise AssertionError(
            f"视觉回归超出阈值：{ratio:.3%} > {max_ratio:.3%}，差异范围 {bbox}；"
            f"详见 {actual_path} 和 {actual_path.with_name(actual_path.stem + '.diff.png')}"
        )


@pytest.mark.parametrize("viewport_name", tuple(VIEWPORTS))
def test_admin_tabs_visual_regression(admin_static_server, viewport_name, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport=VIEWPORTS[viewport_name],
                color_scheme="dark",
                locale="zh-CN",
                device_scale_factor=1,
            )
            context.add_init_script("localStorage.setItem('sakamichiTheme', 'dark');")

            def handle_api(route):
                path = urlsplit(route.request.url).path
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(_api_payload(path), ensure_ascii=False),
                )

            page = context.new_page()
            page.route("**/api/**", handle_api)
            page.goto(f"{admin_static_server}/#tab=status", wait_until="domcontentloaded")
            page.add_style_tag(content="*{animation:none!important;transition:none!important;caret-color:transparent!important}")
            page.locator("#tab-status.active .admin-module").first.wait_for(state="visible", timeout=10000)
            page.wait_for_timeout(150)

            for tab in TABS:
                if tab != "status":
                    page.locator(f'.nav-tab[data-tab="{tab}"]').click()
                    page.locator(f"#tab-{tab}.active .admin-module").first.wait_for(state="visible", timeout=10000)
                    page.wait_for_timeout(100)

                metrics = page.evaluate(
                    """() => ({
                        scrollWidth: document.documentElement.scrollWidth,
                        viewportWidth: window.innerWidth,
                        modules: [...document.querySelectorAll('section.tab.active .admin-module')].map((el) => {
                            const heading = el.querySelector('.admin-module-heading');
                            const actions = el.querySelector('.admin-module-heading .admin-actions');
                            const h = heading?.getBoundingClientRect();
                            const a = actions?.getBoundingClientRect();
                            return {headingWidth: h?.width || 0, actionsWidth: a?.width || 0, headingBottom: h?.bottom || 0, actionsTop: a?.top || 0};
                        }),
                        tableScrollContainers: document.querySelectorAll('section.tab.active .admin-data-group .table-wrap').length,
                        startupItems: [...document.querySelectorAll('section.tab.active .admin-summary-grid--status .admin-summary-item')].map((el) => el.getBoundingClientRect().width),
                        touchTargets: [...document.querySelectorAll('section.tab.active .admin-actions .btn, section.tab.active .status-actions .btn')].map((el) => el.getBoundingClientRect().height).filter((height) => height > 0),
                    })"""
                )
                assert metrics["scrollWidth"] <= metrics["viewportWidth"] + 1
                assert metrics["modules"]
                assert metrics["tableScrollContainers"] >= (1 if tab in {"status", "monitors", "channels", "users", "advanced"} else 0)
                if tab == "status":
                    assert len(metrics["startupItems"]) == 3
                    assert metrics["startupItems"][0] <= (300 if viewport_name != "mobile" else metrics["viewportWidth"])
                if viewport_name == "mobile":
                    assert all(height >= 40 for height in metrics["touchTargets"])
                for module in metrics["modules"]:
                    if module["actionsWidth"]:
                        assert module["headingWidth"] >= module["actionsWidth"]

                baseline = BASELINE_ROOT / f"{tab}-{viewport_name}.png"
                actual = tmp_path / f"{tab}-{viewport_name}.png"
                page.screenshot(path=str(actual), full_page=False)
                if os.environ.get("UPDATE_ADMIN_SNAPSHOTS") == "1":
                    baseline.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(actual, baseline)
                elif not baseline.exists():
                    pytest.fail(f"缺少视觉基线 {baseline}；运行 UPDATE_ADMIN_SNAPSHOTS=1 pytest -q {__file__}")
                else:
                    _assert_screenshot(actual, baseline)

            context.close()
            browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise
