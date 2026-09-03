"""阶段一 1.1：领域对象契约测试。"""

from __future__ import annotations

from dataclasses import fields
from inspect import signature

from src.social.contracts import DeliveryResult, DeliveryTarget, SocialDeliveryResult
from src.social.errors import (
    SocialDownloadError,
    SocialParseError,
    SocialTranslationError,
    SocialDeliveryError,
)
from src.social.delivery_service import DeliveryService
from src.social.route_planner import PlannedRoute
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
    assert [f.name for f in fields(DeliveryResult)] == [
        "outcome",
        "matched_routes",
        "attempted_routes",
        "success_routes",
        "failed_routes",
        "skipped_routes",
        "errors",
    ]


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


def test_service_preserves_request_id_on_typed_component_error():
    post = Post(platform="instagram", post_id="p1", author="author")

    class _Preparation:
        def prepare(self, _post, *, translate=True):
            from src.social.errors import SocialTranslationError

            raise SocialTranslationError("upstream")

    service = SocialService({}, preparation=_Preparation())
    try:
        service.prepare_post(post)
    except SocialTranslationError as exc:
        assert exc.post_id == "p1"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("typed preparation error was not propagated")


def test_delivery_logs_all_link_identifiers():
    logs = []

    class _Adapter:
        async def send_post(self, _target, _text, _media):
            return True

    class _Planner:
        adapters = {"test": _Adapter()}

        def plan(self, _post, _targets=None):
            target = DeliveryTarget("test", "destination")
            return [PlannedRoute("route:test", self.adapters["test"], target)]

    post = Post(
        platform="instagram",
        post_id="p1",
        author="author",
        request_id="req-1",
    )
    service = DeliveryService({}, planner=_Planner(), logger=lambda msg, **_kw: logs.append(msg))
    result = service.deliver_post(post, "text", archive=False)

    assert result.outcome == "success"
    joined = "\n".join(logs)
    assert "request_id=req-1" in joined
    assert "post_id=p1" in joined
    assert "route_id=route:test" in joined


def test_delivery_orchestration_failure_is_typed():
    class _Planner:
        def plan(self, _post, _targets=None):
            raise RuntimeError("planner exploded")

    post = Post(
        platform="instagram",
        post_id="p1",
        author="author",
        request_id="req-2",
    )
    service = DeliveryService({}, planner=_Planner(), logger=lambda *_a, **_k: None)
    try:
        service.deliver_post(post, "text", archive=False)
    except SocialDeliveryError as exc:
        assert exc.request_id == "req-2"
        assert exc.post_id == "p1"
        assert isinstance(exc.__cause__, RuntimeError)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("orchestration failure was not typed")
