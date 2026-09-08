from types import SimpleNamespace

from src.social.adapters import NapCatAdapter
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
