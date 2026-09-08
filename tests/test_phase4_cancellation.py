from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_gather_cancel_safe_releases_semaphore_after_cancellation():
    from src.async_utils import gather_cancel_safe

    semaphore = asyncio.Semaphore(1)
    lock = asyncio.Lock()
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    blocker = asyncio.Event()

    async def worker():
        async with semaphore, lock:
            entered.set()
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    parent = asyncio.create_task(gather_cancel_safe([worker()]))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent

    assert cancelled.is_set()
    await asyncio.wait_for(semaphore.acquire(), timeout=0.2)
    semaphore.release()
    await asyncio.wait_for(lock.acquire(), timeout=0.2)
    lock.release()


@pytest.mark.asyncio
async def test_archive_media_cancellation_removes_partial_temp_file(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from src import archive

    class HangingClient:
        is_closed = False

        def __init__(self):
            self.started = asyncio.Event()
            self.blocker = asyncio.Event()

        async def get(self, *_args, **_kwargs):
            self.started.set()
            await self.blocker.wait()
            raise AssertionError("取消后不应返回网络结果")

    client = HangingClient()
    monkeypatch.setattr(archive, "archive_root", lambda: tmp_path)
    monkeypatch.setattr(archive, "_media_client", client)
    monkeypatch.setattr(archive, "_media_sem", asyncio.Semaphore(1))

    task = asyncio.create_task(
        archive._download_media(
            "测试成员",
            datetime(2026, 9, 8, tzinfo=timezone.utc),
            {"id": "cancel-1", "updated_at": "2026-09-08T00:00:00Z", "type": "image", "file": "https://example.test/a.jpg"},
        )
    )
    await asyncio.wait_for(client.started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert list(tmp_path.rglob("*.tmp")) == []
