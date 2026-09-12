import json
import re
from types import SimpleNamespace

import pytest

import config.config as cfg
from src.platforms import napcat
from src.social.adapters import NapCatAdapter
from src.social.contracts import DeliveryTarget
from src.social.formatter import build_post_message
from src.social.models import MediaItem, Post
from src.webui_modules import social_media_service as media_service


def test_napcat_media_uses_signed_http_url_for_remote_host(tmp_path, monkeypatch):
    root = tmp_path / "social_media"
    media = root / "instagram" / "demo" / "post" / "photo.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"jpeg")
    monkeypatch.setattr(media_service, "_root", lambda: root.resolve())
    monkeypatch.setattr(media_service.cfg, "NAPCAT_MEDIA_BASE_URL", "http://192.168.22.13:46046", raising=False)
    monkeypatch.setattr(media_service.cfg, "NAPCAT_MEDIA_SIGNING_SECRET", "test-secret", raising=False)

    item = NapCatAdapter._chain_item(MediaItem(type="image", url="https://example/photo.jpg", local_path=str(media)))
    assert item and item["type"] == "image"
    assert item["data"]["file"].startswith("http://192.168.22.13:46046/api/social/media/")
    assert "expires=" in item["data"]["file"] and "sig=" in item["data"]["file"]
    assert "file:///" not in item["data"]["file"]


def test_signed_media_rejects_invalid_signature(monkeypatch):
    handler = SimpleNamespace(path="/api/social/media/instagram/demo/photo.jpg?expires=9999999999&sig=bad")
    responses = []
    handler.send_error = lambda code, message: responses.append((code, message))
    assert media_service.serve_signed_social_media(handler, "/api/social/media/instagram/demo/photo.jpg")
    assert responses and responses[0][0] == 403


class _Response:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {"status": "ok", "retcode": 0}


class _Client:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


@pytest.mark.asyncio
async def test_napcat_multi_media_uses_group_forward_endpoint(tmp_path, monkeypatch):
    root = tmp_path / "social_media"
    paths = []
    for name in ("one.jpg", "two.jpg"):
        path = root / "instagram" / "demo" / "post" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        paths.append(path)
    monkeypatch.setattr(media_service, "_root", lambda: root.resolve())
    monkeypatch.setattr(
        media_service.cfg,
        "NAPCAT_MEDIA_BASE_URL",
        "http://127.0.0.1:46046",
        raising=False,
    )
    monkeypatch.setattr(
        media_service.cfg,
        "NAPCAT_MEDIA_SIGNING_SECRET",
        "test-secret",
        raising=False,
    )
    monkeypatch.setattr(cfg, "QQ_BOT_API", "http://napcat:36036/send_group_msg", raising=False)
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0, raising=False)

    client = _Client()
    napcat.initialize(client)
    target = DeliveryTarget("napcat", "123456", "groups").bind_runtime(
        {"group_id": 123456, "self_id": "2272248496"}
    )
    media = [
        MediaItem(type="image", url="https://example/one.jpg", local_path=str(paths[0])),
        MediaItem(type="image", url="https://example/two.jpg", local_path=str(paths[1])),
    ]

    post = Post(
        platform="instagram",
        post_id="demo",
        author="@demo_account",
        text="caption",
        media=media,
        timestamp="2026-09-12 23:54:43 JST",
        extra={"url": "https://www.instagram.com/p/demo/"},
    )
    full_text = build_post_message(post)

    assert await NapCatAdapter().send_post(target, full_text, media) is True
    assert len(client.calls) == 1

    url, kwargs = client.calls[0]
    assert url.endswith("/send_group_forward_msg")
    payload = json.loads(kwargs["content"])
    assert payload["group_id"] == 123456
    assert len(payload["messages"]) == 3
    assert all(node["type"] == "node" for node in payload["messages"])
    assert payload["messages"][0]["data"]["user_id"] == 2272248496
    first_content = payload["messages"][0]["data"]["content"]
    assert first_content == [{"type": "text", "data": {"text": full_text}}]
    assert "2026-09-12 23:54:43 JST" in first_content[0]["data"]["text"]
    assert not {"image", "video", "record"} & {
        item["type"] for item in first_content
    }
    assert payload["messages"][1]["data"]["content"][0]["type"] == "image"
    assert payload["messages"][2]["data"]["content"][0]["type"] == "image"
    assert all(
        len(node["data"]["content"]) == 1
        for node in payload["messages"]
    )
    assert all(
        node["data"]["user_id"] == 2272248496
        for node in payload["messages"]
    )
    assert all(
        int(node["data"].get("time", 0)) > 0
        for node in payload["messages"]
    )
    assert not any(call_url.endswith("/send_group_msg") for call_url, _ in client.calls)


def test_napcat_seven_media_builds_body_plus_seven_media_nodes():
    target = DeliveryTarget("napcat", "123456", "groups").bind_runtime(
        {"group_id": 123456, "self_id": "2272248496"}
    )
    media_items = [
        {"type": "image", "data": {"file": f"https://example/{index}.jpg"}}
        for index in range(7)
    ]

    nodes = NapCatAdapter._forward_nodes(target, "正文", media_items)

    assert nodes is not None
    assert len(nodes) == 8
    assert nodes[0]["data"]["content"] == [
        {"type": "text", "data": {"text": "正文"}}
    ]
    assert all(int(node["data"].get("time", 0)) > 0 for node in nodes)
    assert all(
        len(node["data"]["content"]) == 1
        and node["data"]["content"][0]["type"] == "image"
        for node in nodes[1:]
    )


def test_build_post_message_fallback_timestamp_when_empty():
    post = Post(
        platform="instagram",
        post_id="demo_no_time",
        author="@demo_account",
        text="caption without time",
        media=[],
        timestamp="",
        extra={"url": "https://www.instagram.com/p/demo_no_time/"},
    )
    msg = build_post_message(post)
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} JST", msg)
    assert "📷 Instagram · @demo_account · " in msg


@pytest.mark.asyncio
async def test_napcat_multi_media_without_identity_falls_back_to_normal_message(
    tmp_path, monkeypatch
):
    first = tmp_path / "one.jpg"
    second = tmp_path / "two.jpg"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    monkeypatch.setattr(cfg, "QQ_BOT_API", "http://napcat:36036/send_group_msg", raising=False)
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0, raising=False)
    monkeypatch.delenv("NAPCAT_FORWARD_USER_ID", raising=False)
    monkeypatch.setattr(cfg, "NAPCAT_FORWARD_USER_ID", "", raising=False)

    client = _Client()
    napcat.initialize(client)
    target = DeliveryTarget("napcat", "123456", "groups").bind_runtime(
        {"group_id": 123456}
    )
    media = [
        MediaItem(type="image", url="", local_path=str(first)),
        MediaItem(type="image", url="", local_path=str(second)),
    ]

    assert await NapCatAdapter().send_post(target, "caption", media) is True
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url.endswith("/send_group_msg")
    payload = json.loads(kwargs["content"])
    assert len(payload["message"]) == 3


@pytest.mark.asyncio
async def test_napcat_single_media_keeps_normal_message_endpoint(tmp_path, monkeypatch):
    path = tmp_path / "one.jpg"
    path.write_bytes(b"one")
    monkeypatch.setattr(cfg, "QQ_BOT_API", "http://napcat:36036/send_group_msg", raising=False)
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0, raising=False)
    client = _Client()
    napcat.initialize(client)
    target = DeliveryTarget("napcat", "123456", "groups").bind_runtime(
        {"group_id": 123456, "self_id": "2272248496"}
    )

    assert await NapCatAdapter().send_post(
        target,
        "caption",
        [MediaItem(type="image", url="", local_path=str(path))],
    ) is True
    assert len(client.calls) == 1
    assert client.calls[0][0].endswith("/send_group_msg")
