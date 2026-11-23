from pathlib import Path
import sys
import asyncio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import http_pool


@pytest.mark.asyncio
async def test_http_pool_lifecycle():
    client_gen = await http_pool.get_general_client()
    assert client_gen is not None
    assert not client_gen.is_closed

    client_auth = await http_pool.get_auth_client()
    assert client_auth is not None
    assert not client_auth.is_closed

    client_qq = await http_pool.get_qq_client()
    assert client_qq is not None
    assert not client_qq.is_closed

    client_blog = await http_pool.get_blog_client()
    assert client_blog is not None
    assert not client_blog.is_closed

    # Reset client
    client_new = await http_pool.reset_general_client()
    assert client_new is not None
    assert not client_new.is_closed

    # Close all
    await http_pool.close_all()
    assert client_gen.is_closed or client_new.is_closed
    assert client_auth.is_closed


@pytest.mark.asyncio
async def test_http_pool_reset_is_serialized_and_rebinds_modules():
    rebound = []

    def on_rebind(client):
        rebound.append(client)

    http_pool.register_general_client_rebind(on_rebind)
    try:
        original = await http_pool.get_general_client()
        first, second = await asyncio.gather(
            http_pool.reset_general_client(),
            http_pool.reset_general_client(),
        )
        assert first is not second
        assert original.is_closed
        assert first.is_closed
        assert not second.is_closed
        assert rebound[-2:] == [first, second]
    finally:
        http_pool.unregister_general_client_rebind(on_rebind)
        await http_pool.close_all()


@pytest.mark.asyncio
async def test_auth_pool_concurrent_failure_only_replaces_expected_client_once():
    rebound = []

    def on_rebind(client):
        rebound.append(client)

    http_pool.register_auth_client_rebind(on_rebind)
    try:
        original = await http_pool.get_auth_client()
        first, second = await asyncio.gather(
            http_pool.reset_auth_client(expected_client=original),
            http_pool.reset_auth_client(expected_client=original),
        )
        assert first is second
        assert first is not original
        assert original.is_closed
        assert not first.is_closed
        assert rebound[-1:] == [first]
    finally:
        http_pool.unregister_auth_client_rebind(on_rebind)
        await http_pool.close_all()


@pytest.mark.asyncio
async def test_http_pool_close_timeout_does_not_block(monkeypatch):
    class HangingClient:
        is_closed = False

        async def aclose(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(http_pool, "CLIENT_CLOSE_TIMEOUT_SECONDS", 0.05)
    started = asyncio.get_running_loop().time()
    closed = await http_pool._close_quietly(HangingClient())
    elapsed = asyncio.get_running_loop().time() - started
    assert closed is False
    assert elapsed < 1.0


def test_http_pool_does_not_reuse_clients_across_event_loops():
    async def get_client():
        return await http_pool.get_general_client()

    first = asyncio.run(get_client())
    second = asyncio.run(get_client())
    try:
        assert first is not second
        assert first.is_closed, "切换事件循环时应关闭并替换旧 Client"
    finally:
        asyncio.run(http_pool.close_all())


@pytest.mark.asyncio
async def test_general_pool_concurrent_closed_transport_only_replaces_once():
    try:
        original = await http_pool.get_general_client()
        first, second = await asyncio.gather(
            http_pool.reset_general_client(expected_client=original),
            http_pool.reset_general_client(expected_client=original),
        )
        assert first is second
        assert original.is_closed
        assert not first.is_closed
    finally:
        await http_pool.close_all()


@pytest.mark.asyncio
async def test_startup_and_recovery_keep_the_same_general_pool_configuration(monkeypatch):
    constructors = []
    original_constructor = http_pool.httpx.AsyncClient

    def capture(**kwargs):
        constructors.append(kwargs)
        return original_constructor(**kwargs)

    monkeypatch.setattr(http_pool.httpx, "AsyncClient", capture)
    monkeypatch.setattr(http_pool.cfg, "PROXY", "http://127.0.0.1:19090")
    monkeypatch.setattr(http_pool.cfg, "TIMEOUT", 99, raising=False)
    try:
        startup = http_pool.new_general_client()
        http_pool.bind_runtime_clients(startup)
        assert await http_pool.get_general_client() is startup
        replacement = await http_pool.reset_general_client(expected_client=startup)
        assert replacement is not startup
        assert len(constructors) == 2
        def settings(kwargs):
            return {key: value for key, value in kwargs.items() if key != "mounts"}

        assert settings(constructors[0]) == settings(constructors[1])
        from src.proxy_transport import ClosingHTTPProxyTransport

        assert all(isinstance(item["mounts"]["all://"], ClosingHTTPProxyTransport) for item in constructors)
        assert replacement.timeout.pool == 20
        assert constructors[0]["limits"].max_connections == 20
        assert constructors[0]["limits"].max_keepalive_connections == 10
        assert constructors[0]["proxy"] == "http://127.0.0.1:19090"
        await replacement.aclose()
        healed = await http_pool.get_general_client()
        assert not healed.is_closed
        assert settings(constructors[0]) == settings(constructors[2])
    finally:
        await http_pool.close_all()


@pytest.mark.asyncio
async def test_expected_unmanaged_general_client_is_not_taken_over(monkeypatch):
    monkeypatch.setattr(http_pool, "_general_client", None)
    async with http_pool.httpx.AsyncClient() as external:
        assert await http_pool.reset_general_client(expected_client=external) is external
        assert http_pool._general_client is None
        assert not external.is_closed


@pytest.mark.asyncio
async def test_recycle_idle_clients_replaces_bound_pools_and_skips_unbound():
    await http_pool.close_all()
    assert await http_pool.recycle_idle_clients() == []
    assert http_pool._auth_client is None
    assert http_pool._general_client is None

    rebound_auth = []
    rebound_general = []
    http_pool.register_auth_client_rebind(rebound_auth.append)
    http_pool.register_general_client_rebind(rebound_general.append)
    try:
        old_auth = await http_pool.get_auth_client()
        old_general = await http_pool.get_general_client()

        recycled = await http_pool.recycle_idle_clients()
        assert recycled == ["auth", "general"]
        assert old_auth.is_closed
        assert old_general.is_closed
        assert rebound_auth[-1] is not old_auth
        assert rebound_general[-1] is not old_general
    finally:
        http_pool.unregister_auth_client_rebind(rebound_auth.append)
        http_pool.unregister_general_client_rebind(rebound_general.append)
        await http_pool.close_all()


@pytest.mark.asyncio
async def test_monitor_loop_recycles_idle_clients_on_sleep_wakeup(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from src.app_modules import message_worker

    jst = timezone(timedelta(hours=9))
    stop_event = asyncio.Event()
    poll_event = asyncio.Event()
    recycle_calls = []
    cycle_calls = []

    async def fake_recycle():
        recycle_calls.append(True)
        return ["auth", "general"]

    async def fake_wait(*args, **kwargs):
        return "timeout"

    async def fake_cycle(*args, **kwargs):
        cycle_calls.append(True)
        stop_event.set()
        return message_worker._CycleResult()

    monkeypatch.setattr(message_worker, "_get_jst_now", lambda: datetime(2026, 11, 23, 2, 0, tzinfo=jst))
    monkeypatch.setattr(message_worker, "_wait_for_next_action", fake_wait)
    monkeypatch.setattr(message_worker, "_stop_requested", lambda: False)
    monkeypatch.setattr(message_worker, "_run_cycle", fake_cycle)
    if "src.app" in sys.modules:
        monkeypatch.setattr(sys.modules["src.app"], "_stop_requested", lambda: False)
        monkeypatch.setattr(sys.modules["src.app"], "_run_cycle", fake_cycle)
    monkeypatch.setattr(http_pool, "recycle_idle_clients", fake_recycle)
    monkeypatch.setattr(message_worker.cfg, "SLEEP_START_HOUR", 1, raising=False)
    monkeypatch.setattr(message_worker.cfg, "SLEEP_END_HOUR", 7, raising=False)

    await message_worker._run_loop(None, poll_event, stop_event=stop_event)
    assert recycle_calls == [True]
    assert cycle_calls == [True]


@pytest.mark.asyncio
async def test_member_delivery_intents_default_enabled_and_respects_kill_switch(monkeypatch):
    from unittest.mock import AsyncMock
    from src import fetcher
    from src.notifier import DeliveryReport

    captured_options = []

    async def fake_send_detailed(member, chain, **kwargs):
        captured_options.append(kwargs)
        return DeliveryReport(attempts=())

    monkeypatch.setattr(fetcher.archive, "archive_message", AsyncMock())
    monkeypatch.setattr(fetcher, "build_message_chain", lambda *a, **k: [])
    monkeypatch.setattr(fetcher, "successful_routes", lambda *a, **k: set())
    monkeypatch.setattr(fetcher, "mark_successful_routes", lambda *a, **k: None)
    monkeypatch.setattr(fetcher, "send_member_message_detailed", fake_send_detailed)

    member = {"group_type": "nogizaka", "m_id": "55", "m_name": "冨里 奈央"}
    msg = {"id": "m-1", "updated_at": "2026-11-23T08:00:00Z", "type": "text", "text": ""}

    # 默认开启发送意图落盘
    monkeypatch.delenv("MEMBER_DELIVERY_INTENTS_ENABLED", raising=False)
    await fetcher._handle_message(member, msg, [], set(), [None])
    assert captured_options[-1].get("intent_identity") == ("nogizaka", "55", "m-1", "2026-11-23T08:00:00Z")

    # 显式关闭开关时回退为不携带 intent_identity
    monkeypatch.setenv("MEMBER_DELIVERY_INTENTS_ENABLED", "0")
    await fetcher._handle_message(member, msg, [], set(), [None])
    assert "intent_identity" not in captured_options[-1]
