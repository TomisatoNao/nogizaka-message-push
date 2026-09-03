"""阶段一 1.1：领域对象契约测试。"""

from __future__ import annotations

from dataclasses import fields
from inspect import signature

from src.social.contracts import DeliveryResult, DeliveryTarget, SocialDeliveryResult
from src.social.errors import (
    SocialDownloadError,
    SocialParseError,
    SocialTranslationError,
)
from src.social.models import Post
from src.social.service import SocialService


def test_delivery_target_exposes_only_the_planned_constructor_contract():
    params = list(signature(DeliveryTarget).parameters)
    assert params == ["channel", "target_id", "scope", "bot_name"]

    target = DeliveryTarget(
        channel="qq_official",
        target_id="openid-1",
        scope="users",
        bot_name="official_bot1",
    )
    assert target.channel == "qq_official"
    assert target.target_id == "openid-1"
    assert target.scope == "users"
    assert target.bot_name == "official_bot1"


def test_delivery_result_is_the_core_result_and_keeps_legacy_alias_only():
    result = DeliveryResult(
        outcome="partial",
        matched_routes=2,
        attempted_routes=2,
        success_routes=1,
        failed_routes=1,
        skipped_routes=0,
        errors=("route failed",),
    )
    assert isinstance(result, DeliveryResult)
    assert SocialDeliveryResult is DeliveryResult
    assert {f.name for f in fields(DeliveryResult)} >= {
        "outcome",
        "matched_routes",
        "attempted_routes",
        "success_routes",
        "failed_routes",
        "skipped_routes",
        "errors",
    }


def test_post_carries_request_id_without_breaking_existing_construction():
    post = Post(platform="instagram", post_id="p1", author="author")
    assert post.request_id == ""
    post.request_id = "req-1"
    assert post.request_id == "req-1"


def test_social_service_signatures_match_the_plan():
    assert list(signature(SocialService.parse_url).parameters) == [
        "self", "url", "request_id"
    ]
    assert list(signature(SocialService.prepare_post).parameters) == [
        "self", "post", "translate"
    ]
    assert list(signature(SocialService.deliver_post).parameters) == [
        "self", "post", "targets", "archive"
    ]
    assert list(signature(SocialService.process_url).parameters) == [
        "self", "url", "targets", "translate", "archive", "request_id"
    ]


def test_social_service_converts_parser_and_component_failures_to_domain_errors():
    post = Post(platform="instagram", post_id="p1", author="author")

    class _Parser:
        def parse(self, _url):
            raise ValueError("bad parser")

    service = SocialService({}, parser=_Parser())
    try:
        service.parse_url("https://example.test/p/1", request_id="req-1")
    except SocialParseError as exc:
        assert exc.request_id == "req-1"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("parser failure was not typed")

    class _Downloader:
        def download(self, _post):
            raise OSError("download failed")

    service = SocialService({}, parser=type("P", (), {"parse": lambda _s, _u: post})(), downloader=_Downloader())
    try:
        service.process_post(post, translate=False)
    except SocialDownloadError as exc:
        assert exc.post_id == "p1"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("download failure was not typed")

    class _Preparation:
        def prepare(self, _post, *, translate=True):
            raise RuntimeError("translation failed")

    service = SocialService({}, parser=type("P", (), {"parse": lambda _s, _u: post})(), preparation=_Preparation())
    try:
        service.prepare_post(post)
    except SocialTranslationError as exc:
        assert exc.post_id == "p1"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("preparation failure was not typed")


def test_social_service_rejects_legacy_string_targets_in_the_core():
    service = SocialService({})
    post = Post(platform="instagram", post_id="p1", author="author")
    try:
        service.deliver_post(post, ["official:bot1:private"])
    except TypeError as exc:
        assert "DeliveryTarget" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("legacy target string reached the core")
