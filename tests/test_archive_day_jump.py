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


def test_message_upload_badge_shows_facts_only_for_media(archive_static_server):
    """发布时间秒数不能给纯文本贴定时标签；媒体只展示上传时间和间隔。"""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "**/api/**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"ok":true,"members":[],"groups":[],"months":[],"days":{}}',
            ),
        )
        page.goto(archive_static_server + "/archive.html", wait_until="domcontentloaded")
        badges = page.evaluate("""() => {
            const publishedAt = "2026-10-01T13:11:37Z"; // 22:11:37 JST，旧规则命中“疑似定时”
            const make = (id, type, mediaUrl, uploadAt) => {
                const host = document.createElement("div");
                renderBubble({
                    id, type, group: "hinata", text: "消息正文", translation: "",
                    year: 2026, month: 10,
                    published_at: publishedAt, upload_at: uploadAt, media_url: mediaUrl,
                }, host);
                const badge = host.querySelector(".upload-badge");
                return badge ? {text: badge.textContent, title: badge.title, className: badge.className} : null;
            };
            return {
                text: make(1, "text", null, null),
                image: make(2, "picture", "/image.jpg", "2026-10-01T13:10:00Z"),
                oldImage: make(3, "picture", "/old.jpg", "2026-09-30T13:10:00Z"),
            };
        }""")
        assert badges["text"] is None
        for name in ("image", "oldImage"):
            badge = badges[name]
            assert "真实上传" in badge["text"]
            assert "媒体上传时间" in badge["title"]
            assert "消息发布时间" in badge["title"]
            assert "定时" not in badge["text"] + badge["title"]
            assert "审核" not in badge["text"] + badge["title"]
            assert badge["className"] == "upload-badge"

        for theme in ("light", "dark"):
            page.evaluate("(value) => document.documentElement.setAttribute('data-theme', value)", theme)
            for width in (390, 1280):
                page.set_viewport_size({"width": width, "height": 800})
                metrics = page.evaluate("""() => {
                    const host = document.createElement("div");
                    host.style.width = Math.min(window.innerWidth - 32, 900) + "px";
                    document.body.appendChild(host);
                    renderBubble({
                        id: 4, type: "picture", group: "hinata", text: "", translation: "",
                        year: 2026, month: 10, published_at: "2026-10-01T13:11:37Z",
                        upload_at: "2026-10-01T13:10:00Z", media_url: "/image.jpg",
                    }, host);
                    const badge = host.querySelector(".upload-badge");
                    const bounds = badge.getBoundingClientRect();
                    const display = getComputedStyle(badge).display;
                    host.remove();
                    return {display, width: bounds.width, left: bounds.left,
                        right: bounds.right, viewport: window.innerWidth};
                }""")
                assert metrics["display"] in {"flex", "inline-flex"}
                assert metrics["width"] > 0
                assert metrics["left"] >= 0
                assert metrics["right"] <= metrics["viewport"], (
                    f"upload badge clipped in {theme} theme at {width}px: {metrics}"
                )
        browser.close()


def test_lightbox_has_no_download_action_and_keeps_source_action_responsive(archive_static_server):
    """桌面/手机与浅/深主题下，灯箱保留来源跳转和关闭，不再提供下载按钮。"""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route(
            "**/api/**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"ok":true,"members":[],"groups":[],"months":[],"days":{}}',
            ),
        )
        page.goto(archive_static_server + "/archive.html", wait_until="domcontentloaded")
        page.wait_for_function("typeof openLightbox === 'function'")
        page.evaluate("""() => {
            const pixel = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=";
            images = [{
                url: pixel, thumbUrl: pixel, source: "message", messageId: "42",
                memberDir: "sample-member", year: 2026, month: 9, caption: "测试图片",
            }];
            lbContext = "gallery";
            openLightbox(0);
        }""")

        assert page.locator("#lbDownloadBtn").count() == 0
        assert page.locator("#lightbox").get_attribute("aria-hidden") == "false"
        assert page.locator("#lbSourceBtn").inner_text() == "💬 前往对应消息"
        assert "msg_id=42" in page.locator("#lbSourceBtn").get_attribute("href")
        assert page.locator("#lbClose").is_visible()

        for theme in ("light", "dark"):
            page.evaluate("(value) => document.documentElement.setAttribute('data-theme', value)", theme)
            for width in (390, 1280):
                page.set_viewport_size({"width": width, "height": 844})
                actions = page.evaluate("""() => {
                    const bounds = document.querySelector("#lbActions").getBoundingClientRect();
                    return {left: bounds.left, right: bounds.right, viewport: window.innerWidth};
                }""")
                assert actions["left"] >= 0, f"lightbox controls clipped in {theme} at {width}px: {actions}"
                assert actions["right"] <= actions["viewport"], (
                    f"lightbox controls overflow in {theme} at {width}px: {actions}"
                )
        browser.close()


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


def test_unfavorite_in_filtered_timeline_preserves_sparse_pages_and_reading_anchor(archive_static_server):
    """取消收藏后只同步已加载页，保持锚点并让下一次翻页从原游标继续。"""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width, theme in ((1280, "light"), (390, "dark"), (1280, "dark"), (390, "light")):
            state = {"removed": False, "message_requests": []}
            all_ids = [f"m{index:03d}" for index in range(1, 51)]

            def handle_api(route):
                request = route.request
                parsed = urlsplit(request.url)
                query = parse_qs(parsed.query)
                path = parsed.path
                if path == "/api/archive/messages":
                    requested_page = int(query.get("page", ["1"])[0])
                    state["message_requests"].append(requested_page)
                    ids = [message_id for message_id in all_ids if not (state["removed"] and message_id == "m003")]
                    start = (requested_page - 1) * 10
                    page_ids = ids[start : start + 10]
                    messages = []
                    for message_id in page_ids:
                        message = _message(message_id, "2026-09-20")
                        message["text"] = ("测试消息 " + message_id + "\n长文本用于验证收藏同步不会移动阅读位置。\n") * 2
                        message["is_favorite"] = message_id != "m003" or not state["removed"]
                        messages.append(message)
                    payload = {
                        "ok": True,
                        "member": "示例成员",
                        "group": "nogizaka",
                        "year": 2026,
                        "month": 9,
                        "total": len(ids),
                        "page": requested_page,
                        "total_pages": max(1, (len(ids) + 9) // 10),
                        "order": "desc",
                        "messages": messages,
                    }
                elif path == "/api/archive/message/interaction":
                    body = json.loads(request.post_data or "{}")
                    if body.get("message_id") == "m003" and body.get("action") == "favorite" and body.get("value") is False:
                        state["removed"] = True
                    payload = {"ok": True, "is_favorite": False}
                elif path == "/api/auth/me":
                    payload = {"ok": True, "auth_enabled": False}
                elif path == "/api/archive/members":
                    payload = {
                        "ok": True,
                        "members": [{
                            "name": "示例成员",
                            "display": "示例成员",
                            "group": "nogizaka",
                            "total": 50,
                            "stats": {"total": 50, "months": 1},
                        }],
                        "monitor_members": [],
                    }
                elif path == "/api/archive/months":
                    payload = {"ok": True, "months": [{"year": 2026, "month": 9, "count": 50}]}
                elif path == "/api/archive/calendar":
                    payload = {"ok": True, "days": {"2026-09-20": 50}}
                elif path == "/api/archive/blog_groups":
                    payload = {"ok": True, "groups": []}
                else:
                    payload = {"ok": True, "members": [], "groups": [], "months": [], "days": {}}
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

            context = browser.new_context(
                viewport={"width": width, "height": 760},
                service_workers="block",
            )
            context.route("**/api/**", handle_api)
            page = context.new_page()
            url = archive_static_server + "/archive.html#member=" + quote("示例成员") + "&y=2026&m=9&fav=1"
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_function("loadedMessagePages.has(1) && document.querySelector('[data-msg-id=\\\"m001\\\"]')")
            page.evaluate("(value) => document.documentElement.setAttribute('data-theme', value)", theme)

            page.evaluate("""async () => {
                page = 2;
                await loadPage();
                page = 4;
                await loadPage();
            }""")
            assert page.evaluate("Array.from(loadedMessagePages).sort((a, b) => a - b)") == [1, 2, 4]
            assert page.evaluate("page") == 4
            assert page.evaluate("Array.from(document.querySelectorAll('#timeline > .bubble')).some((bubble) => bubble.dataset.msgId === 'm003')")

            before = page.evaluate("""() => {
                const target = document.querySelector('[data-msg-id="m035"]');
                target.scrollIntoView({block: "start", behavior: "instant"});
                const anchor = Array.from(document.querySelectorAll('#timeline > .bubble[data-msg-id]'))
                    .find((bubble) => {
                        const rect = bubble.getBoundingClientRect();
                        return rect.bottom > 0 && rect.top < window.innerHeight;
                    });
                const rect = anchor.getBoundingClientRect();
                return {id: anchor.dataset.msgId, top: rect.top, scrollY: window.scrollY};
            }""")
            page.evaluate("""() => {
                const apply = window.applyTimelineAnchor;
                window.__favoriteAnchorTrace = [];
                window.applyTimelineAnchor = (anchor, version) => {
                    const bubble = anchor && document.querySelector('[data-msg-id="' + anchor.id + '"]');
                    const beforeTop = bubble ? bubble.getBoundingClientRect().top : null;
                    const beforeY = window.scrollY;
                    apply(anchor, version);
                    const restored = anchor && document.querySelector('[data-msg-id="' + anchor.id + '"]');
                    window.__favoriteAnchorTrace.push({
                        id: anchor && anchor.id, wantedTop: anchor && anchor.top,
                        beforeY, beforeTop, afterY: window.scrollY,
                        afterTop: restored ? restored.getBoundingClientRect().top : null,
                    });
                };
            }""")
            state["message_requests"].clear()
            removed_immediately = page.evaluate("""() => {
                const target = document.querySelector('[data-msg-id="m003"]');
                target.querySelector('.fav-btn').click();
                return !document.querySelector('[data-msg-id="m003"]');
            }""")
            assert removed_immediately, f"favorite result remained visible immediately at {width}px/{theme}"
            page.wait_for_function("favoriteTimelineSyncState === 'idle' && !document.querySelector('[data-msg-id=\\\"m003\\\"]')")
            assert sorted(state["message_requests"]) == [1, 2, 4]
            assert page.evaluate("page") == 4
            assert page.evaluate("Array.from(loadedMessagePages).sort((a, b) => a - b)") == [1, 2, 4]
            after = page.evaluate("""async (anchorId) => {
                await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                const anchor = document.querySelector('[data-msg-id="' + anchorId + '"]');
                return {
                    top: anchor ? anchor.getBoundingClientRect().top : null,
                    scrollY: window.scrollY,
                    trace: window.__favoriteAnchorTrace,
                };
            }""", before["id"])
            assert after["top"] is not None, f"reading anchor {before['id']} disappeared at {width}px/{theme}"
            assert abs(after["top"] - before["top"]) <= 2, (
                f"reading anchor moved at {width}px/{theme}: {before} -> {after}"
            )
            assert after["trace"], f"anchor correction did not run at {width}px/{theme}"
            restored = after["trace"][-1]
            assert restored["id"] == before["id"]
            assert abs(restored["afterTop"] - restored["wantedTop"]) <= 2, (
                f"internal anchor correction drifted at {width}px/{theme}: {restored}"
            )

            visible_ids = page.locator("#timeline > .bubble").evaluate_all("nodes => nodes.map((node) => node.dataset.msgId)")
            assert len(visible_ids) == len(set(visible_ids)), f"duplicate messages after sync at {width}px/{theme}"
            assert page.locator("#loadMore").is_visible()
            page.locator("#loadMore").click()
            page.wait_for_function("page === 5 && loadedMessagePages.has(5)")
            assert state["message_requests"][-1] == 5
            visible_ids = page.locator("#timeline > .bubble").evaluate_all("nodes => nodes.map((node) => node.dataset.msgId)")
            assert len(visible_ids) == len(set(visible_ids)), f"duplicate messages after page 5 at {width}px/{theme}"
            expected_ids = [f"m{index:03d}" for index in range(1, 22) if index != 3]
            expected_ids.extend(f"m{index:03d}" for index in range(32, 51))
            assert visible_ids == expected_ids, f"loaded page content shifted at {width}px/{theme}"
            assert "m003" not in visible_ids
            context.close()

        browser.close()
