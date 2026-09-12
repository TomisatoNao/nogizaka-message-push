import json
from types import SimpleNamespace

import pytest

import config.config as cfg
from src.platforms import napcat
from src.social.adapters import NapCatAdapter
from src.social.contracts import DeliveryTarget
from src.social.models import MediaItem
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

    assert await NapCatAdapter().send_post(target, "caption", media) is True
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url.endswith("/send_group_forward_msg")
    payload = json.loads(kwargs["content"])
    assert payload["group_id"] == 123456
    assert len(payload["messages"]) == 2
    assert all(node["type"] == "node" for node in payload["messages"])
    assert payload["messages"][0]["data"]["user_id"] == 2272248496
    assert payload["messages"][0]["data"]["content"][0]["type"] == "text"
    assert payload["messages"][0]["data"]["content"][1]["type"] == "image"
    assert payload["messages"][1]["data"]["content"][0]["type"] == "image"


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
