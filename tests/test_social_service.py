"""统一社媒服务层的契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.social.forwarder import (
    PreparedSocialPost,
    SocialDeliveryResult,
    SocialForwarder,
)
from src.social.models import MediaItem, Post
from src.social.service import SocialService, delivery_to_dict


class _Parser:
    def __init__(self, post):
        self.post = post
        self.calls = []

    def parse(self, url):
        self.calls.append(url)
        return self.post


class _Downloader:
    def __init__(self):
        self.posts = []

    def download(self, post):
        self.posts.append(post)


class _Forwarder:
    def __init__(self):
        self.prepare_calls = []
        self.forward_calls = []
        self.qq_calls = []
        self.last_delivery_result = None

    def prepare_post(self, post, *, translate=True):
        self.prepare_calls.append((post, translate))
        return PreparedSocialPost(
            translated="译文" if translate else None,
            alt_translations={},
            full_text="统一正文",
        )

    def forward_post(self, post, target_channels=None, *, archive=True, prepared=None):
        self.forward_calls.append((post, target_channels, archive, prepared))
        self.last_delivery_result = SocialDeliveryResult(
            outcome="success",
            matched_routes=1,
            attempted_routes=1,
            success_routes=1,
            failed_routes=0,
        )
        return True

    async def send_qq_target(self, post, bot, scope, target_id, *, prepared=None, archive=True):
        self.qq_calls.append((post, bot, scope, target_id, prepared, archive))
        return SocialDeliveryResult(
            outcome="success",
            matched_routes=1,
            attempted_routes=1,
            success_routes=1,
            failed_routes=0,
            media_sent=len(post.media),
            media_total=len(post.media),
        )


def _service():
    post = Post(platform="instagram", post_id="p1", author="author", text="原文")
    parser = _Parser(post)
    downloader = _Downloader()
    forwarder = _Forwarder()
    service = SocialService(
        {}, parser=parser, downloader=downloader, forwarder=forwarder
    )
    return service, post, parser, downloader, forwarder


def test_process_url_has_one_prepare_and_one_delivery():
    service, post, parser, downloader, forwarder = _service()

    result = service.process_url(
        "https://www.instagram.com/p/p1/",
        targets=["official:bot1:private"],
        translate=True,
        archive=False,
    )

    assert result.post is post
    assert result.completed is True
    assert result.prepared is not None
    assert result.prepared.full_text == "统一正文"
    assert parser.calls == ["https://www.instagram.com/p/p1/"]
    assert downloader.posts == [post]
    assert len(forwarder.prepare_calls) == 1
    assert len(forwarder.forward_calls) == 1
    assert forwarder.forward_calls[0][1] == ["official:bot1:private"]
    assert forwarder.forward_calls[0][2] is False


@pytest.mark.asyncio
async def test_process_url_to_qq_reuses_prepared_contract():
    service, post, _parser, downloader, forwarder = _service()
    bot = object()

    result = await service.process_url_to_qq(
        "https://www.instagram.com/p/p1/",
        bot,
        "users",
        "openid-1",
        translate=False,
        archive=True,
    )

    assert result.completed is True
    assert result.prepared is not None
    assert result.prepared.translated is None
    assert downloader.posts == [post]
    assert len(forwarder.prepare_calls) == 1
    assert forwarder.prepare_calls[0][1] is False
    assert len(forwarder.qq_calls) == 1
    assert forwarder.qq_calls[0][1:] == (
        bot,
        "users",
        "openid-1",
        result.prepared,
        True,
    )


def test_delivery_to_dict_is_stable_and_includes_media_counts():
    result = SocialDeliveryResult(
        outcome="partial",
        matched_routes=2,
        attempted_routes=2,
        success_routes=1,
        failed_routes=1,
        skipped_routes=0,
        errors=("failed",),
        media_sent=2,
        media_total=3,
    )

    assert delivery_to_dict(result) == {
        "outcome": "partial",
        "matched_routes": 2,
        "attempted_routes": 2,
        "success_routes": 1,
        "failed_routes": 1,
        "skipped_routes": 0,
        "errors": ["failed"],
        "media_sent": 2,
        "media_total": 3,
    }
    assert delivery_to_dict(None) is None


@pytest.mark.asyncio
async def test_direct_qq_delivery_keeps_media_names_and_reports_counts(tmp_path: Path):
    class _Bot:
        def __init__(self):
            self.text_targets = []
            self.media = []

        async def send_private_text(self, target, text):
            self.text_targets.append((target, text))
            return True

        async def send_media_file(self, scope, target, media_type, content, filename=""):
            self.media.append((scope, target, media_type, content, filename))
            return True

    image = tmp_path / "Dcz0fgID5h__1.jpg"
    audio = tmp_path / "Dcz0fgID5h__2.ogg"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    post = Post(
        platform="instagram",
        post_id="Dcz0fgID5h_",
        author="author",
        media=[
            MediaItem(type="image", url="", local_path=str(image)),
            MediaItem(type="audio", url="", local_path=str(audio)),
        ],
    )
    forwarder = SocialForwarder({"social": {"translate": False}})
    bot = _Bot()
    prepared = PreparedSocialPost(None, {}, "统一正文")

    result = await forwarder.send_qq_target(
        post,
        bot,
        "users",
        "openid-1",
        prepared=prepared,
        archive=False,
    )

    assert result.outcome == "success"
    assert result.media_sent == result.media_total == 2
    assert [item[-1] for item in bot.media] == [image.name, audio.name]
    assert [item[2] for item in bot.media] == ["image", "record"]
