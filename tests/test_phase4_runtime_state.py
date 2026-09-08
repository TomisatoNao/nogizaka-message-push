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
