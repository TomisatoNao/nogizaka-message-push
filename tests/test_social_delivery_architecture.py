"""第三阶段投递架构契约测试。"""

from __future__ import annotations

from dataclasses import dataclass

from src.social.adapters import DeliveryTarget
from src.social.delivery_service import ArchiveService, DeliveryService
from src.social.models import MediaItem, Post
from src.social.route_planner import PlannedRoute


class _Adapter:
    def __init__(self, outcomes: dict[str, bool]):
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def send_post(self, target, _text, _media):
        self.calls.append(target.route_id)
        return self.outcomes.get(target.route_id, False)


class _Planner:
    def __init__(self, routes):
        self.routes = routes

    def plan(self, _post, _target_channels=None):
        return list(self.routes)


class _State:
    def __init__(self):
        self.success: set[str] = set()
        self.records: list[tuple[str, bool, str]] = []

    def delivered_routes(self, _platform, _item_id):
        return set(self.success)

    def mark_route_result(self, _platform, _item_id, route_id, ok, error=""):
        self.records.append((route_id, ok, error))
        if ok:
            self.success.add(route_id)


@dataclass
class _Archive:
    calls: int = 0

    def archive(self, _post):
        self.calls += 1
        return True


def _post():
    return Post(
        platform="instagram",
        post_id="post-1",
        author="account",
        text="正文",
        media=[MediaItem(type="image", url="https://example.test/a.jpg")],
    )


def test_delivery_service_persists_each_route_and_skips_successful_route():
    adapter = _Adapter({"route:ok": True, "route:retry": False})
    routes = [
        PlannedRoute("route:ok", adapter, DeliveryTarget("route:ok", "test", {})),
        PlannedRoute("route:retry", adapter, DeliveryTarget("route:retry", "test", {})),
    ]
    state = _State()
    archive = _Archive()
    service = DeliveryService(
        {},
        planner=_Planner(routes),
        state=state,
        archive_service=ArchiveService(archive.archive),
    )

    first = service.deliver_post(_post(), "统一正文")
    assert first.outcome == "partial"
    assert first.success_routes == 1 and first.failed_routes == 1
    assert adapter.calls == ["route:ok", "route:retry"]
    assert state.success == {"route:ok"}
    assert archive.calls == 1

    # 失败路由恢复后，已成功路由不会再次调用；结果包含跳过计数。
    adapter.outcomes["route:retry"] = True
    second = service.deliver_post(_post(), "统一正文", archive=False)
    assert second.outcome == "success"
    assert second.skipped_routes == 1
    assert second.attempted_routes == 1
    assert adapter.calls == ["route:ok", "route:retry", "route:retry"]


def test_delivery_service_returns_no_route_without_invoking_adapter():
    adapter = _Adapter({})
    service = DeliveryService(
        {},
        planner=_Planner([]),
        adapters={},
    )

    result = service.deliver_post(_post(), "统一正文", archive=False)
    assert result.outcome == "no_route"
    assert result.matched_routes == 0
    assert result.route_results == ()
    assert adapter.calls == []

