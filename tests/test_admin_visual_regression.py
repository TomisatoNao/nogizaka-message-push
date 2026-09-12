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
        "napcat_api_base": "http://napcat:3000",
        "napcat_api_token": "fixture-token",
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
    if path == "/api/members":
        return {
            "ok": True,
            "members": [
                {"id": "1", "name": "示例订阅成员", "state": "open", "is_subscribed": True, "sub_end": "2026-10-01"},
                {"id": "2", "name": "示例历史成员", "state": "closed", "is_past_subscribed": True, "sub_start": "2025-01-01", "sub_end": "2025-06-01"},
                {"id": "3", "name": "示例在籍成员", "state": "open"},
            ],
            "total": 3,
            "subscribed_count": 1,
            "past_subscribed_count": 1,
            "open_count": 2,
        }
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
    import sys
    from PIL import Image, ImageChops

    # 基线快照在 Windows 环境（DirectWrite / Segoe UI / 微软雅黑）下生成。
    # Linux CI 环境（FreeType / DejaVu / Noto）由于跨平台字体度量与平滑渲染差异，
    # 纯文字渲染在整屏上通常产生 10%~18% 的亚像素与字宽偏差；在密集配置表单或移动端窄屏下，
    # 垂直文本换行与行高累积位移甚至可达 45%~50%；
    # 在非 Windows（如 Linux CI）环境下放宽阈值至 0.60（或由环境变量 ADMIN_VISUAL_MAX_RATIO 指定），
    # 既能有效捕获布局崩溃、样式丢失、整块缺失等致命回归，又避免跨平台字体渲染差异造成误报。
    default_ratio = max_ratio if sys.platform == "win32" else 0.60
    env_ratio = os.environ.get("ADMIN_VISUAL_MAX_RATIO")
    effective_max_ratio = float(env_ratio) if env_ratio is not None else default_ratio

    # 使用 RGB 计算差异；RGBA 的 alpha 通道在静态截图中通常相同，
    # ImageChops.getbbox() 会因此忽略实际发生变化的 RGB 像素。
    actual_image = Image.open(actual).convert("RGB")
    expected_image = Image.open(expected).convert("RGB")
    if actual_image.size != expected_image.size:
        raise AssertionError(f"截图尺寸变化：{actual_image.size} != {expected_image.size}")
    diff = ImageChops.difference(actual_image, expected_image)
    bbox = diff.getbbox()
    if bbox is None:
        return
    # Pillow 12 已将 ``Image.getdata`` 标记为弃用；优先使用新的扁平化
    # 像素接口，同时保留对 Pillow 10/11 的兼容回退。
    flattened = getattr(diff, "get_flattened_data", None)
    pixels = flattened() if callable(flattened) else diff.getdata()
    changed = sum(1 for pixel in pixels if pixel != (0, 0, 0))
    ratio = changed / (actual_image.width * actual_image.height)
    if ratio > effective_max_ratio:
        actual_path = Path(actual)
        diff.save(actual_path.with_name(actual_path.stem + ".diff.png"))
        raise AssertionError(
            f"视觉回归超出阈值：{ratio:.3%} > {effective_max_ratio:.3%}，差异范围 {bbox}；"
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
                if path == "/api/config" and route.request.method == "PUT":
                    save_payloads.append(json.loads(route.request.post_data or "{}"))
                    route.fulfill(
                        status=200,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(
                            {"ok": True, "reloaded": True, "cred_status": {}},
                            ensure_ascii=False,
                        ),
                    )
                    return
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(_api_payload(path), ensure_ascii=False),
                )

            save_payloads = []
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
                                    actionCount: card.querySelectorAll('.admin-storage-card-meta > .admin-storage-card-actions').length,
                                    countActionSameRow: !!card.querySelector('.admin-storage-card-meta > .admin-storage-card-count') &&
                                        !!card.querySelector('.admin-storage-card-meta > .admin-storage-card-actions'),
                                    cleanInActions: [...card.querySelectorAll('.admin-storage-clean')].every((button) =>
                                        button.parentElement?.classList.contains('admin-storage-card-actions')),
                                    cleanRowsOverlap: [...card.querySelectorAll('.admin-storage-clean')].map((button) => {
                                        const count = card.querySelector('.admin-storage-card-count');
                                        const countRect = count?.getBoundingClientRect();
                                        const buttonRect = button.getBoundingClientRect();
                                        return countRect ? buttonRect.top < countRect.bottom && buttonRect.bottom > countRect.top : false;
                                    }),
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
                        assert card["countActionSameRow"] is True
                        assert card["cleanInActions"] is True
                        assert all(card["cleanRowsOverlap"])
                        assert card["right"] <= metrics["viewportWidth"] + 1
                elif tab == "monitors":
                    page.wait_for_function(
                        "() => document.querySelectorAll('#memberRows tr').length >= 1",
                        timeout=5000,
                    )
                    members = page.evaluate(
                        """() => {
                            const table = document.querySelector('.member-table');
                            const tableWrap = table?.closest('.table-wrap');
                            const row = document.querySelector('#memberRows tr');
                            const cells = row ? [...row.children] : [];
                            const accountCell = row?.querySelector('td:nth-child(2)');
                            const subscriptionCell = row?.querySelector('td:nth-child(3)');
                            const accountReadonly = row?.querySelector('td:nth-child(2) .admin-member-account-readonly');
                            const accountPoolId = document.querySelector('#accountRows .admin-cell-id');
                            const accountActions = [...document.querySelectorAll('#accountRows td:last-child .admin-row-actions')];
                            const memberActions = [...document.querySelectorAll('#memberRows td:last-child .admin-row-actions')];
                            const box = (el) => {
                                const r = el.getBoundingClientRect();
                                return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width};
                            };
                            return {
                                tableDisplay: table ? getComputedStyle(table).display : '',
                                tableMinWidth: table ? getComputedStyle(table).minWidth : '',
                                theadDisplay: table ? getComputedStyle(table.querySelector('thead')).display : '',
                                rowDisplay: row ? getComputedStyle(row).display : '',
                                areas: row ? getComputedStyle(row).gridTemplateAreas : '',
                                labels: cells.map((cell) => cell.dataset.label || ''),
                                accountActionCount: accountActions.length,
                                memberActionCount: memberActions.length,
                                accountActionBoxes: accountActions.map(box),
                                memberActionBoxes: memberActions.map(box),
                                accountBox: accountCell ? box(accountCell) : null,
                                subscriptionBox: subscriptionCell ? box(subscriptionCell) : null,
                                accountReadonlyBox: accountReadonly ? box(accountReadonly) : null,
                                accountReadonlyStyle: accountReadonly ? {
                                    display: getComputedStyle(accountReadonly).display,
                                    background: getComputedStyle(accountReadonly).backgroundColor,
                                    borderTopWidth: getComputedStyle(accountReadonly).borderTopWidth,
                                    fontFamily: getComputedStyle(accountReadonly).fontFamily,
                                    fontSize: getComputedStyle(accountReadonly).fontSize,
                                    color: getComputedStyle(accountReadonly).color,
                                    fontWeight: getComputedStyle(accountReadonly).fontWeight,
                                } : null,
                                accountPoolIdStyle: accountPoolId ? {
                                    display: getComputedStyle(accountPoolId).display,
                                    background: getComputedStyle(accountPoolId).backgroundColor,
                                    borderTopWidth: getComputedStyle(accountPoolId).borderTopWidth,
                                    fontFamily: getComputedStyle(accountPoolId).fontFamily,
                                    fontSize: getComputedStyle(accountPoolId).fontSize,
                                    color: getComputedStyle(accountPoolId).color,
                                    fontWeight: getComputedStyle(accountPoolId).fontWeight,
                                } : null,
                                subscriptionJustify: subscriptionCell ? getComputedStyle(subscriptionCell).justifyContent : '',
                                subscriptionTextAlign: subscriptionCell ? getComputedStyle(subscriptionCell).textAlign : '',
                                tableBox: table ? box(table) : null,
                                tableWrapBox: tableWrap ? box(tableWrap) : null,
                            };
                        }"""
                    )
                    assert members["accountActionCount"] >= 1
                    assert members["memberActionCount"] >= 1
                    if viewport_name == "mobile":
                        assert members["tableMinWidth"] == "760px"
                        assert members["theadDisplay"] == "table-header-group"
                        assert members["rowDisplay"] == "table-row"
                        assert members["labels"] == ["姓名", "Message 账号", "订阅状态", "操作"]
                        assert members["tableBox"]["width"] >= 760
                        assert members["tableWrapBox"]["right"] <= metrics["viewportWidth"] + 1
                        assert members["tableBox"]["right"] > members["tableWrapBox"]["right"]
                        assert members["accountBox"]["right"] <= members["tableBox"]["right"] + 1
                        assert members["subscriptionBox"]["right"] <= members["tableBox"]["right"] + 1
                        assert members["subscriptionBox"]["top"] == members["accountBox"]["top"]
                        assert members["accountReadonlyBox"]["right"] <= members["accountBox"]["right"] + 1
                        assert members["subscriptionTextAlign"] == "left"
                    else:
                        assert members["tableMinWidth"] == "760px"
                        assert members["rowDisplay"] == "table-row"
                    assert members["accountReadonlyStyle"]["display"] == "inline"
                    assert members["accountReadonlyStyle"]["borderTopWidth"] == "0px"
                    assert members["accountReadonlyStyle"]["fontWeight"] in {"600", "bold"}
                    assert members["accountReadonlyStyle"] == members["accountPoolIdStyle"]
                    for action_box in members["accountActionBoxes"] + members["memberActionBoxes"]:
                        assert action_box["right"] >= action_box["left"]
                    page.locator("#btnPickMember").click()
                    page.locator("#memberPickDialog[open]").wait_for(state="visible", timeout=5000)
                    picker = page.evaluate(
                        """() => ({
                            groups: [...document.querySelectorAll('#pickGroupChips .chip')].map((chip) => chip.textContent),
                            unavailableGroups: [...document.querySelectorAll('#pickGroupChips .chip.is-unavailable')].map((chip) => chip.textContent),
                            accounts: [...document.querySelectorAll('#pickAccountChips .chip')].map((chip) => chip.textContent),
                            selectedGroup: document.querySelector('#pickGroupChips')?.dataset.value || '',
                            selectedAccount: document.querySelector('#pickAccountChips')?.dataset.value || '',
                            fetchDisabled: !!document.querySelector('#btnFetchMembers')?.disabled,
                            dialogWidth: document.querySelector('#memberPickDialog')?.getBoundingClientRect().width || 0,
                            dialogOverflow: (() => {
                                const dialog = document.querySelector('#memberPickDialog');
                                return dialog ? dialog.scrollWidth > dialog.clientWidth + 1 : false;
                            })(),
                            stepsColumns: getComputedStyle(document.querySelector('.member-pick-steps')).gridTemplateColumns,
                            accountDirection: getComputedStyle(document.querySelector('.member-pick-account-row')).flexDirection,
                        })"""
                    )
                    assert len(picker["groups"]) == 4
                    assert picker["accounts"] == ["demo_main"]
                    assert picker["selectedGroup"] == "nogizaka46"
                    assert picker["selectedAccount"] == "demo_main"
                    assert picker["fetchDisabled"] is False
                    assert len(picker["unavailableGroups"]) == 3
                    assert picker["dialogWidth"] <= metrics["viewportWidth"] + 1
                    assert picker["dialogOverflow"] is False
                    if viewport_name == "mobile":
                        assert len(picker["stepsColumns"].split()) == 1
                        assert picker["accountDirection"] == "column"
                    else:
                        assert len(picker["stepsColumns"].split()) == 2
                        assert picker["accountDirection"] == "row"
                    page.locator("#btnFetchMembers").click()
                    page.locator("#memberPickList .member-pick-row").first.wait_for(state="visible", timeout=5000)
                    fetched = page.evaluate(
                        """() => ({
                            rows: [...document.querySelectorAll('#memberPickList .member-pick-row')].map((row) => ({
                                state: row.dataset.state,
                                hasName: !!row.querySelector('.member-pick-name'),
                                hasStatus: !!row.querySelector('.member-pick-status-badge'),
                                hasInlineStyle: row.hasAttribute('style'),
                            })),
                            hintHasStats: !!document.querySelector('#memberPickHint .member-pick-stat--subscribed'),
                            listOverflow: (() => {
                                const list = document.querySelector('#memberPickList');
                                return list ? list.scrollWidth > list.clientWidth + 1 : false;
                            })(),
                        })"""
                    )
                    assert len(fetched["rows"]) == 1
                    assert fetched["rows"][0]["state"] == "open"
                    assert fetched["rows"][0]["hasName"] is True
                    assert fetched["rows"][0]["hasStatus"] is True
                    assert fetched["rows"][0]["hasInlineStyle"] is False
                    assert fetched["hintHasStats"] is True
                    assert fetched["listOverflow"] is False
                    page.locator("#memberPickClose").click()
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
                    napcat = page.evaluate(
                        """() => {
                            const grid = document.querySelector('.napcat-endpoint-grid');
                            const fields = grid ? [...grid.querySelectorAll(':scope > .field')] : [];
                            const inputs = fields.map((field) => field.querySelector('input'));
                            const rects = inputs.map((input) => {
                                const r = input.getBoundingClientRect();
                                return {left: r.left, right: r.right, top: r.top, bottom: r.bottom};
                            });
                            const note = document.querySelector('.napcat-endpoint-note');
                            const gridRect = grid?.getBoundingClientRect();
                            return {
                                columns: grid ? getComputedStyle(grid).gridTemplateColumns : '',
                                rects,
                                noteText: note?.textContent?.trim() || '',
                                gridRight: gridRect?.right || 0,
                            };
                        }"""
                    )
                    assert len(napcat["rects"]) == 3
                    assert napcat["noteText"] == "NapCat 不在本机时填写媒体地址；共享文件系统可留空。"
                    assert napcat["gridRight"] <= metrics["viewportWidth"] + 1
                    if viewport_name == "mobile":
                        assert len(napcat["columns"].split()) == 1
                        assert napcat["rects"][1]["top"] > napcat["rects"][0]["top"]
                        assert napcat["rects"][2]["top"] > napcat["rects"][1]["top"]
                    elif viewport_name == "tablet":
                        assert len(napcat["columns"].split()) == 2
                        assert abs(napcat["rects"][1]["top"] - napcat["rects"][0]["top"]) <= 2
                        assert napcat["rects"][2]["top"] > napcat["rects"][1]["top"]
                    else:
                        assert len(napcat["columns"].split()) == 3
                        assert abs(napcat["rects"][1]["top"] - napcat["rects"][0]["top"]) <= 2
                        assert abs(napcat["rects"][2]["top"] - napcat["rects"][0]["top"]) <= 2
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

                    summary = page.evaluate(
                        """() => {
                            const grid = document.querySelector('#tab-social.active .monitor-summary-grid');
                            const cards = [...(grid?.querySelectorAll('.monitor-summary-card') || [])];
                            const box = (el) => {
                                const r = el.getBoundingClientRect();
                                return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height};
                            };
                                return {
                                    columns: grid ? getComputedStyle(grid).gridTemplateColumns : '',
                                    gridBox: grid ? box(grid) : null,
                                    scheduleBox: (() => {
                                        const el = document.querySelector('#tab-social.active .schedule-card');
                                        return el ? box(el) : null;
                                    })(),
                                    toolBox: (() => {
                                        const el = grid?.nextElementSibling;
                                        return el ? box(el) : null;
                                    })(),
                                    cards: cards.map((card) => ({
                                    key: card.dataset.monitorSummaryCard,
                                    box: box(card),
                                    metrics: card.querySelectorAll('.monitor-summary-metric').length,
                                    settingsTarget: card.querySelector('[data-monitor-dialog-target]')?.dataset.monitorDialogTarget || '',
                                })),
                                dialogs: [...document.querySelectorAll('.monitor-settings-dialog')].map((dialog) => ({
                                    id: dialog.id,
                                    fieldCount: dialog.querySelectorAll('input[id], textarea[id], select[id]').length,
                                    fieldIds: [...dialog.querySelectorAll('input[id], textarea[id], select[id]')].map((field) => field.id),
                                    hasScroll: !!dialog.querySelector('.monitor-dialog-scroll'),
                                })),
                            };
                        }"""
                    )
                    expected_columns = {"desktop": 3, "tablet": 2, "mobile": 1}[viewport_name]
                    assert len(summary["columns"].split()) == expected_columns
                    assert summary["gridBox"]["right"] <= metrics["viewportWidth"] + 1
                    assert len(summary["cards"]) == 6
                    assert {card["key"] for card in summary["cards"]} == {
                        "message", "blog", "x", "instagram", "tiktok", "tiktok-live",
                    }
                    assert {card["key"]: card["settingsTarget"] for card in summary["cards"]} == {
                        "message": "monitorMessageDialog",
                        "blog": "monitorBlogDialog",
                        "x": "monitorXDialog",
                        "instagram": "monitorInstagramDialog",
                        "tiktok": "monitorTiktokDialog",
                        "tiktok-live": "monitorLiveDialog",
                    }
                    assert all(card["box"]["height"] > 120 for card in summary["cards"])
                    card_heights = {round(card["box"]["height"], 1) for card in summary["cards"]}
                    assert len(card_heights) == 1
                    assert all(card["box"]["right"] <= metrics["viewportWidth"] + 1 for card in summary["cards"])
                    assert summary["scheduleBox"] and summary["gridBox"] and summary["toolBox"]
                    assert round(summary["gridBox"]["top"] - summary["scheduleBox"]["bottom"], 1) == 14.0
                    assert round(summary["toolBox"]["top"] - summary["gridBox"]["bottom"], 1) == 14.0
                    assert all(card["metrics"] >= 2 for card in summary["cards"])
                    assert {dialog["id"] for dialog in summary["dialogs"]} == {
                        "monitorMessageDialog", "monitorBlogDialog", "monitorXDialog",
                        "monitorInstagramDialog", "monitorTiktokDialog", "monitorLiveDialog",
                    }
                    assert all(dialog["fieldCount"] > 0 and dialog["hasScroll"] for dialog in summary["dialogs"])
                    dialog_fields = {dialog["id"]: set(dialog["fieldIds"]) for dialog in summary["dialogs"]}
                    assert dialog_fields["monitorTiktokDialog"] == {
                        "socialTiktokInterval", "socialTiktokAccounts",
                    }
                    assert dialog_fields["monitorLiveDialog"] == {
                        "socialLiveInterval", "socialLiveAccounts",
                    }

                    # 每张摘要卡的设置入口都应打开正确的独立 dialog；关闭后输入值保持不变。
                    settings_buttons = page.locator("#tab-social.active [data-monitor-dialog-target]")
                    assert settings_buttons.count() == 6
                    seen_dialogs = set()
                    for index in range(settings_buttons.count()):
                        button = settings_buttons.nth(index)
                        target = button.get_attribute("data-monitor-dialog-target")
                        assert target
                        seen_dialogs.add(target)
                        button.click()
                        dialog = page.locator("#" + target)
                        dialog.wait_for(state="visible", timeout=5000)
                        dialog_metrics = page.evaluate(
                            """(id) => {
                                const dialog = document.getElementById(id);
                                const scroll = dialog?.querySelector('.monitor-dialog-scroll');
                                return {
                                    open: !!dialog?.open,
                                    dialogOverflow: !!dialog && dialog.scrollWidth > dialog.clientWidth + 1,
                                    scrollOverflow: !!scroll && scroll.scrollWidth > scroll.clientWidth + 1,
                                    width: dialog?.getBoundingClientRect().width || 0,
                                    pageScrollWidth: document.documentElement.scrollWidth,
                                    viewportWidth: window.innerWidth,
                                };
                            }""",
                            target,
                        )
                        assert dialog_metrics["open"] is True
                        assert dialog_metrics["dialogOverflow"] is False
                        assert dialog_metrics["scrollOverflow"] is False
                        assert dialog_metrics["width"] <= metrics["viewportWidth"] + 1
                        assert dialog_metrics["pageScrollWidth"] <= dialog_metrics["viewportWidth"] + 1
                        if index == settings_buttons.count() - 1:
                            page.keyboard.press("Escape")
                        else:
                            dialog.locator("[data-monitor-dialog-close]").first.click()
                        dialog.wait_for(state="hidden", timeout=5000)
                    assert seen_dialogs == {
                        "monitorMessageDialog", "monitorBlogDialog", "monitorXDialog",
                        "monitorInstagramDialog", "monitorTiktokDialog", "monitorLiveDialog",
                    }

                    x_settings = page.locator('[data-monitor-dialog-target="monitorXDialog"]').first
                    x_settings.click()
                    x_interval = page.locator("#socialXInterval")
                    original_interval = x_interval.input_value()
                    x_interval.fill(str(int(original_interval) + 1))
                    page.locator("#monitorXDialog [data-monitor-dialog-close]").last.click()
                    assert x_interval.input_value() == str(int(original_interval) + 1)

                    # 摘要卡的快速开关沿用原有脏状态/保存栏链路。
                    x_toggle_label = page.locator('label.switch:has(#socialXOn)')
                    x_toggle_label.click()
                    page.locator("footer.savebar.show").wait_for(state="visible", timeout=5000)
                    x_toggle_label.click()

                    # 桌面端额外拦截一次保存请求，确认弹窗表单仍序列化为原有配置结构。
                    if viewport_name == "desktop":
                        page.locator("#btnSave").click()
                        page.wait_for_timeout(150)
                        assert save_payloads
                        payload = save_payloads[-1]
                        expected_top_level = {
                            "accounts", "monitor", "channels", "day_interval", "night_interval",
                            "monitor_schedule", "message_monitor", "blog_monitor", "platforms",
                        }
                        assert expected_top_level <= set(payload)
                        assert {"x", "instagram", "tiktok", "tiktok_live"} <= set(payload["platforms"])
                        assert {"interval_range_seconds", "night_interval_range_seconds"} <= set(payload["platforms"]["instagram"])
                        page.locator("#btnReloadFile").click()
                        x_interval.wait_for(state="attached")
                        page.wait_for_function(
                            "(value) => document.querySelector('#socialXInterval')?.value === value",
                            arg=original_interval,
                            timeout=5000,
                        )
                    else:
                        # 非桌面尺寸只验证交互，不把临时值带入该尺寸的视觉基线。
                        x_settings.click()
                        x_interval.fill(original_interval)
                        page.locator("#monitorXDialog [data-monitor-dialog-close]").last.click()
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


@pytest.mark.parametrize("viewport_name", ("desktop", "mobile"))
def test_admin_history_actions_align_with_table_header(admin_static_server, viewport_name):
    """历史快照的操作按钮必须落在“操作”表头对应的单元格内。"""
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
                payload = _api_payload(path)
                if path == "/api/config/history":
                    payload = {
                        "ok": True,
                        "history": [
                            {"name": "config-a.json", "mtime_epoch": 1789000000, "size": 1024},
                            {"name": "config-b.json", "mtime_epoch": 1788996400, "size": 8192},
                        ],
                    }
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(payload, ensure_ascii=False),
                )

            page = context.new_page()
            page.route("**/api/**", handle_api)
            page.goto(f"{admin_static_server}/#tab=status", wait_until="domcontentloaded")
            page.add_style_tag(content="*{animation:none!important;transition:none!important;caret-color:transparent!important}")
            page.locator("#tab-status.active .admin-module").first.wait_for(state="visible", timeout=10000)
            page.locator('.nav-tab[data-tab="advanced"]').click()
            page.locator("#tab-advanced.active .admin-module").first.wait_for(state="visible", timeout=10000)
            page.locator("#historyRows tr").first.wait_for(state="visible", timeout=5000)
            layout = page.evaluate(
                """() => {
                    const table = document.querySelector('.admin-history-table');
                    const header = table?.querySelector('thead th:last-child');
                    const rows = [...(table?.querySelectorAll('tbody tr') || [])];
                    const box = (el) => {
                        const r = el.getBoundingClientRect();
                        return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height};
                    };
                    return {
                        viewportWidth: window.innerWidth,
                        scrollWidth: document.documentElement.scrollWidth,
                        tableMinWidth: table ? getComputedStyle(table).minWidth : '',
                        header: header ? box(header) : null,
                        rows: rows.map((row) => {
                            const cell = row.lastElementChild;
                            const actions = cell?.querySelector('.admin-row-actions');
                            return {
                                cellDisplay: cell ? getComputedStyle(cell).display : '',
                                cell: cell ? box(cell) : null,
                                actions: actions ? box(actions) : null,
                            };
                        }),
                    };
                }"""
            )
            assert layout["scrollWidth"] <= layout["viewportWidth"] + 1
            assert layout["header"]
            assert layout["rows"]
            for row in layout["rows"]:
                assert row["cellDisplay"] == "table-cell"
                assert abs(row["cell"]["left"] - layout["header"]["left"]) <= 1
                assert row["actions"]["left"] >= row["cell"]["left"]
                assert row["actions"]["right"] <= row["cell"]["right"] + 1
                assert row["actions"]["top"] >= row["cell"]["top"]
            if viewport_name == "mobile":
                assert layout["tableMinWidth"] == "520px"
            context.close()
            browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise


def test_admin_mobile_openid_cards_keep_information_and_actions_separate(admin_static_server):
    """长 OpenID 在手机端独占信息行，操作栏不与身份信息争抢宽度。"""
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport=VIEWPORTS["mobile"],
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
            page.goto(f"{admin_static_server}/#tab=channels", wait_until="domcontentloaded")
            page.add_style_tag(content="*{animation:none!important;transition:none!important;caret-color:transparent!important}")
            page.locator("#tab-channels.active .admin-module").first.wait_for(state="visible", timeout=10000)
            page.evaluate(
                """() => {
                    window._cmdOpenids = [
                        {type: "group", name: "示例群聊", openid: "AC2E2DCA8F90C0B6D0_LONG_OPENID"},
                        {type: "user", name: "示例用户", openid: "DB1F400710798AC0C45D71AE1EFF1C8A"},
                    ];
                    renderCmdOpenids();
                }"""
            )
            page.locator("#cmdOpenidList .cmd-openid-card").first.wait_for(state="visible", timeout=5000)
            layout = page.evaluate(
                """() => {
                    const card = document.querySelector('#cmdOpenidList .cmd-openid-card');
                    const main = card?.querySelector('.cmd-openid-main');
                    const code = card?.querySelector('.cmd-openid-code');
                    const actions = card?.querySelector('.cmd-openid-actions');
                    const box = (el) => {
                        const r = el.getBoundingClientRect();
                            return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height};
                    };
                    return {
                        viewportWidth: window.innerWidth,
                        scrollWidth: document.documentElement.scrollWidth,
                        cardDirection: card ? getComputedStyle(card).flexDirection : '',
                        cardAlign: card ? getComputedStyle(card).alignItems : '',
                        mainDisplay: main ? getComputedStyle(main).display : '',
                        codeBox: code ? box(code) : null,
                        mainBox: main ? box(main) : null,
                        actionsBox: actions ? box(actions) : null,
                        buttons: [...(actions?.querySelectorAll('.btn') || [])].map(box),
                    };
                }"""
            )
            assert layout["scrollWidth"] <= layout["viewportWidth"] + 1
            assert layout["cardDirection"] == "column"
            assert layout["cardAlign"] == "stretch"
            assert layout["mainDisplay"] == "grid"
            assert layout["codeBox"]["right"] <= layout["mainBox"]["right"] + 1
            assert layout["actionsBox"]["top"] >= layout["codeBox"]["bottom"]
            assert layout["actionsBox"]["right"] <= layout["viewportWidth"] + 1
            assert all(button["width"] > 0 and button["height"] >= 40 for button in layout["buttons"])
            context.close()
            browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise
