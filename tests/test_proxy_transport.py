"""真实 CONNECT/TLS 故障验证：失败、超时和取消不得永久消耗池容量。"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from src.proxy_transport import new_async_client


@asynccontextmanager
async def proxy_server(*, hang_tls=False, hold_http=False):
    handshake = asyncio.Event()
    http_started = asyncio.Event()
    release = asyncio.Event()
    tasks = set()
    headers_seen = []

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            headers_seen.append(headers)
            if headers.startswith(b"CONNECT "):
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await reader.read(4096)  # 收到 TLS ClientHello 后模拟上游故障。
                handshake.set()
                if hang_tls:
                    await release.wait()
            else:
                http_started.set()
                if hold_http:
                    await release.wait()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield url, handshake, http_started, release, headers_seen
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.gather(*tuple(tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_tls_failure_releases_slots_and_same_client_recovers():
    async with proxy_server() as (proxy, _, _, _, headers):
        async with new_async_client(
            proxy=proxy.replace("http://", "http://test:password@"),
            limits=httpx.Limits(max_connections=2),
            timeout=httpx.Timeout(2, pool=0.1),
        ) as client:
            # 原版 httpcore 1.0.9 在第三次请求开始永久 PoolTimeout。
            for _ in range(8):
                with pytest.raises(httpx.ConnectError):
                    await client.get("https://example.test/")
            assert (await client.get("http://example.test/recovered")).text == "ok"
        assert all(b"Proxy-Authorization: Basic " in value for value in headers)


@pytest.mark.asyncio
async def test_tls_timeout_releases_slots():
    async with proxy_server(hang_tls=True) as (proxy, _, _, _, _):
        async with new_async_client(
            proxy=proxy, limits=httpx.Limits(max_connections=1),
            timeout=httpx.Timeout(2, connect=0.1, pool=0.1),
        ) as client:
            for _ in range(3):
                with pytest.raises(httpx.ConnectTimeout):
                    await client.get("https://example.test/")
            assert (await client.get("http://example.test/recovered")).status_code == 200


@pytest.mark.asyncio
async def test_cancelled_tls_handshake_releases_slot():
    async with proxy_server(hang_tls=True) as (proxy, handshake, _, _, _):
        async with new_async_client(
            proxy=proxy, limits=httpx.Limits(max_connections=1), timeout=2,
        ) as client:
            task = asyncio.create_task(client.get("https://example.test/"))
            await asyncio.wait_for(handshake.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await client.get("http://example.test/recovered")).status_code == 200


@pytest.mark.asyncio
async def test_failed_tunnel_does_not_interrupt_other_requests_or_reset_busy_pool():
    async with proxy_server(hold_http=True) as (proxy, _, started, release, _):
        async with new_async_client(
            proxy=proxy, limits=httpx.Limits(max_connections=2),
            timeout=httpx.Timeout(3, pool=0.1),
        ) as client:
            active = asyncio.create_task(client.get("http://example.test/slow"))
            await asyncio.wait_for(started.wait(), 2)
            try:
                for _ in range(4):
                    with pytest.raises(httpx.ConnectError):
                        await client.get("https://example.test/")
                assert not active.done()
                # 真正容量竞争仍然报告 PoolTimeout，不关闭正在使用的池。
                started.clear()
                second = asyncio.create_task(client.get("http://other.test/slow"))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    with pytest.raises(httpx.PoolTimeout):
                        await client.get("http://third.test/queued")
                finally:
                    release.set()
                    assert (await second).status_code == 200
                assert (await active).status_code == 200
            finally:
                release.set()
                await asyncio.gather(active, return_exceptions=True)
