"""归档日历日期跳转的真实浏览器请求回归。"""

from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = ROOT / "src" / "webui_static"


class _ArchiveStaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        relative = urlsplit(path).path
        if relative == "/":
            relative = "/archive.html"
        elif relative.startswith("/static/"):
            relative = relative[len("/static") :]
        return str(STATIC_ROOT / unquote(relative.lstrip("/")))

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture(scope="module")
def archive_static_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveStaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def _message(message_id: str, date: str) -> dict:
    return {
        "id": message_id,
        "type": "text",
        "text": "测试消息 " + message_id,
        "translation": "测试翻译",
        "published_at": date + "T12:00:00Z",
        "upload_at": "",
        "group": "nogizaka",
        "media_url": None,
        "tags": "",
        "custom_tags": "",
        "is_favorite": False,
        "is_liked": False,
    }


def test_message_day_jump_avoids_reloading_loaded_days_and_stops_at_target_page(archive_static_server):
    playwright = pytest.importorskip("playwright.sync_api")
    requests: list[dict[str, int]] = []

    def handle_api(route):
        request = route.request
        parsed = urlsplit(request.url)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path == "/api/archive/messages":
            requested_page = int(query.get("page", ["1"])[0])
            requests.append({"page": requested_page})
            messages = (
                [_message("first", "2026-09-20")]
                if requested_page == 1
                else [_message("second", "2026-09-19")]
            )
            payload = {
                "ok": True,
                "member": "示例成员",
                "group": "nogizaka",
                "year": 2026,
                "month": 9,
                "total": 2,
                "page": requested_page,
                "total_pages": 2,
                "order": "desc",
                "messages": messages,
            }
        elif path == "/api/auth/me":
            payload = {"ok": False}
        elif path == "/api/archive/members":
            payload = {
                "ok": True,
                "members": [
                    {
                        "name": "示例成员",
                        "display": "示例成员",
                        "group": "nogizaka",
                        "total": 2,
                        "stats": {"total": 2, "months": 1},
                    }
                ],
                "monitor_members": [],
            }
        elif path == "/api/archive/months":
            payload = {"ok": True, "months": [{"year": 2026, "month": 9, "count": 2}]}
        elif path == "/api/archive/calendar":
            payload = {"ok": True, "days": {"2026-09-19": 1, "2026-09-20": 1}}
        elif path == "/api/archive/blog_groups":
            payload = {"ok": True, "groups": []}
        else:
            payload = {"ok": True, "members": [], "groups": [], "months": [], "days": {}}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.route("**/api/**", handle_api)
        url = archive_static_server + "/archive.html#member=" + quote("示例成员") + "&y=2026&m=9"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector('.day-sep[data-date="2026-09-20"]', timeout=10000)
        assert [item["page"] for item in requests] == [1]

        requests.clear()
        page.evaluate("jumpToDay('2026-09-20')")
        page.wait_for_timeout(500)
        assert requests == []

        page.evaluate("jumpToDay('2026-09-19')")
        page.wait_for_selector('.day-sep[data-date="2026-09-19"]', timeout=10000)
        assert [item["page"] for item in requests] == [2]

        browser.close()


def test_message_day_jump_batches_missing_pages_using_calendar_index(archive_static_server):
    """远日期跳转应按日历索引并行补齐缺页，而不是逐页串行等待。"""
    playwright = pytest.importorskip("playwright.sync_api")
    requests: list[int] = []
    dates = [f"2026-09-{day:02d}" for day in range(20, 15, -1)]

    def handle_api(route):
        request = route.request
        parsed = urlsplit(request.url)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path == "/api/archive/messages":
            requested_page = int(query.get("page", ["1"])[0])
            requests.append(requested_page)
            date = dates[requested_page - 1]
            payload = {
                "ok": True,
                "member": "示例成员",
                "group": "nogizaka",
                "year": 2026,
                "month": 9,
                "total": 5,
                "page": requested_page,
                "total_pages": 5,
                "order": "desc",
                "messages": [_message(f"page-{requested_page}", date)],
            }
        elif path == "/api/auth/me":
            payload = {"ok": False}
        elif path == "/api/archive/members":
            payload = {
                "ok": True,
                "members": [{
                    "name": "示例成员",
                    "display": "示例成员",
                    "group": "nogizaka",
                    "total": 5,
                    "stats": {"total": 5, "months": 1},
                }],
                "monitor_members": [],
            }
        elif path == "/api/archive/months":
            payload = {"ok": True, "months": [{"year": 2026, "month": 9, "count": 5}]}
        elif path == "/api/archive/calendar":
            payload = {"ok": True, "days": {date: 1 for date in dates}}
        elif path == "/api/archive/blog_groups":
            payload = {"ok": True, "groups": []}
        else:
            payload = {"ok": True, "members": [], "groups": [], "months": [], "days": {}}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.route("**/api/**", handle_api)
        url = archive_static_server + "/archive.html#member=" + quote("示例成员") + "&y=2026&m=9"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector('.day-sep[data-date="2026-09-20"]', timeout=10000)

        requests.clear()
        elapsed_ms = page.evaluate("""async () => {
            const started = performance.now();
            await jumpToDay('2026-09-16');
            return performance.now() - started;
        }""")
        page.wait_for_selector('.day-sep[data-date="2026-09-16"]', timeout=10000)
        assert sorted(requests) == [2, 3, 4, 5]
        assert elapsed_ms < 1000
        assert page.locator('.day-sep[data-date="2026-09-16"]').count() == 1
        browser.close()
