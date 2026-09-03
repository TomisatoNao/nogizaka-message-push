"""Regression tests for the WebUI social one-off tools."""

from __future__ import annotations

from io import BytesIO
import json
import urllib.request

from src.social.instagram_embed import _extract_page_data
from src.social.models import MediaItem, Post
from src.webui_modules import social_tool_handlers


class _Locator:
    def __init__(self, values=None, attrs=None):
        self.values = values or []
        self.attrs = attrs or {}

    def evaluate_all(self, _script):
        return self.values

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.attrs else len(self.values)

    def get_attribute(self, name):
        return self.attrs.get(name, "")


class _CarouselPage:
    def __init__(self, script: str):
        self._locators = {
            "script": _Locator([script]),
            "img": _Locator([]),
            "video": _Locator([]),
            "a": _Locator([]),
            'meta[property="og:image"]': _Locator([], {"content": ""}),
            'meta[name="twitter:image"]': _Locator([], {"content": ""}),
            'meta[property="og:description"]': _Locator([], {"content": ""}),
            'meta[name="description"]': _Locator([], {"content": ""}),
            'meta[property="article:published_time"]': _Locator([], {"content": ""}),
        }

    def locator(self, selector):
        return self._locators[selector]


class _ResponseHandler:
    def __init__(self):
        self.headers = {}
        self.wfile = BytesIO()
        self.sent = None

    def send_response(self, code):
        self.sent = code

    def send_header(self, _key, _value):
        pass

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def test_instagram_carousel_structured_payload_deduplicates_seven_fields_to_six():
    urls = [f"https://scontent.cdninstagram.com/photo-{idx}.jpg" for idx in range(6)]
    # The final field is the same first photo with a different signed query,
    # mirroring the root ``display_url`` vs sidecar child rendition.
    duplicate_variant = urls[0] + "?stp=alternate"
    escaped_urls = [url.replace("/", "\\/") for url in [*urls, duplicate_variant]]
    fields = ",".join(
        f'{{"node":{{"display_url":"{url}"}}}}'
        for url in escaped_urls
    )
    script = (
        r'{"GraphSidecar":true,"shortcode":"Dcz0fgID5h_",'
        r'"edge_sidecar_to_children":{"edges":[' + fields + "]}}"
    )
    # The fixture above intentionally models the escaped JSON found in an
    # inert script tag, including the root/first-slide duplicate.
    media, *_ = _extract_page_data(
        _CarouselPage(script), max_media=20, shortcode="Dcz0fgID5h_"
    )

    assert len(media) == 6
    assert [item.url for item in media] == urls
    assert all(item.type == "image" for item in media)


def test_parse_post_handler_returns_structured_result(monkeypatch):
    post = Post(
        platform="instagram",
        post_id="Dcz0fgID5h_",
        author="Public User",
        text="caption",
        media=[MediaItem(type="image", url="https://cdn.example/photo.jpg")],
    )

    class _Parser:
        def __init__(self, _config):
            pass

        def parse(self, _url):
            return post

    monkeypatch.setattr("src.social.single_fetcher.SocialUrlParser", _Parser)
    handler = _ResponseHandler()

    assert social_tool_handlers.handle_parse_post(
        handler,
        {"url": "https://www.instagram.com/p/Dcz0fgID5h_/", "translate": False},
        load_raw_config=lambda: {},
    )
    payload = _payload(handler)
    assert handler.sent == 200
    assert payload["ok"] and payload["media_count"] == 1
    assert payload["extra"]["url"].endswith("Dcz0fgID5h_/")
    assert "request_id" in payload and len(payload["request_id"]) == 12


def test_parse_post_handler_rejects_unknown_domain_without_calling_parser(monkeypatch):
    called = []

    class _Parser:
        def __init__(self, _config):
            called.append(True)

    monkeypatch.setattr("src.social.single_fetcher.SocialUrlParser", _Parser)
    handler = _ResponseHandler()

    assert not social_tool_handlers.handle_parse_post(
        handler,
        {"url": "https://evil.example/p/abc"},
        load_raw_config=lambda: {},
    )
    payload = _payload(handler)
    assert handler.sent == 400
    assert payload["error_code"] == "invalid_request"
    assert not called


def test_manual_push_handler_reports_delivery_summary(monkeypatch):
    def fake_push(url, config, target_channels, translate, archive):
        assert url.startswith("https://www.instagram.com/p/")
        assert target_channels == ["official:bot1:private"]
        assert translate is False and archive is True
        return {
            "ok": True,
            "platform": "instagram",
            "media_count": 6,
            "media": [],
            "delivery": {
                "outcome": "success",
                "matched_routes": 1,
                "attempted_routes": 1,
                "success_routes": 1,
                "failed_routes": 0,
                "skipped_routes": 0,
                "errors": [],
            },
        }

    monkeypatch.setattr("src.social.single_fetcher.manual_push_social_url", fake_push)
    handler = _ResponseHandler()

    assert social_tool_handlers.handle_manual_push(
        handler,
        {
            "url": "https://www.instagram.com/p/Dcz0fgID5h_/",
            "channels": ["official:bot1:private"],
            "translate": False,
            "archive": True,
        },
        load_raw_config=lambda: {},
    )
    payload = _payload(handler)
    assert handler.sent == 200
    assert payload["media_count"] == 6 and payload["delivery"]["success_routes"] == 1


def test_webui_dispatches_social_tool_routes_instead_of_404(monkeypatch):
    import config.config as cfg
    from src import webui

    monkeypatch.setattr(cfg, "AUTH_ENABLED", False)
    monkeypatch.delenv("WEB_ADMIN_TOKEN", raising=False)
    calls = []

    def fake_parse(handler, body, **_kwargs):
        calls.append(("parse", body))
        handler._send_json({"ok": True, "route": "parse"})
        return True

    def fake_push(handler, body, **_kwargs):
        calls.append(("push", body))
        handler._send_json({"ok": True, "route": "push"})
        return True

    monkeypatch.setattr(webui, "_social_tool_handle_parse_post", fake_parse)
    monkeypatch.setattr(webui, "_social_tool_handle_manual_push", fake_push)
    server = webui.start_webui(host="127.0.0.1", port=0)
    assert server is not None
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, body):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    try:
        assert post("/api/social/parse_post", {"url": "https://www.instagram.com/p/demo/"}) == (
            200, {"ok": True, "route": "parse"}
        )
        assert post("/api/social/manual_push", {"url": "https://www.instagram.com/p/demo/"}) == (
            200, {"ok": True, "route": "push"}
        )
    finally:
        server.shutdown()
        server.server_close()

    assert [kind for kind, _body in calls] == ["parse", "push"]
