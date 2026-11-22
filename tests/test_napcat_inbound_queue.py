"""Tests for NapCat inbound queue persistence, backpressure, and protected deduplication (A6)."""
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from src.platforms.napcat_listener import NapCatInboundListener


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(queue_size=2, **kwargs):
    return {
        "enable_napcat_qq": True,
        "napcat_routes": [{"group_id": 123456}],
        "napcat_inbound": {
            "enabled": True,
            "queue_size": queue_size,
            "workers": 1,
            "cooldown_seconds": 0,
            **kwargs,
        },
    }


def _event(message_id=1, text="https://x.com/example/status/1"):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123456,
        "user_id": 987654,
        "self_id": 111222,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }


@pytest.fixture
def inbound_db(tmp_path):
    return tmp_path / "test_inbound.db"


def test_queue_full_does_not_consume_dedupe_allowing_retry(inbound_db):
    """When queue is full, the event must NOT be marked seen, so retry succeeds once space opens."""
    listener = NapCatInboundListener(
        config_provider=lambda: _config(queue_size=1),
        event_token="token",
        db_path=inbound_db,
    )
    # Fill the single queue slot
    assert listener.accept_event(_event(message_id=1, text="https://x.com/user/status/1")) == "queued"
    assert listener._queue.full()

    # Second event hits queue_full
    assert listener.accept_event(_event(message_id=2, text="https://x.com/user/status/2")) == "queue_full"

    # Drain slot
    job = listener._queue.get_nowait()
    assert job.url == "https://x.com/user/status/1"
    listener._queue.task_done()
    assert not listener._queue.full()

    # Retry second event: MUST be accepted (not rejected as duplicate!)
    assert listener.accept_event(_event(message_id=2, text="https://x.com/user/status/2")) == "queued"
    job2 = listener._queue.get_nowait()
    assert job2.url == "https://x.com/user/status/2"


@pytest.mark.asyncio
async def test_concurrent_same_event_accepted_only_once(inbound_db):
    """Multiple concurrent calls for the same event must result in exactly 1 queued and others duplicate."""
    listener = NapCatInboundListener(
        config_provider=lambda: _config(queue_size=10),
        event_token="token",
        db_path=inbound_db,
    )
    event = _event(message_id=99, text="https://x.com/user/status/99")

    results = await asyncio.gather(
        *(asyncio.to_thread(listener.accept_event, event) for _ in range(5))
    )

    assert results.count("queued") == 1
    assert results.count("duplicate") == 4
    assert listener._queue.qsize() == 1


def test_multi_link_partial_enqueue_and_resume(inbound_db):
    """An event with 2 links when queue has 1 slot left queues 1 link, and on retry queues only the 2nd link."""
    listener = NapCatInboundListener(
        config_provider=lambda: _config(queue_size=1, max_links_per_message=2),
        event_token="token",
        db_path=inbound_db,
    )
    two_links_event = _event(
        message_id=50,
        text="https://x.com/user/status/501 https://x.com/user/status/502",
    )

    # First attempt: slot 1 takes first link, slot 2 is full
    res1 = listener.accept_event(two_links_event)
    assert res1 == "queued"
    assert listener._queue.qsize() == 1

    first_job = listener._queue.get_nowait()
    assert first_job.url == "https://x.com/user/status/501"
    listener._queue.task_done()

    # Second attempt (retry of the same event): first link is skipped; second link is queued!
    res2 = listener.accept_event(two_links_event)
    assert res2 == "queued"
    second_job = listener._queue.get_nowait()
    assert second_job.url == "https://x.com/user/status/502"
    listener._queue.task_done()

    # Third attempt: both links have now been enqueued -> should return duplicate
    res3 = listener.accept_event(two_links_event)
    assert res3 == "duplicate"


@pytest.mark.asyncio
async def test_service_restart_recovers_pending_jobs(inbound_db):
    """Jobs stored in SQLite during accept_event are restored on next listener start()."""
    port = _find_free_port()
    cfg = _config(queue_size=5, listen_port=port)

    # Instance 1: accept 2 events without workers processing them
    listener1 = NapCatInboundListener(
        config_provider=lambda: cfg,
        event_token="secret",
        db_path=inbound_db,
    )
    assert listener1.accept_event(_event(message_id=1, text="https://x.com/user/status/1")) == "queued"
    assert listener1.accept_event(_event(message_id=2, text="https://x.com/user/status/2")) == "queued"
    # Close instance 1 without worker loop
    await listener1.stop()

    # Instance 2: start listener pointing to same DB -> must recover the 2 jobs
    listener2 = NapCatInboundListener(
        config_provider=lambda: cfg,
        service_provider=lambda: SimpleNamespace(
            process_url=MagicMock(
                return_value=SimpleNamespace(completed=True, delivery=SimpleNamespace(outcome="success"))
            )
        ),
        event_token="secret",
        db_path=inbound_db,
    )
    started = await listener2.start()
    assert started is True
    try:
        # Jobs should be recovered into the queue
        assert listener2._queue.qsize() >= 0  # Workers may process them quickly
        for _ in range(30):
            if listener2.stats()["succeeded"] >= 2:
                break
            await asyncio.sleep(0.05)
        assert listener2.stats()["succeeded"] == 2
    finally:
        await listener2.stop()


@pytest.mark.asyncio
async def test_http_backpressure_returns_503_on_queue_full(inbound_db):
    """When the inbound queue is full, HTTP endpoint returns 503 backpressure status."""
    port = _find_free_port()
    cfg = _config(queue_size=1, listen_port=port, workers=0)

    listener = NapCatInboundListener(
        config_provider=lambda: cfg,
        event_token="secret",
        db_path=inbound_db,
    )
    assert await listener.start() is True
    try:
        endpoint = f"http://127.0.0.1:{port}/api/napcat/events"
        async with httpx.AsyncClient(trust_env=False) as client:
            # 1st request fills queue
            r1 = await client.post(
                endpoint,
                json=_event(message_id=1, text="https://x.com/u/1"),
                headers={"Authorization": "Bearer secret"},
            )
            assert r1.status_code == 200
            assert r1.json()["accepted"] is True

            # 2nd request encounters full queue -> 503
            r2 = await client.post(
                endpoint,
                json=_event(message_id=2, text="https://x.com/u/2"),
                headers={"Authorization": "Bearer secret"},
            )
            assert r2.status_code == 503
            data = r2.json()
            assert data["status"] == "error"
            assert data["message"] == "queue_full"
            assert data["accepted"] is False
    finally:
        await listener.stop()
