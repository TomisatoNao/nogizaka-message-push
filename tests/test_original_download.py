from __future__ import annotations

from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from src.webui_modules.archive import original_download


JPEG_BYTES = b"\xff\xd8\xff\xe0archive-test-image"


def _public_dns(monkeypatch):
    monkeypatch.setattr(
        original_download.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )


class _Response:
    def __init__(self, body=JPEG_BYTES, url="https://cdn.hinatazaka46.com/image.jpg", content_type="image/jpeg"):
        self.body = body
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.body[:size]


def test_validate_remote_url_only_allows_public_official_https_hosts(monkeypatch):
    _public_dns(monkeypatch)

    assert original_download._validate_remote_url("https://cdn.hinatazaka46.com/a.jpg") == (
        "https://cdn.hinatazaka46.com/a.jpg"
    )
    for url in (
        "http://cdn.hinatazaka46.com/a.jpg",
        "https://hinatazaka46.com.attacker.test/a.jpg",
        "https://user@cdn.hinatazaka46.com/a.jpg",
        "https://cdn.hinatazaka46.com:444/a.jpg",
        "https://127.0.0.1/a.jpg",
    ):
        with pytest.raises(original_download.OriginalDownloadError):
            original_download._validate_remote_url(url)


def test_validate_remote_url_rejects_private_dns_results(monkeypatch):
    monkeypatch.setattr(
        original_download.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("192.168.1.10", 443))],
    )

    with pytest.raises(original_download.OriginalDownloadError, match="非公网"):
        original_download._validate_remote_url("https://cdn.hinatazaka46.com/a.jpg")


def test_remote_image_fetch_checks_redirect_target_and_image_signature(monkeypatch):
    _public_dns(monkeypatch)
    responses = iter([_Response()])

    class _Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://cdn.hinatazaka46.com/image.jpg"
            assert timeout == original_download._DOWNLOAD_TIMEOUT_SECONDS
            return next(responses)

    monkeypatch.setattr(original_download, "build_opener", lambda *_handlers: _Opener())
    data, content_type, extension = original_download._fetch_official_image(
        "https://cdn.hinatazaka46.com/image.jpg"
    )
    assert (data, content_type, extension) == (JPEG_BYTES, "image/jpeg", ".jpg")

    redirect = original_download._OfficialRedirectHandler()
    with pytest.raises(original_download.OriginalDownloadError):
        redirect.redirect_request(
            None, None, 302, "Found", {}, "http://127.0.0.1/private.jpg"
        )

    class _BadOpener:
        def open(self, *_args, **_kwargs):
            return _Response(body=b"<html>not an image</html>")

    monkeypatch.setattr(original_download, "build_opener", lambda *_handlers: _BadOpener())
    with pytest.raises(original_download.OriginalDownloadError, match="内容校验"):
        original_download._fetch_official_image("https://cdn.hinatazaka46.com/image.jpg")


def test_original_download_route_sends_safe_attachment_headers(monkeypatch):
    from src.webui_modules.archive_handlers import _handle_archive_impl

    monkeypatch.setattr(
        original_download,
        "_fetch_official_image",
        lambda _url: (JPEG_BYTES, "image/jpeg", ".jpg"),
    )
    headers = {}
    handler = SimpleNamespace(
        path="/api/archive/download-original?" + urlencode({
            "url": "https://cdn.hinatazaka46.com/image.jpg",
            "filename": "奈央:写真.jpg",
        }),
        send_response=lambda code: headers.update(status=code),
        send_header=lambda key, value: headers.update({key: value}),
        end_headers=lambda: None,
        wfile=BytesIO(),
    )

    _handle_archive_impl(handler, "download-original", lambda **_kwargs: True, None)
    assert headers["status"] == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert "attachment" in headers["Content-Disposition"]
    assert "filename*=UTF-8''" in headers["Content-Disposition"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert handler.wfile.getvalue() == JPEG_BYTES


def test_original_download_route_applies_archive_guard():
    called = []
    handler = SimpleNamespace(path="/api/archive/download-original?url=x")

    assert original_download.handle_original_download(
        handler, "download-original", lambda **kwargs: called.append(kwargs) or False
    ) is True
    assert called == [{"need_admin": False}]
