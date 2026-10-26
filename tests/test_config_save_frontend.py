from __future__ import annotations

import copy
import importlib.util
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "src" / "webui_static"
FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "admin_visual_regression_fixtures", ROOT / "tests" / "test_admin_visual_regression.py"
)
assert FIXTURE_SPEC and FIXTURE_SPEC.loader
ADMIN_FIXTURES = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(ADMIN_FIXTURES)
VIEWPORTS = {"desktop": {"width": 1440, "height": 1000}, "mobile": {"width": 390, "height": 844}}
CONFIG_SECRET = "CONFIG_SECRET_MUST_NOT_APPEAR_IN_REVIEW"
BOT_SECRET = "BOT_SECRET_MUST_NOT_APPEAR_IN_REVIEW"
HISTORY_SECRET = "HISTORY_SECRET_MUST_NOT_APPEAR_IN_REVIEW"


class _StaticHandler(SimpleHTTPRequestHandler):
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
def admin_config_server():
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
    config = ADMIN_FIXTURES._fixture_config()
    config["napcat_api_token"] = CONFIG_SECRET
    return config


def _page_in_context(context, server, handler, theme: str):
    page = context.new_page()
    page.set_default_timeout(5000)
    page.set_default_navigation_timeout(8000)
    page.route("**/api/**", handler)
    page.goto(f"{server}/#tab=channels", wait_until="domcontentloaded", timeout=8000)
    page.wait_for_function("document.querySelector('#napcatApi')?.value === 'http://napcat:3000'", timeout=5000)
    page.locator('.header-nav .nav-tab[data-tab="channels"], nav.tabs button[data-tab="channels"]').first.click()
    page.locator("#napcatApi").wait_for(state="visible")
    page.evaluate("theme => { document.documentElement.dataset.theme = theme; }", theme)
    return page


def _new_page(playwright, server, handler, viewport_name: str, theme: str):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(viewport=VIEWPORTS[viewport_name], color_scheme=theme, locale="zh-CN")
    context.add_init_script(f"localStorage.setItem('sakamichiTheme', {json.dumps(theme)});")
    return browser, context, _page_in_context(context, server, handler, theme)


def _fulfill(route, payload: dict, status: int = 200):
    route.fulfill(
        status=status,
        content_type="application/json; charset=utf-8",
        body=json.dumps(payload, ensure_ascii=False),
    )


def _response_for(path: str, config: dict, revision: str = "revision-local-1") -> dict:
    payload = ADMIN_FIXTURES._api_payload(path)
    if path == "/api/config":
        payload["config"] = copy.deepcopy(config)
        payload["revision"] = revision
    return payload


@pytest.mark.parametrize(
    "viewport_name,theme",
    [("desktop", "light"), ("desktop", "dark"), ("mobile", "light"), ("mobile", "dark")],
)
def test_save_review_can_cancel_then_commit_without_secret_values(
    admin_config_server, viewport_name: str, theme: str
):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"writes": []}
    reload_id = "d" * 64

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            state["writes"].append((route.request.method, path, body))
            _fulfill(route, {
                "ok": True, "saved": True, "revision": "revision-local-2",
                "reload": {"id": reload_id, "config": "loaded", "components": "pending", "application": {"id": reload_id, "state": "pending", "config": "loaded", "components": {}}},
                "cred_status": {}, "qq_bot_status": [], "env_status": {},
            })
            return
        if path == "/api/status" and "reload_id=" in route.request.url:
            _fulfill(route, {"ok": True, "config_application": {"id": reload_id, "state": "pending", "config": "loaded", "components": {}}})
            return
        _fulfill(route, _response_for(path, config))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page = _new_page(p, admin_config_server, handle, viewport_name, theme)
            try:
                page.locator("#napcatApi").fill("http://napcat-next:3000")
                assert page.locator("#btnSave").is_enabled()
                page.locator("#btnSave").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                preview = page.locator("#confirmBody").inner_text()
                assert "推送通道" in preview
                assert "OneBot API" in preview
                assert "修改" in preview
                assert CONFIG_SECRET not in preview
                assert "http://napcat-next:3000" not in preview
                box = page.locator("#confirmDialog").bounding_box()
                assert box and box["x"] >= 0 and box["x"] + box["width"] <= VIEWPORTS[viewport_name]["width"] + 1
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.locator("#confirmNo").click()
                assert state["writes"] == []
                assert page.locator("#napcatApi").input_value() == "http://napcat-next:3000"

                page.locator("#btnSave").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                page.locator("#confirmYes").click()
                page.locator("#saveOutcome").wait_for(state="visible")
                assert len(state["writes"]) == 1
                _, path, body = state["writes"][0]
                assert path == "/api/config"
                assert body["expected_revision"] == "revision-local-1"
                assert body["napcat_api_base"] == "http://napcat-next:3000"
                assert "revision-local-1" not in page.locator("body").inner_text()
                assert "已保存" in page.locator("#saveOutcome").inner_text()
                assert "配置已加载" in page.locator("#saveOutcome").inner_text()
                assert "运行组件重配待确认" in page.locator("#saveOutcome").inner_text()
                assert page.evaluate("localStorage.getItem('sakamichiConfigRevision')") is None
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise


def test_instagram_cookie_save_and_clear_use_current_revision(admin_config_server):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"revision": "revision-local-1", "writes": []}

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "GET":
            _fulfill(route, _response_for(path, config, state["revision"]))
            return
        if path == "/api/social/ig_session" and route.request.method == "GET":
            _fulfill(route, {
                "ok": True, "configured": True, "cookie_names": ["sessionid"],
                "user_id": "", "assessment": {"safe_enough": True, "level": "good"},
            })
            return
        if path in {"/api/social/ig_session", "/api/social/ig_session/clear"} and route.request.method == "POST":
            body = json.loads(route.request.post_data or "{}")
            state["writes"].append((path, copy.deepcopy(body)))
            if body.get("expected_revision") != state["revision"]:
                _fulfill(route, {"ok": False, "error": "revision_conflict", "revision": state["revision"]}, 409)
                return
            state["revision"] = "revision-local-" + ("2" if path.endswith("ig_session") else "3")
            if path.endswith("/clear"):
                _fulfill(route, {
                    "ok": True, "revision": state["revision"], "reloaded": True,
                    "status": {"configured": False},
                })
            else:
                _fulfill(route, {
                    "ok": True, "revision": state["revision"], "reloaded": True,
                    "health": {"valid": True, "detail": "mock health"},
                    "status": {"configured": True},
                })
            return
        _fulfill(route, _response_for(path, config, state["revision"]))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page = _new_page(p, admin_config_server, handle, "desktop", "light")
            try:
                page.evaluate("document.querySelector('#monitorInstagramDialog').showModal()")
                page.locator("#monitorInstagramDialog[open]").wait_for(state="visible")
                page.locator("#btnFillIgSession").click()
                page.locator("#igCookieTextarea").fill("sessionid=SESSION_COOKIE_PRIVATE; csrftoken=CSRF_COOKIE_PRIVATE")
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/social/ig_session" and response.request.method == "POST") as save_response:
                    page.locator("#btnIgCookieSave").click()
                assert save_response.value.status == 200
                save_path, save_body = state["writes"][0]
                assert save_path == "/api/social/ig_session"
                assert save_body["expected_revision"] == "revision-local-1"
                assert page.evaluate("configRevision === 'revision-local-2' && secretRevision === 'revision-local-2'")
                page.locator("#confirmDialog").wait_for(state="visible")
                page.locator("#confirmYes").click()

                page.locator("#btnClearIgSession").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/social/ig_session/clear" and response.request.method == "POST") as clear_response:
                    page.locator("#confirmYes").click()
                assert clear_response.value.status == 200
                clear_path, clear_body = state["writes"][1]
                assert clear_path == "/api/social/ig_session/clear"
                assert clear_body == {"expected_revision": "revision-local-2"}
                page.wait_for_function("configRevision === 'revision-local-3' && secretRevision === 'revision-local-3'")
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise


def test_instagram_cookie_clear_http_500_keeps_dialog_and_revision(admin_config_server):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"revision": "revision-local-1", "clear_body": None}

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "GET":
            _fulfill(route, _response_for(path, config, state["revision"]))
            return
        if path == "/api/social/ig_session" and route.request.method == "GET":
            _fulfill(route, {
                "ok": True, "configured": True, "cookie_names": ["sessionid"],
                "user_id": "", "assessment": {"safe_enough": True, "level": "good"},
            })
            return
        if path == "/api/social/ig_session/clear" and route.request.method == "POST":
            state["clear_body"] = json.loads(route.request.post_data or "{}")
            _fulfill(route, {
                "ok": False,
                "errors": ["simulated clear failure"],
                "revision": "revision-local-2",
            }, 500)
            return
        _fulfill(route, _response_for(path, config, state["revision"]))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page = _new_page(p, admin_config_server, handle, "desktop", "light")
            try:
                page.evaluate("document.querySelector('#monitorInstagramDialog').showModal()")
                page.locator("#monitorInstagramDialog[open]").wait_for(state="visible")
                page.locator("#btnFillIgSession").click()
                page.locator("#igCookieDialog[open]").wait_for(state="visible")
                page.locator("#btnIgCookieClear").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/social/ig_session/clear" and response.request.method == "POST") as clear_response:
                    page.locator("#confirmYes").click()
                assert clear_response.value.status == 500
                assert state["clear_body"] == {"expected_revision": "revision-local-1"}
                assert page.locator("#igCookieDialog[open]").is_visible()
                assert page.evaluate("configRevision === 'revision-local-1' && secretRevision === 'revision-local-1'")
                assert page.evaluate("configRevisionStale === false")
                outcome = page.locator("#saveMsg").inner_text()
                assert "清空失败" in outcome and "simulated clear failure" in outcome
                assert "已清空 Instagram Cookies" not in outcome
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise


@pytest.mark.parametrize("error_code", ["revision_conflict", "expected_revision_required"])
def test_revision_error_keeps_draft_and_never_reloads_automatically(admin_config_server, error_code: str):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"config_reads": 0, "writes": []}

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "GET":
            state["config_reads"] += 1
        if path == "/api/config" and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            state["writes"].append(body)
            _fulfill(route, {"ok": False, "error": error_code, "revision": "revision-server-2"}, 409 if error_code == "revision_conflict" else 428)
            return
        _fulfill(route, _response_for(path, config))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page = _new_page(p, admin_config_server, handle, "desktop", "dark")
            try:
                page.locator("#napcatApi").fill("http://my-local-draft:3000")
                page.locator("#btnSave").click()
                assert "OneBot API" in page.locator("#confirmBody").inner_text()
                page.locator("#confirmYes").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                page.locator("#confirmNo").click()
                assert page.locator("#napcatApi").input_value() == "http://my-local-draft:3000"
                assert page.locator("#btnSave").is_disabled()
                assert state["config_reads"] == 1
                assert len(state["writes"]) == 1
                assert state["writes"][0]["expected_revision"] == "revision-local-1"
                assert "页面草稿已保留" in page.locator("#saveOutcome").inner_text()
                assert "revision-server-2" not in page.locator("body").inner_text()
                assert page.url.endswith("#tab=channels")
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise


def test_combined_bot_secret_commit_and_history_preview_are_revision_safe(admin_config_server):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"writes": [], "config_reads": 0}
    history_name = "config-20261024-120000-000.json"

    def handle(route):
        path = urlsplit(route.request.url).path
        query = urlsplit(route.request.url).query
        if path == "/api/config" and route.request.method == "GET":
            state["config_reads"] += 1
        if path == "/api/config/commit" and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            state["writes"].append((path, body))
            _fulfill(route, {
                "ok": True, "saved": True, "revision": "revision-local-2",
                "reload": {"config": "loaded", "components": "pending"},
                "cred_status": {}, "qq_bot_status": [], "env_status": {},
            })
            return
        if path == "/api/config/restore" and route.request.method == "POST":
            body = json.loads(route.request.post_data or "{}")
            state["writes"].append((path, body))
            _fulfill(route, {
                "ok": True, "saved": True, "revision": "revision-local-3",
                "reload": {"config": "loaded", "components": "not_scheduled"},
                "cred_status": {}, "qq_bot_status": [],
            })
            return
        if path == "/api/config/history" and "name=" in query:
            _fulfill(route, {
                "ok": True, "name": history_name, "content_redacted": True,
                "restore_uses_server_snapshot": True,
                "content": json.dumps({"secret": HISTORY_SECRET, "alert_cooldown": 4800}),
                "preview": {"groups": [
                    {"label": "alert_cooldown", "changes": [{"path": "alert_cooldown", "kind": "changed"}]},
                    {"label": "napcat_api_token", "changes": [{"path": "napcat_api_token", "kind": "changed"}]},
                ]},
            })
            return
        if path == "/api/config/history":
            _fulfill(route, {"ok": True, "history": [{"name": history_name, "mtime_epoch": 1792824000, "size": 1024}]})
            return
        revision = "revision-local-2" if state["config_reads"] > 1 else "revision-local-1"
        _fulfill(route, _response_for(path, config, revision))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page = _new_page(p, admin_config_server, handle, "desktop", "light")
            try:
                page.evaluate("() => { queueBotSecretUpdate('DEMO_CLIENT_SECRET', '" + BOT_SECRET + "'); markDirty(); }")
                page.locator("#btnSave").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                preview = page.locator("#confirmBody").inner_text()
                assert "Bot 凭证内容（已隐藏）" in preview
                assert BOT_SECRET not in preview and CONFIG_SECRET not in preview
                page.locator("#confirmYes").click()
                page.locator("#saveOutcome").wait_for(state="visible")
                assert state["writes"]
                commit_path, commit_body = state["writes"][0]
                assert commit_path == "/api/config/commit"
                assert commit_body["expected_revision"] == "revision-local-1"
                assert commit_body["secret_updates"]["DEMO_CLIENT_SECRET"] == BOT_SECRET

                page.locator('.header-nav .nav-tab[data-tab="advanced"], nav.tabs button[data-tab="advanced"]').first.click()
                page.locator("#historyRows button").first.wait_for(state="visible")
                page.locator("#historyRows button").first.click()
                page.locator("#historyViewDialog").wait_for(state="visible")
                page.wait_for_function("document.querySelector('#historyViewContent').textContent.length > 0")
                history_text = page.locator("#historyViewContent").inner_text()
                assert "告警间隔" in history_text
                assert "凭证内容（已隐藏）" in history_text
                assert HISTORY_SECRET not in history_text
                assert CONFIG_SECRET not in history_text

                page.locator("#historyViewRestore").click()
                confirm_text = page.locator("#confirmBody").inner_text()
                assert "恢复到" in confirm_text
                assert "当前版本会先快照" in confirm_text
                assert "未保存修改" not in confirm_text
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/config/restore" and response.request.method == "POST") as restore_response:
                    page.locator("#confirmYes").click()
                assert restore_response.value.status == 200
                page.wait_for_function("document.querySelector('#saveOutcome') && !document.querySelector('#saveOutcome').hidden && document.querySelector('#saveOutcome').innerText.includes('未启动组件重配')")
                restore_path, restore_body = state["writes"][1]
                assert restore_path == "/api/config/restore"
                assert restore_body == {"name": history_name, "expected_revision": "revision-local-2"}
                assert "revision-local-1" not in page.locator("body").inner_text()
                assert "revision-local-2" not in page.locator("body").inner_text()
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise

@pytest.mark.parametrize("merge_case", ["different-members", "same-member-conflict", "order-conflict"])
def test_two_admin_clients_merge_stable_monitor_rows_without_losing_draft(
    admin_config_server, merge_case: str
):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    config["monitor"].append({"id": "56", "name": "另一成员", "account": "demo_main"})
    if merge_case == "order-conflict":
        config["monitor"].append({"id": "57", "name": "第三成员", "account": "demo_main"})
    state = {"config": copy.deepcopy(config), "revision": "revision-v1", "writes": [], "reads": 0}

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "GET":
            state["reads"] += 1
            payload = _response_for(path, state["config"], state["revision"])
            _fulfill(route, payload)
            return
        if path in {"/api/config", "/api/config/commit"} and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            state["writes"].append((path, copy.deepcopy(body)))
            if body.get("expected_revision") != state["revision"]:
                _fulfill(route, {"ok": False, "error": "revision_conflict", "revision": state["revision"]}, 409)
                return
            config_body = copy.deepcopy(body.get("config", body))
            config_body.pop("expected_revision", None)
            state["config"] = config_body
            state["revision"] = "revision-v" + str(int(state["revision"].split("v")[-1]) + 1)
            _fulfill(route, {
                "ok": True, "saved": True, "revision": state["revision"],
                "reload": {"config": "loaded", "components": "pending"},
                "cred_status": {}, "qq_bot_status": [], "env_status": {},
            })
            return
        _fulfill(route, _response_for(path, state["config"], state["revision"]))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page_a = _new_page(p, admin_config_server, handle, "desktop", "light")
            context_b = browser.new_context(viewport=VIEWPORTS["desktop"], color_scheme="dark", locale="zh-CN")
            context_b.add_init_script("localStorage.setItem('sakamichiTheme', 'dark');")
            page_b = _page_in_context(context_b, admin_config_server, handle, "dark")
            try:
                assert state["reads"] == 2
                if merge_case == "order-conflict":
                    page_a.evaluate("() => { config.monitor=[config.monitor[1],config.monitor[0],config.monitor[2]]; renderAll(); markDirty(); }")
                    page_b.evaluate("() => { config.monitor=[config.monitor[2],config.monitor[1],config.monitor[0]]; renderAll(); markDirty(); }")
                else:
                    index_a = 0
                    index_b = 0 if merge_case == "same-member-conflict" else 1
                    page_a.evaluate("(index) => { config.monitor[index].name='客户端 A 的成员'; renderAll(); markDirty(); }", index_a)
                    page_b.evaluate(
                        "(index) => { config.monitor[index].name='客户端 B 的成员'; renderAll(); markDirty(); }",
                        index_b,
                    )

                page_a.locator("#btnSave").click()
                page_a.locator("#confirmDialog").wait_for(state="visible")
                with page_a.expect_response(lambda response: urlsplit(response.url).path == "/api/config" and response.request.method == "PUT") as first_save_response:
                    page_a.locator("#confirmYes").click()
                assert first_save_response.value.status == 200
                assert state["revision"] == "revision-v2"

                page_b.locator("#btnSave").click()
                page_b.locator("#confirmDialog").wait_for(state="visible")
                page_b.locator("#confirmYes").click()
                page_b.locator("#confirmDialog").wait_for(state="visible")
                page_b.locator("#confirmNo").click()
                assert page_b.locator("#saveOutcome").inner_text().find("页面草稿已保留") >= 0
                assert page_b.locator("#btnSave").is_disabled()
                assert state["reads"] == 2
                assert page_b.locator("#btnReloadFile").inner_text().strip() == "载入最新版并合并草稿"
                page_b.locator("#btnReloadFile").click()
                page_b.wait_for_function("!configRevisionStale && configRevision === 'revision-v2'")
                merged = page_b.evaluate("config.monitor.map((row) => row.name)")
                if merge_case == "order-conflict":
                    assert merged == ["第三成员", "另一成员", "示例成员"]
                    assert "服务器与本页修改了不同设置" in page_b.locator("#saveOutcome").inner_text()
                elif merge_case == "same-member-conflict":
                    assert "客户端 B 的成员" in merged
                    assert "客户端 A 的成员" not in merged
                    assert "同项冲突保留本页值" in page_b.locator("#saveOutcome").inner_text()
                    assert "双方都修改" in page_b.locator("#saveOutcome").inner_text()
                else:
                    assert "客户端 A 的成员" in merged
                    assert "客户端 B 的成员" in merged
                    assert "服务器与本页修改了不同设置" in page_b.locator("#saveOutcome").inner_text()
                assert page_b.locator("#btnSave").is_enabled()

                page_b.locator("#btnSave").click()
                page_b.locator("#confirmDialog").wait_for(state="visible")
                preview = page_b.locator("#confirmBody").inner_text()
                assert "客户端 B 的成员" not in preview
                assert "监控成员" in preview
                with page_b.expect_response(lambda response: urlsplit(response.url).path == "/api/config" and response.request.method == "PUT") as rebased_save_response:
                    page_b.locator("#confirmYes").click()
                assert rebased_save_response.value.status == 200
                assert state["revision"] == "revision-v3"
                assert len(state["writes"]) == 3
                merge_path, merge_body = state["writes"][2]
                assert merge_path == "/api/config"
                assert merge_body["expected_revision"] == "revision-v2"
                assert "secret_updates" not in merge_body
                saved_names = [row["name"] for row in state["config"]["monitor"]]
                if merge_case == "order-conflict":
                    assert saved_names == ["第三成员", "另一成员", "示例成员"]
                elif merge_case == "same-member-conflict":
                    assert "客户端 B 的成员" in saved_names
                    assert "客户端 A 的成员" not in saved_names
                else:
                    assert "客户端 A 的成员" in saved_names
                    assert "客户端 B 的成员" in saved_names
                assert page_b.locator("#btnReloadFile").inner_text().strip() == "↺ 放弃修改并重新载入"
            finally:
                context.close()
                context_b.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise


def test_saved_config_reload_can_be_retried_without_resaving(admin_config_server):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"writes": 0, "reloads": 0, "revision": "revision-v1"}
    reload_id = "e" * 64

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "PUT":
            state["writes"] += 1
            body = json.loads(route.request.post_data or "{}")
            assert body["expected_revision"] == "revision-v1"
            state["revision"] = "revision-v2"
            _fulfill(route, {
                "ok": True, "saved": True, "revision": "revision-v2",
                "reload": {"id": reload_id, "config": "failed", "components": "not_scheduled"},
            })
            return
        if path == "/api/reload" and route.request.method == "POST":
            state["reloads"] += 1
            _fulfill(route, {
                "ok": True, "reloaded": True,
                "reload": {"id": reload_id, "config": "loaded", "components": "pending", "application": {"id": reload_id, "state": "pending", "config": "loaded", "components": {}}},
            })
            return
        if path == "/api/status" and "reload_id=" in route.request.url:
            _fulfill(route, {"ok": True, "config_application": {"id": reload_id, "state": "pending", "config": "loaded", "components": {}}})
            return
        _fulfill(route, _response_for(path, config, state["revision"]))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page = _new_page(p, admin_config_server, handle, "mobile", "dark")
            try:
                page.locator("#napcatApi").fill("http://reload-retry:3000")
                page.locator("#btnSave").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                page.locator("#confirmYes").click()
                page.locator("#saveOutcome").wait_for(state="visible")
                outcome = page.locator("#saveOutcome").inner_text()
                assert "已保存" in outcome and "配置未加载" in outcome and "未启动组件重配" in outcome
                assert page.locator("#btnRetryReload").is_visible()
                assert state["writes"] == 1 and state["reloads"] == 0
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/reload" and response.request.method == "POST") as reload_response:
                    page.locator("#btnRetryReload").click()
                assert reload_response.value.status == 200
                page.wait_for_function("document.querySelector('#saveOutcome')?.innerText.includes('运行组件重配待确认')")
                assert page.locator("#btnRetryReload").is_visible()
                assert page.locator("#btnRetryReload").inner_text().strip() == "↻ 刷新组件状态"
                outcome = page.locator("#saveOutcome").inner_text()
                assert "已保存" in outcome and "配置已加载" in outcome and "运行组件重配待确认" in outcome
                assert state["writes"] == 1 and state["reloads"] == 1
                assert page.locator("#btnRetryReload").is_visible()
                assert page.locator("#napcatApi").input_value() == "http://reload-retry:3000"
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise


def test_bot_secret_conflict_requires_explicit_reentry_before_commit(admin_config_server):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"config": copy.deepcopy(config), "revision": "revision-v1", "writes": [], "secret": None}

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "GET":
            _fulfill(route, _response_for(path, state["config"], state["revision"]))
            return
        if path in {"/api/config", "/api/config/commit"} and route.request.method == "PUT":
            body = json.loads(route.request.post_data or "{}")
            state["writes"].append((path, copy.deepcopy(body)))
            if body.get("expected_revision") != state["revision"]:
                _fulfill(route, {"ok": False, "error": "revision_conflict", "revision": state["revision"]}, 409)
                return
            if path.endswith("/commit"):
                state["secret"] = body.get("secret_updates", {}).get("DEMO_CLIENT_SECRET", state["secret"])
                config_body = copy.deepcopy(body["config"])
            else:
                config_body = copy.deepcopy(body)
                config_body.pop("expected_revision", None)
            state["config"] = config_body
            state["revision"] = "revision-v" + str(int(state["revision"].split("v")[-1]) + 1)
            _fulfill(route, {
                "ok": True, "saved": True, "revision": state["revision"],
                "reload": {"config": "loaded", "components": "pending"},
                "cred_status": {}, "qq_bot_status": [], "env_status": {},
            })
            return
        _fulfill(route, _response_for(path, state["config"], state["revision"]))

    try:
        with playwright.sync_playwright() as p:
            browser_a, context_a, page_a = _new_page(p, admin_config_server, handle, "desktop", "light")
            context_b = browser_a.new_context(viewport=VIEWPORTS["desktop"], color_scheme="dark", locale="zh-CN")
            context_b.add_init_script("localStorage.setItem('sakamichiTheme', 'dark');")
            page_b = _page_in_context(context_b, admin_config_server, handle, "dark")
            try:
                page_a.evaluate("() => { queueBotSecretUpdate('DEMO_CLIENT_SECRET','SECRET_A_NEW_VALUE'); markDirty(); }")
                page_b.locator("#napcatApi").fill("http://client-b-draft:3000")
                page_b.evaluate("() => { queueBotSecretUpdate('DEMO_CLIENT_SECRET','SECRET_B_OLD_DRAFT'); markDirty(); }")

                page_a.locator("#btnSave").click()
                page_a.locator("#confirmDialog").wait_for(state="visible")
                with page_a.expect_response(lambda response: urlsplit(response.url).path == "/api/config/commit" and response.request.method == "PUT") as client_a_response:
                    page_a.locator("#confirmYes").click()
                assert client_a_response.value.status == 200
                assert state["secret"] == "SECRET_A_NEW_VALUE"

                page_b.locator("#btnSave").click()
                page_b.locator("#confirmDialog").wait_for(state="visible")
                page_b.locator("#confirmYes").click()
                page_b.locator("#confirmDialog").wait_for(state="visible")
                page_b.locator("#confirmNo").click()
                assert "旧凭证操作已清除，需重新输入" in page_b.locator("#saveOutcome").inner_text()
                page_b.locator("#btnReloadFile").click()
                page_b.wait_for_function("!configRevisionStale && configRevision === 'revision-v2'")
                assert "凭证旧操作已清除，请在对应窗口重新输入" in page_b.locator("#saveOutcome").inner_text()
                assert page_b.locator("#napcatApi").input_value() == "http://client-b-draft:3000"
                assert page_b.evaluate("Object.keys(pendingBotSecretUpdates).length") == 0
                assert "SECRET_B_OLD_DRAFT" not in page_b.locator("body").inner_text()

                page_b.locator("#btnSave").click()
                page_b.locator("#confirmDialog").wait_for(state="visible")
                preview = page_b.locator("#confirmBody").inner_text()
                assert "凭证修改未随本次保存提交" in preview
                with page_b.expect_response(lambda response: urlsplit(response.url).path == "/api/config" and response.request.method == "PUT") as config_only_response:
                    page_b.locator("#confirmYes").click()
                assert config_only_response.value.status == 200
                assert state["writes"][2][0] == "/api/config"
                assert "secret_updates" not in state["writes"][2][1]
                assert state["secret"] == "SECRET_A_NEW_VALUE"

                page_b.evaluate("() => { queueBotSecretUpdate('DEMO_CLIENT_SECRET','SECRET_B_REENTERED_VALUE'); markDirty(); }")
                page_b.locator("#btnSave").click()
                page_b.locator("#confirmDialog").wait_for(state="visible")
                assert "凭证内容（已隐藏）" in page_b.locator("#confirmBody").inner_text()
                assert "SECRET_B_REENTERED_VALUE" not in page_b.locator("#confirmBody").inner_text()
                with page_b.expect_response(lambda response: urlsplit(response.url).path == "/api/config/commit" and response.request.method == "PUT") as reentered_secret_response:
                    page_b.locator("#confirmYes").click()
                assert reentered_secret_response.value.status == 200
                assert state["writes"][3][0] == "/api/config/commit"
                assert state["writes"][3][1]["expected_revision"] == "revision-v3"
                assert state["writes"][3][1]["secret_updates"]["DEMO_CLIENT_SECRET"] == "SECRET_B_REENTERED_VALUE"
                assert state["secret"] == "SECRET_B_REENTERED_VALUE"
            finally:
                context_a.close()
                context_b.close()
                browser_a.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise



def test_saved_config_shows_per_component_application_report(admin_config_server):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"revision": "revision-v1", "polls": 0}
    reload_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "PUT":
            _fulfill(route, {
                "ok": True, "saved": True, "revision": "revision-v2",
                "reload": {"id": reload_id, "config": "loading", "components": "pending",
                           "application": {"id": reload_id, "state": "pending", "config": "loading", "components": {}}},
            })
            return
        if path == "/api/status" and "reload_id=" in route.request.url:
            state["polls"] += 1
            _fulfill(route, {
                "ok": True,
                "config_application": {
                    "id": reload_id, "state": "partial_failure", "config": "loaded",
                    "components": {
                        "loggers": {"state": "completed"},
                        "accounts": {"state": "failed", "reason_code": "component_reported_failure"},
                        "napcat_inbound_listener": {"state": "not_required", "reason_code": "listener_disabled"},
                    },
                },
            })
            return
        _fulfill(route, _response_for(path, config, state["revision"]))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page = _new_page(p, admin_config_server, handle, "desktop", "light")
            try:
                page.locator("#napcatApi").fill("http://component-report:3000")
                page.locator("#btnSave").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/status" and "reload_id=" in response.url) as status_response:
                    page.locator("#confirmYes").click()
                assert status_response.value.status == 200
                page.wait_for_function("document.querySelector('#configApplicationDetail')?.children.length > 0")
                page.locator("#configSaveDisclosure summary").click()
                page.wait_for_function("document.querySelector('#configApplicationDetail')?.innerText.includes('部分失败')")
                outcome = page.locator("#saveOutcome").inner_text()
                assert "已保存" in outcome and "配置已加载" in outcome
                assert "日志" in outcome and "已完成重配" in outcome
                assert "账号凭证" in outcome and "重配失败" in outcome
                assert "组件报告更新失败" in outcome
                assert "OneBot 入站监听" in outcome and "无需重配" in outcome
                assert reload_id not in outcome
                assert state["polls"] == 1
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise


def test_config_application_timeout_stops_polling_and_allows_manual_refresh(admin_config_server):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"revision": "revision-v1", "polls": 0, "reloads": 0}
    reload_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "PUT":
            _fulfill(route, {
                "ok": True, "saved": True, "revision": "revision-v2",
                "reload": {"id": reload_id, "config": "loaded", "components": "pending"},
            })
            return
        if path == "/api/status" and "reload_id=" in route.request.url:
            state["polls"] += 1
            app_state = "pending" if state["polls"] == 1 else "completed"
            component_state = "pending" if state["polls"] == 1 else "completed"
            _fulfill(route, {
                "ok": True,
                "config_application": {"id": reload_id, "state": app_state,
                                       "components": {"telegram_bots": {"state": component_state}}},
            })
            return
        if path == "/api/reload" and route.request.method == "POST":
            state["reloads"] += 1
            _fulfill(route, {"ok": True, "reload": {"config": "loaded", "components": "pending"}})
            return
        _fulfill(route, _response_for(path, config, state["revision"]))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page = _new_page(p, admin_config_server, handle, "mobile", "dark")
            try:
                page.locator("#napcatApi").fill("http://application-timeout:3000")
                page.locator("#btnSave").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/status" and "reload_id=" in response.url):
                    page.locator("#confirmYes").click()
                assert page.evaluate("configApplicationPollTimer !== null")
                page.evaluate("configApplicationPollDeadline = Date.now() - 1")
                page.wait_for_function("configApplicationPollTimer === null")
                page.locator("#configSaveDisclosure summary").click()
                assert "尚未确认" in page.locator("#configApplicationDetail").inner_text()
                assert page.evaluate("configApplicationPollTimer === null")
                button = page.locator("#btnRetryReload")
                assert button.is_visible() and button.inner_text().strip() == "↻ 刷新组件状态"
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/status" and "reload_id=" in response.url) as refresh_response:
                    button.click()
                assert refresh_response.value.status == 200
                page.wait_for_function("document.querySelector('#configApplicationDetail')?.innerText.includes('已完成重配')")
                assert "已保存" in page.locator("#saveOutcome").inner_text()
                assert "配置已加载" in page.locator("#saveOutcome").inner_text()
                assert "已完成重配" in page.locator("#configApplicationDetail").inner_text()
                page.wait_for_function("document.querySelector('#btnRetryReload')?.hidden === true")
                assert state["polls"] == 2 and state["reloads"] == 0
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise


def test_config_application_pagehide_clears_poll_timer(admin_config_server):
    playwright = pytest.importorskip("playwright.sync_api")
    config = _fixture_config()
    state = {"revision": "revision-v1"}
    reload_id = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/api/config" and route.request.method == "PUT":
            _fulfill(route, {
                "ok": True, "saved": True, "revision": "revision-v2",
                "reload": {"id": reload_id, "config": "loaded", "components": "pending"},
            })
            return
        if path == "/api/status" and "reload_id=" in route.request.url:
            _fulfill(route, {"ok": True, "config_application": {
                "id": reload_id, "state": "pending",
                "components": {"telegram_bots": {"state": "pending"}},
            }})
            return
        _fulfill(route, _response_for(path, config, state["revision"]))

    try:
        with playwright.sync_playwright() as p:
            browser, context, page = _new_page(p, admin_config_server, handle, "mobile", "light")
            try:
                page.locator("#napcatApi").fill("http://application-pagehide:3000")
                page.locator("#btnSave").click()
                page.locator("#confirmDialog").wait_for(state="visible")
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/status" and "reload_id=" in response.url):
                    page.locator("#confirmYes").click()
                assert page.evaluate("configApplicationPollTimer !== null")
                page.locator('.header-nav .nav-tab[data-tab="social"], nav.tabs button[data-tab="social"]').first.click()
                assert page.evaluate("configApplicationPollTimer !== null")
                assert page.locator("#saveOutcome").is_visible()
                page.evaluate("window.dispatchEvent(new Event('pagehide'))")
                assert page.evaluate("configApplicationPollTimer === null")
                page.locator("#configSaveDisclosure summary").click()
                assert "待确认" in page.locator("#configApplicationDetail").inner_text()
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        if exc.__class__.__name__ in {"Error", "PlaywrightError"} and "executable" in str(exc).lower():
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise
