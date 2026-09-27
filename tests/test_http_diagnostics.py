"""以真实本地连接池及受控并发验证超时归因、取消清理和脱敏。"""

import asyncio

from anyio import ClosedResourceError
import httpx
import pytest

from src import fetcher, http_pool, logger
from src.app_modules import message_worker
from src.http_diagnostics import (
    CycleHTTPDiagnostics, classify_http_error, cycle_diagnostics, observe_request,
)


@pytest.mark.asyncio
async def test_real_pool_exhaustion_is_distinct_from_application_queue():
    accepted = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            accepted.set()
            await release.wait()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/timeline?token=not-logged"
    diagnostics = CycleHTTPDiagnostics("pool-test")
    token = cycle_diagnostics.set(diagnostics)
    task = None
    try:
        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=1),
            timeout=httpx.Timeout(2, pool=0.05), trust_env=False,
        ) as client:
            async def request():
                async with observe_request("timeline", url) as sample:
                    response = await client.get(url)
                    sample.status_code = response.status_code
                    return response

            task = asyncio.create_task(request())
            await asyncio.wait_for(accepted.wait(), 2)
            with pytest.raises(httpx.PoolTimeout):
                await request()
            release.set()
            assert (await task).status_code == 200
            await asyncio.wait_for(finished.wait(), 2)
        totals = diagnostics.totals[("timeline", "127.0.0.1", "")]
        assert totals.results == {"pool_timeout": 1, "http_200": 1}
        assert totals.active == 0
        assert "not-logged" not in " ".join(diagnostics.report())
    finally:
        release.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        cycle_diagnostics.reset(token)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [True, False])
async def test_member_timeout_identifies_wait_phase_and_releases_only_owned_slot(queued):
    gate = asyncio.Semaphore(1)
    if queued:
        await gate.acquire()
    diagnostics = CycleHTTPDiagnostics("cancel-test")
    token = cycle_diagnostics.set(diagnostics)

    async def fetch():
        async with observe_request("timeline", "https://api.test/timeline", semaphore=gate):
            await asyncio.Event().wait()

    try:
        result = await message_worker._fetch_member_bounded(
            {"m_id": "1", "account_id": "test"}, fetch(), 0.03,
        )
        assert isinstance(result, message_worker._MemberFetchTimeout)
        expected = "semaphore_wait" if queued else "http_request"
        assert f"wait_phase={expected}" in result.diagnostics
        totals = diagnostics.totals[("timeline", "api.test", "")]
        assert totals.results[f"cancelled_{expected}"] == 1
        assert totals.active == 0
        assert gate.locked() is queued
    finally:
        if queued:
            gate.release()
        cycle_diagnostics.reset(token)


@pytest.mark.asyncio
async def test_thirteen_members_share_three_slots_without_losing_diagnostics():
    gate = asyncio.Semaphore(3)
    diagnostics = CycleHTTPDiagnostics("13-members")
    token = cycle_diagnostics.set(diagnostics)

    async def fetch():
        async with observe_request("timeline", "https://api.test/timeline", semaphore=gate) as sample:
            await asyncio.sleep(0.005)
            sample.status_code = 200
            return "ok"

    try:
        results = await asyncio.gather(*[
            message_worker._fetch_member_bounded({"m_id": str(i)}, fetch(), 1)
            for i in range(13)
        ])
        assert results == ["ok"] * 13
        totals = diagnostics.totals[("timeline", "api.test", "")]
        assert totals.peak == 3
        assert totals.calls == 13
        assert totals.results == {"http_200": 13}
        assert totals.queue_max_ms > 0
        assert totals.active == 0
        assert "request_ms_p95=" in diagnostics.report()[0]
    finally:
        cycle_diagnostics.reset(token)


@pytest.mark.asyncio
async def test_get_closed_transport_repairs_once_and_reuses_new_pool(monkeypatch):
    class Broken:
        async def get(self, *_args, **_kwargs):
            raise ClosedResourceError()

    class Healthy:
        async def get(self, *_args, **_kwargs):
            return httpx.Response(200)

    broken, healthy = Broken(), Healthy()
    resets = []

    async def reset(*, expected_client=None):
        resets.append(expected_client)
        return healthy

    monkeypatch.setattr(fetcher, "_http_client", broken)
    monkeypatch.setattr(fetcher, "_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(http_pool, "reset_general_client", reset)
    with pytest.raises(ClosedResourceError):
        await fetcher._fetch_get("https://api.test/", headers={}, account_id="demo", operation="timeline")
    response = await fetcher._fetch_get("https://api.test/", headers={}, account_id="demo", operation="timeline")
    assert response.status_code == 200
    assert resets == [broken]
    assert not fetcher._semaphore.locked()


@pytest.mark.parametrize("error,kind", [
    (httpx.PoolTimeout(""), "pool_timeout"),
    (httpx.ConnectTimeout(""), "connect_timeout"),
    (httpx.ReadTimeout(""), "read_timeout"),
    (httpx.WriteTimeout(""), "write_timeout"),
    (httpx.ConnectError(""), "connect_error"),
    (ClosedResourceError(), "closed_transport"),
])
def test_http_failure_phase_classification(error, kind):
    assert classify_http_error(error) == kind


@pytest.mark.parametrize("parameter", ["token", "ACCESS_TOKEN", "refresh_token", "api_key", "key", "signature"])
def test_error_url_query_values_are_redacted_without_losing_useful_context(parameter):
    url = f"https://api.test/timeline?count=200&{parameter}=secret%2Fvalue&order=asc"
    error = httpx.ConnectError(f"cannot connect to '{url}'", request=httpx.Request("GET", url))
    formatted = logger.format_httpx_error(error)
    assert "secret" not in formatted
    assert "count=200" in formatted
    assert "order=asc" in formatted
    assert "kind=connect_error" in formatted


def test_redaction_applies_before_terminal_file_and_admin_log_outputs(capsys, monkeypatch):
    from unittest.mock import Mock

    sink = Mock()
    monkeypatch.setattr(logger, "error_logger", sink)
    text = "failed https://api.test/?token=private-value&count=3"
    _, seq = logger.get_recent()
    logger.log_all(text, is_error=True)
    assert "private-value" not in capsys.readouterr().out
    assert "private-value" not in sink.error.call_args.args[0]
    recent, _ = logger.get_recent(seq)
    assert "private-value" not in recent[-1]["text"]


@pytest.mark.asyncio
async def test_pool_timeout_does_not_reset_shared_general_client(monkeypatch):
    class BusyClient:
        async def get(self, *_args, **_kwargs):
            raise httpx.PoolTimeout("busy")

    async def unexpected_reset(**_kwargs):
        pytest.fail("普通池的容量竞争不应触发重建和中断其他请求")

    monkeypatch.setattr(fetcher, "_http_client", BusyClient())
    monkeypatch.setattr(fetcher, "_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(http_pool, "reset_general_client", unexpected_reset)
    with pytest.raises(httpx.PoolTimeout):
        await fetcher._fetch_get("https://api.test/", headers={}, account_id="demo", operation="timeline")
    assert not fetcher._semaphore.locked()
