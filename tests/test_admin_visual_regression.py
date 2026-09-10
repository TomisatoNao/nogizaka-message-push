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
                    # 存储卡片的容量、项目数和清理动作必须是稳定的语义行；
                    # 这部分在移动端通常位于首屏以下，单纯的顶端截图覆盖不到。
                    page.wait_for_function(
                        "() => document.querySelectorAll('#stStorageGrid .admin-storage-card').length >= 2",
                        timeout=5000,
                    )
                    storage = page.evaluate(
                        """() => {
                            const grid = document.querySelector('#stStorageGrid');
                            const cards = [...(grid?.querySelectorAll('.admin-storage-card') || [])];
                            return {
                                gridWidth: grid?.getBoundingClientRect().width || 0,
                                cards: cards.map((card) => ({
                                    children: [...card.children].map((el) => el.className),
                                    headingHasCount: !!card.querySelector('.admin-storage-card-heading .admin-storage-card-count'),
                                    metaCount: card.querySelectorAll('.admin-storage-card-meta .admin-storage-card-count').length,
                                    actionCount: card.querySelectorAll(':scope > .admin-storage-card-actions').length,
                                    cleanInActions: [...card.querySelectorAll('.admin-storage-clean')].every((button) =>
                                        button.parentElement?.classList.contains('admin-storage-card-actions')),
                                    right: card.getBoundingClientRect().right,
                                })),
                            };
                        }"""
                    )
                    assert storage["cards"]
                    assert storage["gridWidth"] <= metrics["viewportWidth"] + 1
                    for card in storage["cards"]:
                        assert card["headingHasCount"] is False
                        assert card["metaCount"] == 1
                        assert card["actionCount"] == 1
                        assert card["cleanInActions"] is True
                        assert card["right"] <= metrics["viewportWidth"] + 1
                elif tab == "monitors":
                    page.wait_for_function(
                        "() => document.querySelectorAll('#memberRows tr').length >= 1",
                        timeout=5000,
                    )
                    members = page.evaluate(
                        """() => {
                            const table = document.querySelector('.member-table');
                            const row = document.querySelector('#memberRows tr');
                            const cells = row ? [...row.children] : [];
                            const accountActions = [...document.querySelectorAll('#accountRows td:last-child .admin-row-actions')];
                            const memberActions = [...document.querySelectorAll('#memberRows td:last-child .admin-row-actions')];
                            const box = (el) => {
                                const r = el.getBoundingClientRect();
                                return {left: r.left, right: r.right, top: r.top, bottom: r.bottom};
                            };
                            return {
                                tableDisplay: table ? getComputedStyle(table).display : '',
                                tableMinWidth: table ? getComputedStyle(table).minWidth : '',
                                rowDisplay: row ? getComputedStyle(row).display : '',
                                areas: row ? getComputedStyle(row).gridTemplateAreas : '',
                                labels: cells.map((cell) => cell.dataset.label || ''),
                                accountActionCount: accountActions.length,
                                memberActionCount: memberActions.length,
                                accountActionBoxes: accountActions.map(box),
                                memberActionBoxes: memberActions.map(box),
                                tableBox: table ? box(table) : null,
                            };
                        }"""
                    )
                    assert members["accountActionCount"] >= 1
                    assert members["memberActionCount"] >= 1
                    if viewport_name == "mobile":
                        assert members["tableMinWidth"] in {"0px", "auto"}
                        assert members["rowDisplay"] == "grid"
                        assert members["labels"] == ["成员 ID", "姓名", "社交账号绑定", "Message 账号", "订阅状态", "操作"]
                        assert '"social social"' in members["areas"]
                        assert '"account subscription"' in members["areas"]
                        assert members["tableBox"]["right"] <= metrics["viewportWidth"] + 1
                    else:
                        assert members["tableMinWidth"] == "1000px"
                        assert members["rowDisplay"] == "table-row"
                    for action_box in members["accountActionBoxes"] + members["memberActionBoxes"]:
                        assert action_box["right"] >= action_box["left"]
                elif tab == "channels":
                    actions = page.evaluate(
                        """() => [...document.querySelectorAll('#qqBotRows .admin-row-actions, #napcatRows .admin-row-actions, #tgBotRows .admin-row-actions, #cmdOpenidList .admin-row-actions')].map((wrap) => ({
                            display: getComputedStyle(wrap).display,
                            flexWrap: getComputedStyle(wrap).flexWrap,
                            wrap: (() => { const r = wrap.getBoundingClientRect(); return {left: r.left, right: r.right}; })(),
                            buttons: [...wrap.querySelectorAll('.btn')].map((button) => {
                                const r = button.getBoundingClientRect();
                                return {width: r.width, height: r.height, top: r.top};
                            }),
                        }))"""
                    )
                    assert actions
                    for action in actions:
                        assert action["display"] in {"inline-flex", "flex"}
                        assert action["flexWrap"] == "nowrap"
                        assert action["buttons"]
                        assert all(button["width"] > 0 and button["height"] > 0 for button in action["buttons"])
                        assert action["wrap"]["right"] >= action["wrap"]["left"]
                elif tab == "social":
                    schedule = page.evaluate(
                        """() => {
                            const groups = [...document.querySelectorAll('.admin-schedule-group')];
                            return groups.map((group) => {
                                const fields = [...group.querySelectorAll('.admin-schedule-fields > .schedule-field')];
                                const inputs = fields.map((field) => field.querySelector('input'));
                                const rects = inputs.map((input) => {
                                    const r = input.getBoundingClientRect();
                                    return {left: r.left, right: r.right, top: r.top, bottom: r.bottom};
                                });
                                const gr = group.getBoundingClientRect();
                                return {fieldCount: fields.length, rects, groupRight: gr.right};
                            });
                        }"""
                    )
                    assert len(schedule) == 2
                    for group in schedule:
                        assert group["fieldCount"] == 2
                        assert group["rects"][1]["top"] - group["rects"][0]["top"] <= 2
                        assert all(rect["right"] <= group["groupRight"] + 1 for rect in group["rects"])
                elif tab in {"users", "advanced"}:
                    # 用户和历史记录表的动态操作也必须通过统一操作组承载；
                    # 历史记录为空时允许没有操作行，但一旦有行就不能回退到空格分隔按钮。
                    selector = "#userRows .admin-row-actions" if tab == "users" else "#historyRows .admin-row-actions"
                    row_actions = page.evaluate(
                        """(selector) => [...document.querySelectorAll(selector)].map((wrap) => ({
                            display: getComputedStyle(wrap).display,
                            flexWrap: getComputedStyle(wrap).flexWrap,
                            buttonCount: wrap.querySelectorAll('.btn').length,
                        }))""",
                        selector,
                    )
                    for action in row_actions:
                        assert action["display"] in {"inline-flex", "flex"}
                        assert action["flexWrap"] == "nowrap"
                        assert action["buttonCount"] >= 1
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
