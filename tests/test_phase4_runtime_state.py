from __future__ import annotations

import asyncio

import httpx
import pytest


@pytest.mark.asyncio
async def test_credentials_runtime_locks_are_loop_scoped_and_cleanable():
    from config import credentials

    loop = asyncio.get_running_loop()
    suffix = f"phase4-{id(loop)}"
    file_lock = credentials.get_file_lock(f"{suffix}.txt")
    refresh_lock = credentials._get_refresh_lock(suffix)
    refresh_gate = credentials._get_refresh_semaphore()

    assert credentials._lock_owners[(id(loop), f"{suffix}.txt")] is loop
    assert credentials._lock_owners[(id(loop), suffix)] is loop
    assert credentials._refresh_semaphores[id(loop)][0] is loop

    removed = credentials.clear_loop_state(loop)
    assert removed >= 3
    assert (id(loop), f"{suffix}.txt") not in credentials._file_locks
    assert (id(loop), suffix) not in credentials._token_refresh_locks
    assert id(loop) not in credentials._refresh_semaphores

    # 清理后再次获取必须创建当前 loop 的新对象，而不是复用旧锁。
    assert credentials.get_file_lock(f"{suffix}.txt") is not file_lock
    assert credentials._get_refresh_lock(suffix) is not refresh_lock
    assert credentials._get_refresh_semaphore() is not refresh_gate
    credentials.clear_loop_state(loop)


@pytest.mark.asyncio
async def test_http_pool_lifecycle_lock_can_be_released_after_loop_shutdown():
    from src import http_pool

    loop = asyncio.get_running_loop()
    http_pool._get_lifecycle_lock()
    assert id(loop) in http_pool._lifecycle_locks
    assert http_pool.clear_loop_state(loop) is True
    assert id(loop) not in http_pool._lifecycle_locks
    assert http_pool.clear_loop_state(loop) is False


@pytest.mark.asyncio
async def test_tagger_wait_pending_cancels_stalled_background_task():
    from src import tagger

    blocker = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled_task():
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(stalled_task())
    tagger._bg_tasks.add(task)
    task.add_done_callback(tagger._bg_tasks.discard)
    try:
        assert await tagger.wait_pending(timeout=0.01) is True
        assert cancelled.is_set()
        assert task.done()
    finally:
        blocker.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        tagger._bg_tasks.discard(task)


@pytest.mark.asyncio
async def test_manual_trigger_does_not_overlap_three_monitor_cycles(monkeypatch):
    import src.app as app
    from src import health
    from src.app_modules import message_worker

    health.initialize(
        cycle_timeout_seconds=1,
        monitor_stale_seconds=2,
        startup_grace_seconds=0,
    )
    health.get_tracker().set_startup_state("READY")
    calls = 0
    active = 0
    max_active = 0

    async def cycle():
        nonlocal calls, active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.005)
            calls += 1
        finally:
            active -= 1

    def should_stop():
        return calls >= 3

    async def wait_or_trigger(_event, _timeout):
        return True

    monkeypatch.setattr(app, "_run_cycle", cycle)
    monkeypatch.setattr(app, "_stop_requested", should_stop)
    monkeypatch.setattr(message_worker, "_calc_sleep_seconds", lambda: 0)
    monkeypatch.setattr(message_worker, "_next_interval", lambda: (1, "test"))
    monkeypatch.setattr(message_worker, "_wait_or_trigger", wait_or_trigger)
    monkeypatch.setattr(message_worker.cfg, "MESSAGE_CYCLE_TIMEOUT_SECONDS", 1, raising=False)

    await message_worker._run_loop(None, asyncio.Event())

    assert calls == 3
    assert max_active == 1
    assert active == 0


@pytest.mark.asyncio
async def test_timeout_then_transport_error_still_recovers_for_three_cycles(monkeypatch):
    import src.app as app
    from src import health, http_pool
    from src.app_modules import message_worker

    health.initialize(
        cycle_timeout_seconds=0.01,
        monitor_stale_seconds=1,
        startup_grace_seconds=0,
    )
    health.get_tracker().set_startup_state("READY")
    monkeypatch.setattr(http_pool, "_general_client", None)
    blocker = asyncio.Event()
    semaphore = asyncio.Semaphore(1)
    lock = asyncio.Lock()
    calls = 0
    active = 0
    max_active = 0

    async def cycle():
        nonlocal calls, active, max_active
        async with semaphore, lock:
            active += 1
            max_active = max(max_active, active)
            try:
                calls += 1
                if calls == 1:
                    await blocker.wait()
                elif calls == 2:
                    raise httpx.ConnectError("proxy disconnected")
                await asyncio.sleep(0.005)
            finally:
                active -= 1

    def should_stop():
        return calls >= 3

    async def wait_or_trigger(_event, _timeout):
        return True

    monkeypatch.setattr(app, "_run_cycle", cycle)
    monkeypatch.setattr(app, "_stop_requested", should_stop)
    monkeypatch.setattr(message_worker, "_calc_sleep_seconds", lambda: 0)
    monkeypatch.setattr(message_worker, "_next_interval", lambda: (1, "test"))
    monkeypatch.setattr(message_worker, "_wait_or_trigger", wait_or_trigger)
    monkeypatch.setattr(message_worker.cfg, "MESSAGE_CYCLE_TIMEOUT_SECONDS", 0.01, raising=False)

    await message_worker._run_loop(None, asyncio.Event())

    assert calls == 3
    assert max_active == 1
    assert active == 0
    await asyncio.wait_for(semaphore.acquire(), timeout=0.2)
    semaphore.release()
    await asyncio.wait_for(lock.acquire(), timeout=0.2)
    lock.release()


@pytest.mark.asyncio
async def test_cycle_timeout_rebuilds_only_an_existing_general_client(monkeypatch):
    from src import http_pool, health
    from src.app_modules import message_worker

    health.initialize(
        cycle_timeout_seconds=0.01,
        monitor_stale_seconds=1,
        startup_grace_seconds=0,
    )
    health.get_tracker().set_startup_state("READY")
    marker = object()
    monkeypatch.setattr(http_pool, "_general_client", marker)
    resets: list[object] = []

    async def fake_reset():
        resets.append(marker)
        return object()

    monkeypatch.setattr(http_pool, "reset_general_client", fake_reset)

    async def never_finishes():
        await asyncio.Event().wait()

    with pytest.raises(asyncio.TimeoutError):
        await message_worker._run_cycle_bounded(never_finishes, 0.01, "pool-reset-timeout")
    assert resets == [marker]


@pytest.mark.asyncio
async def test_cancelled_member_push_checkpoints_last_successful_watermark(monkeypatch):
    from src import fetcher

    first_time = "2026-09-08T00:01:00Z"
    watermark_calls: list[str] = []
    entered_second = asyncio.Event()
    blocker = asyncio.Event()

    async def handle(_member, msg, _id_list, _id_set, l_time_ref):
        if msg["id"] == "first":
            l_time_ref[0] = first_time
            return True
        entered_second.set()
        await blocker.wait()
        return True

    monkeypatch.setattr(fetcher, "_handle_message", handle)
    monkeypatch.setattr(
        fetcher.archive,
        "set_timeline_watermark",
        lambda _group, _member, value: watermark_calls.append(value),
    )

    member = {"m_name": "测试成员", "group_type": "nogizaka46", "m_id": "1"}
    task = asyncio.create_task(
        fetcher._push_member_messages(
            member,
            [
                {"id": "first", "updated_at": first_time},
                {"id": "second", "updated_at": "2026-09-08T00:02:00Z"},
            ],
            [],
            set(),
            ["2026-09-08T00:00:00Z"],
            "",
            None,
        )
    )
    await asyncio.wait_for(entered_second.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert watermark_calls == [first_time]


@pytest.mark.asyncio
async def test_failed_member_push_does_not_advance_watermark_to_failed_message(monkeypatch):
    from src import fetcher

    first_time = "2026-09-08T00:01:00Z"
    second_time = "2026-09-08T00:02:00Z"
    watermark_calls: list[str] = []

    async def handle(_member, msg, _id_list, _id_set, l_time_ref):
        if msg["id"] == "first":
            l_time_ref[0] = first_time
            return True
        # 模拟投递失败：不得修改 l_time_ref。
        return False

    async def write_record(*_args):
        return None

    monkeypatch.setattr(fetcher, "_handle_message", handle)
    monkeypatch.setattr(
        fetcher.archive,
        "set_timeline_watermark",
        lambda _group, _member, value: watermark_calls.append(value),
    )
    monkeypatch.setattr(fetcher, "write_time_record", write_record)

    member = {"m_name": "测试成员", "group_type": "nogizaka46", "m_id": "1"}
    result = await fetcher._push_member_messages(
        member,
        [
            {"id": "first", "updated_at": first_time},
            {"id": "second", "updated_at": second_time},
        ],
        [],
        set(),
        ["2026-09-08T00:00:00Z"],
        "",
        None,
    )

    assert result is False
    assert watermark_calls == [first_time]


@pytest.mark.asyncio
async def test_message_retry_skips_routes_persisted_as_successful(monkeypatch):
    from src import fetcher
    from src.notifier import DeliveryReport

    delivered = {"tg:main"}
    seen_skip_sets: list[set[str]] = []

    async def archive_message(*_args, **_kwargs):
        return None

    async def send_message(_member, _chain, *, skip_route_ids=None):
        seen_skip_sets.append(set(skip_route_ids or set()))
        return DeliveryReport(())

    monkeypatch.setattr(fetcher.cfg, "ENABLE_TRANSLATION", False, raising=False)
    monkeypatch.setattr(fetcher.cfg, "QQ_SEND_INTERVAL", 0, raising=False)
    monkeypatch.setattr(fetcher.archive, "archive_message", archive_message)
    monkeypatch.setattr(fetcher, "successful_routes", lambda *_args: delivered)
    monkeypatch.setattr(fetcher, "mark_successful_routes", lambda *_args: None)
    monkeypatch.setattr(fetcher, "save_sent_id", lambda *_args: None)
    monkeypatch.setattr(fetcher, "send_member_message_detailed", send_message)
    monkeypatch.setattr(fetcher, "build_message_chain", lambda *_args, **_kwargs: [])

    member = {"m_name": "测试成员", "group_type": "nogizaka46", "m_id": "1"}
    result = await fetcher._handle_message(
        member,
        {"id": "message-1", "updated_at": "2026-09-08T00:01:00Z", "text": "hello"},
        [],
        set(),
        ["2026-09-08T00:00:00Z"],
    )

    assert result is True
    assert seen_skip_sets == [delivered]


@pytest.mark.asyncio
async def test_route_lane_failure_does_not_block_healthy_route(monkeypatch):
    """较早消息的 NapCat 失败时，TG lane 仍按顺序处理后续消息。"""

    from src import fetcher
    from src.notifier import DeliveryAttempt, DeliveryReport

    attempts: list[tuple[str, set[str]]] = []
    successful: list[tuple[str, set[str]]] = []
    pending: list[tuple[str, set[str], set[str]]] = []
    saved_ids: list[str] = []

    async def archive_message(*_args, **_kwargs):
        return None

    async def send_message(_member, _chain, *, skip_route_ids=None):
        skipped = set(skip_route_ids or set())
        message_id = str(_chain[0].get("data", {}).get("text", ""))
        attempts.append((message_id, skipped))
        matched = ("napcat:1", "tg:main")
        route_attempts = []
        if "napcat:1" not in skipped:
            route_attempts.append(
                DeliveryAttempt("napcat", "napcat:1", "NapCat", "群 1", False, "timeout")
            )
        if "tg:main" not in skipped:
            route_attempts.append(
                DeliveryAttempt("tg", "tg:main", "TG", "Chat 1", True, None)
            )
        return DeliveryReport(tuple(route_attempts), matched)

    def mark_ok(_group, _member, message_id, route_ids, **_kwargs):
        successful.append((str(message_id), set(route_ids)))

    def mark_pending(_group, _member, message_id, _message_time, route_ids, *, errors=None, attempted_route_ids=None):
        pending.append((str(message_id), set(route_ids), set(attempted_route_ids or set())))

    monkeypatch.setattr(fetcher.cfg, "ENABLE_TRANSLATION", False, raising=False)
    monkeypatch.setattr(fetcher.cfg, "QQ_SEND_INTERVAL", 0, raising=False)
    monkeypatch.setattr(fetcher.archive, "archive_message", archive_message)
    monkeypatch.setattr(fetcher, "build_message_chain", lambda _name, _updated, msg, *_args, **_kwargs: [
        {"type": "text", "data": {"text": str(msg["id"])}}
    ])
    monkeypatch.setattr(fetcher, "successful_routes", lambda *_args: set())
    monkeypatch.setattr(fetcher, "mark_successful_routes", mark_ok)
    monkeypatch.setattr(fetcher, "mark_pending_routes", mark_pending)
    monkeypatch.setattr(fetcher, "save_sent_id", lambda _g, _m, message_id, *_args: saved_ids.append(str(message_id)))
    monkeypatch.setattr(fetcher, "send_member_message_detailed", send_message)
    monkeypatch.setattr(fetcher.archive, "set_timeline_watermark", lambda *_args: None)

    member = {"m_name": "测试成员", "group_type": "nogizaka46", "m_id": "1"}
    messages = [
        {"id": "m1", "updated_at": "2026-09-08T00:01:00Z", "text": "one"},
        {"id": "m2", "updated_at": "2026-09-08T00:02:00Z", "text": "two"},
        {"id": "m3", "updated_at": "2026-09-08T00:03:00Z", "text": "three"},
    ]
    result = await fetcher._push_member_messages(
        member, messages, [], set(), ["2026-09-08T00:00:00Z"], "", None
    )

    assert result is False
    assert [item[0] for item in attempts] == ["m1", "m2", "m3"]
    assert attempts[0][1] == set()
    assert attempts[1][1] == {"napcat:1"}
    assert attempts[2][1] == {"napcat:1"}
    assert [item[0] for item in successful] == ["m1", "m2", "m3"]
    assert all(item[1] == {"napcat:1"} for item in pending)
    assert pending[0][2] == {"napcat:1"}
    assert saved_ids == []


def test_pending_route_suspend_and_resume_preserves_recovery_state(tmp_path, monkeypatch):
    """停用路由不再反复回放；恢复同一 ID 后仍可补偿且不丢记录。"""

    import config.config as cfg
    from src import archive, delivery_state

    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(tmp_path), raising=False)
    archive.close_db()
    try:
        delivery_state.mark_pending_routes(
            "nogizaka46",
            "member-1",
            "message-1",
            "2026-09-08T00:01:00Z",
            {"napcat:123"},
            errors={"napcat:123": "qq_send_network_error"},
            attempted_route_ids={"napcat:123"},
        )
        assert delivery_state.pending_route_count("nogizaka46", "member-1") == 1

        changed = delivery_state.reconcile_pending_routes(
            "nogizaka46", "member-1", {"tg:main"}
        )
        assert changed == {"suspended": 1, "resumed": 0}
        assert delivery_state.pending_routes_for_member("nogizaka46", "member-1") == []
        suspended = delivery_state.pending_routes_for_member(
            "nogizaka46", "member-1", include_suspended=True
        )
        assert len(suspended) == 1
        assert suspended[0]["route_state"] == "suspended"
        assert suspended[0]["attempts"] == 1
        assert delivery_state.pending_backlog_summary("nogizaka46", "member-1") == {
            "routes": 0,
            "messages": 0,
            "members": 0,
            "suspended_routes": 1,
            "suspended_messages": 1,
            "suspended_members": 1,
        }

        changed = delivery_state.reconcile_pending_routes(
            "nogizaka46", "member-1", {"napcat:123"}
        )
        assert changed == {"suspended": 0, "resumed": 1}
        pending = delivery_state.pending_routes_for_member("nogizaka46", "member-1")
        assert [row["message_id"] for row in pending] == ["message-1"]
        assert pending[0]["route_state"] == "pending"
        assert pending[0]["last_error"] == "qq_send_network_error"
    finally:
        archive.close_db()


def test_successful_route_advances_to_next_pending_message(tmp_path, monkeypatch):
    """补偿 lane 成功一条后，阻塞指针应指向下一条而不是整条清空。"""

    import config.config as cfg
    from src import archive, delivery_state

    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(tmp_path), raising=False)
    archive.close_db()
    try:
        delivery_state.mark_pending_routes(
            "nogizaka46", "member-2", "message-1", "2026-09-08T00:01:00Z",
            {"napcat:123"}, errors={"napcat:123": "timeout"},
            attempted_route_ids={"napcat:123"},
        )
        delivery_state.mark_pending_routes(
            "nogizaka46", "member-2", "message-2", "2026-09-08T00:02:00Z",
            {"napcat:123"},
        )
        delivery_state.mark_successful_routes(
            "nogizaka46", "member-2", "message-1", {"napcat:123"},
            message_time="2026-09-08T00:01:00Z",
        )
        lane = delivery_state.route_lane_snapshot("nogizaka46", "member-2")
        assert lane[0]["last_success_message_id"] == "message-1"
        assert lane[0]["blocked_message_id"] == "message-2"
        assert lane[0]["pending_count"] == 1

        delivery_state.mark_successful_routes(
            "nogizaka46", "member-2", "message-2", {"napcat:123"},
            message_time="2026-09-08T00:02:00Z",
        )
        lane = delivery_state.route_lane_snapshot("nogizaka46", "member-2")
        assert lane[0]["last_success_message_id"] == "message-2"
        assert lane[0]["blocked_message_id"] is None
        assert lane[0]["pending_count"] == 0
    finally:
        archive.close_db()
