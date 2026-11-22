"""A5: 社交媒体投递与持久化闭环测试。

覆盖：
1. DeliveryResult 契约冻结（7个dataclass字段不变，通过观测支持 archived）。
2. SQLite 状态持久化失败转暂挂（outcome='unconfirmed', complete=False, 禁止重复投递）。
3. 真实上报归档状态（DeliveryResult.archived 与 SocialOperationResult.archived）。
4. 归档失败时 complete=False，防止推进游标，下轮跳过已投递路由并重试归档自愈。
5. release_held 释放内存暂挂。
"""

from __future__ import annotations

import sqlite3
from dataclasses import fields
import pytest

from src.social.adapters import DeliveryTarget
from src.social.contracts import DeliveryResult
from src.social.delivery_service import ArchiveService, DeliveryService
from src.social.models import MediaItem, Post
from src.social.route_planner import PlannedRoute
from src.social.service import SocialService


class _MockAdapter:
    def __init__(self, outcome: bool = True):
        self.outcome = outcome
        self.calls: list[str] = []

    async def send_post(self, target, _text, _media):
        self.calls.append(target.route_id)
        return self.outcome


class _MockPlanner:
    def __init__(self, routes: list[PlannedRoute]):
        self.routes = list(routes)
        self.adapters = {r.target.channel: r.adapter for r in routes}

    def plan(self, _post, _targets=None):
        return list(self.routes)


class _FaultyState:
    """模拟在 mark_route_result 时发生 SQLite 故障的状态仓储。"""

    def __init__(self, should_fail_save: bool = True):
        self.should_fail_save = should_fail_save
        self.success: set[str] = set()
        self.calls: list[tuple[str, bool, str]] = []

    def delivered_routes(self, _platform: str, _item_id: str) -> set[str]:
        return set(self.success)

    def mark_route_result(
        self, _platform: str, _item_id: str, route_id: str, ok: bool, error: str = ""
    ) -> None:
        self.calls.append((route_id, ok, error))
        if self.should_fail_save:
            raise sqlite3.OperationalError("database is locked")
        if ok:
            self.success.add(route_id)


def _make_post(post_id: str = "post-101", platform: str = "instagram") -> Post:
    return Post(
        platform=platform,
        post_id=post_id,
        author="test_author",
        text="测试正文内容",
        media=[MediaItem(type="image", url="https://example.test/img.jpg")],
    )


def test_delivery_result_contract_fields_and_archived_observation():
    """验证 DeliveryResult 的 7 个 dataclass 字段未受污染，同时支持 archived 观察属性。"""
    assert [f.name for f in fields(DeliveryResult)] == [
        "outcome",
        "matched_routes",
        "attempted_routes",
        "success_routes",
        "failed_routes",
        "skipped_routes",
        "errors",
    ]

    r1 = DeliveryResult(
        outcome="success",
        matched_routes=1,
        attempted_routes=1,
        success_routes=1,
        failed_routes=0,
        archived=True,
    )
    assert r1.archived is True
    assert r1.complete is True

    r2 = DeliveryResult(
        outcome="success",
        matched_routes=1,
        attempted_routes=1,
        success_routes=1,
        failed_routes=0,
        archived=False,
    )
    assert r2.archived is False
    assert r2.complete is True

    # 错误列表中包含状态或归档异常时，complete 必须为 False
    r3 = DeliveryResult(
        outcome="success",
        matched_routes=1,
        attempted_routes=1,
        success_routes=1,
        failed_routes=0,
        errors=("route:1: state_OperationalError",),
    )
    assert r3.complete is False

    r4 = DeliveryResult(
        outcome="success",
        matched_routes=1,
        attempted_routes=1,
        success_routes=1,
        failed_routes=0,
        errors=("archive: archive_failed",),
    )
    assert r4.complete is False


def test_state_persistence_failure_turns_to_held_and_prevents_duplicate_send():
    """通道发送成功但 SQLite 写库失败：outcome 转 unconfirmed，内存转暂挂，后续调用严禁重发。"""
    adapter = _MockAdapter(outcome=True)
    target = DeliveryTarget(channel="tg", target_id="chat-1", scope="", bot_name="")
    route = PlannedRoute("tg:chat-1", adapter, target)
    planner = _MockPlanner([route])
    state = _FaultyState(should_fail_save=True)

    service = DeliveryService({}, planner=planner, state=state, archive_service=ArchiveService(lambda _p: True))
    post = _make_post("post-state-fail")

    # 第一轮：外部投递成功，但状态落库异常
    result1 = service.deliver_post(post, "统一正文")
    assert result1.outcome == "unconfirmed"
    assert result1.complete is False
    assert any(": state_OperationalError" in e for e in result1.errors)
    assert adapter.calls == ["tg:chat-1"]

    # 第二轮：再次调用 deliver_post，该路由必须已被暂挂（held），绝不能二次调用通道适配器
    result2 = service.deliver_post(post, "统一正文")
    assert result2.outcome == "unconfirmed"
    assert result2.complete is False
    assert result2.skipped_routes == 1
    assert result2.attempted_routes == 0
    # 适配器调用次数仍为 1，杜绝重复投递！
    assert adapter.calls == ["tg:chat-1"]

    # 核实并释放暂挂后，方可再次调度
    service.release_held(post.platform, post.post_id)
    state.should_fail_save = False
    result3 = service.deliver_post(post, "统一正文")
    assert result3.outcome == "success"
    assert result3.complete is True
    assert adapter.calls == ["tg:chat-1", "tg:chat-1"]


def test_archive_failure_reports_accurately_and_marks_incomplete():
    """归档失败：DeliveryResult.archived=False，errors 记录 archive_failed，complete=False。"""
    adapter = _MockAdapter(outcome=True)
    target = DeliveryTarget(channel="tg", target_id="chat-1", scope="", bot_name="")
    route = PlannedRoute("tg:chat-1", adapter, target)
    planner = _MockPlanner([route])
    state = _FaultyState(should_fail_save=False)

    archive_service = ArchiveService(archive_fn=lambda _p: False)
    service = DeliveryService({}, planner=planner, state=state, archive_service=archive_service)
    post = _make_post("post-archive-fail")

    result = service.deliver_post(post, "统一正文", archive=True)
    assert result.archived is False
    assert "archive: archive_failed" in result.errors
    assert result.complete is False
    # 通道发送成功，状态已落库
    assert state.success == {"tg:chat-1"}


def test_social_service_process_post_archive_completion_closure():
    """SocialService.process_post 全流程自愈测试：
    首轮归档失败 -> completed=False（不推进游标）；
    次轮已投递路由自动跳过 -> 仅补做归档 -> 归档成功后 completed=True。
    """
    adapter = _MockAdapter(outcome=True)
    target = DeliveryTarget(channel="tg", target_id="chat-1", scope="", bot_name="")
    route = PlannedRoute("tg:chat-1", adapter, target)
    planner = _MockPlanner([route])
    state = _FaultyState(should_fail_save=False)

    archive_results = [False, True]

    def _flaky_archive(_p):
        return archive_results.pop(0)

    archive_service = ArchiveService(archive_fn=_flaky_archive)
    delivery_service = DeliveryService({}, planner=planner, state=state, archive_service=archive_service)
    social_service = SocialService({}, delivery_service=delivery_service)

    post = _make_post("post-healing-1")

    # 第一轮：通道成功，归档失败
    op1 = social_service.process_post(post, download=False, archive=True)
    assert op1.archived is False
    assert op1.completed is False
    assert op1.delivery.outcome == "success"
    assert op1.delivery.complete is False
    assert adapter.calls == ["tg:chat-1"]

    # 模拟调度器：由于 op1.completed is False，该 post 未加入 succeeded，未调用 mark_synced。
    # 第二轮：轮询重试
    op2 = social_service.process_post(post, download=False, archive=True)
    assert op2.archived is True
    assert op2.completed is True
    assert op2.delivery.outcome == "already_delivered"
    assert op2.delivery.complete is True
    # 适配器未产生重复发送！
    assert adapter.calls == ["tg:chat-1"]


def test_multiple_routes_partial_state_failure_isolates_and_holds():
    """多路由场景：Route1落库成功，Route2落库失败。
    Route2转暂挂，整体outcome为unconfirmed。下轮Route1跳过，Route2暂挂跳过，0次重复外发。
    """
    adapter = _MockAdapter(outcome=True)
    r1 = PlannedRoute("tg:chat-1", adapter, DeliveryTarget("tg", "chat-1"))
    r2 = PlannedRoute("tg:chat-2", adapter, DeliveryTarget("tg", "chat-2"))
    planner = _MockPlanner([r1, r2])

    class _SelectiveFailState:
        def __init__(self):
            self.success = set()

        def delivered_routes(self, _platform, _item_id):
            return set(self.success)

        def mark_route_result(self, _platform, _item_id, route_id, ok, error=""):
            if route_id == "tg:chat-2":
                raise sqlite3.OperationalError("disk I/O error")
            if ok:
                self.success.add(route_id)

    service = DeliveryService(
        {},
        planner=planner,
        state=_SelectiveFailState(),
        archive_service=ArchiveService(lambda _p: True),
    )
    post = _make_post("post-multi-routes")

    # 第一轮：两个路由外发成功，但 r2 写库失败
    res1 = service.deliver_post(post, "统一正文")
    assert res1.outcome == "unconfirmed"
    assert res1.complete is False
    assert any("tg:chat-2: state_OperationalError" in e for e in res1.errors)
    assert sorted(adapter.calls) == ["tg:chat-1", "tg:chat-2"]

    # 第二轮：r1 已在 delivered_routes，r2 在 in_memory_held；两个路由全部跳过！
    adapter.calls.clear()
    res2 = service.deliver_post(post, "统一正文")
    assert res2.outcome == "unconfirmed"
    assert res2.complete is False
    assert res2.skipped_routes == 2
    assert res2.attempted_routes == 0
    assert adapter.calls == []


def test_process_post_without_archive():
    """显式指定 archive=False 时，archived 为 False，但在投递成功时 completed 为 True。"""
    adapter = _MockAdapter(outcome=True)
    route = PlannedRoute("tg:chat-1", adapter, DeliveryTarget("tg", "chat-1"))
    planner = _MockPlanner([route])
    state = _FaultyState(should_fail_save=False)

    service = DeliveryService({}, planner=planner, state=state)
    social_service = SocialService({}, delivery_service=service)
    post = _make_post("post-no-archive")

    op = social_service.process_post(post, download=False, archive=False)
    assert op.archived is False
    assert op.completed is True
    assert op.delivery.outcome == "success"
    assert op.delivery.archived is False
    assert op.delivery.complete is True


@pytest.mark.asyncio
async def test_deliver_to_target_archive_error_handling():
    """deliver_to_target 直接QQ投递时，归档失败也如实反映在 errors 与 archived/complete 中。"""
    from unittest.mock import AsyncMock

    service = DeliveryService({}, archive_service=ArchiveService(lambda _p: False))
    post = _make_post("post-qq-target")

    mock_bot = AsyncMock()
    mock_bot.send_private_text = AsyncMock(return_value=True)
    mock_bot.send_media_file = AsyncMock(return_value=True)

    result = await service.deliver_to_target(
        post, "正文", mock_bot, "users", "user123", archive=True
    )
    assert result.archived is False
    assert "archive: archive_failed" in result.errors
    assert result.complete is False

