"""NapCat 入站事件的安全边界、排队和统一社媒服务适配测试。"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest

from src.platforms.napcat_listener import (
    NapCatInboundJob,
    NapCatInboundListener,
    configured_group_ids,
    event_message_text,
)
from src.social.url_utils import extract_social_urls


def _config(**inbound):
    return {
        "enable_napcat_qq": True,
        "napcat_routes": [{"group_id": 123456}],
        "napcat_inbound": {
            "enabled": True,
            "queue_size": 4,
            "workers": 1,
            "cooldown_seconds": 0,
            **inbound,
        },
    }


def _event(*, group_id=123456, message_id=1, url="https://x.com/example/status/1"):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": 987654,
        "self_id": 111222,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": url}}],
    }


def test_group_routes_are_the_only_inbound_whitelist():
    assert configured_group_ids([
        {"group_id": 123456},
        {"group_id": "00123456"},
        {"group_id": "not-a-group"},
    ]) == frozenset({"123456"})


def test_event_text_supports_onebot_segments_and_raw_message():
    text = event_message_text(
        [
            {"type": "image", "data": {"url": "https://cdn.example/a.jpg"}},
            {"type": "text", "data": {"text": "https://x.com/a/status/1"}},
        ],
        "https://x.com/a/status/1",
    )
    assert text.count("https://x.com/a/status/1") == 1


def test_url_extractor_handles_links_adjacent_to_chinese_text():
    assert extract_social_urls("看这里https://x.com/a/status/1，谢谢") == (
        "https://x.com/a/status/1",
    )
    assert extract_social_urls("看这里https://x.com/a/status/1谢谢") == (
        "https://x.com/a/status/1",
    )


def test_listener_rejects_unauthorized_groups_and_self_messages():
    listener = NapCatInboundListener(
        config_provider=lambda: _config(),
        event_token="event-secret",
    )

    assert listener.accept_event(_event(group_id=999, message_id=1)) == "group_not_allowed"
    own = _event(message_id=2)
    own["user_id"] = own["self_id"]
    assert listener.accept_event(own) == "self_message"
    own_nested = _event(message_id=3)
    own_nested.pop("user_id")
    own_nested["sender"] = {"user_id": own_nested["self_id"]}
    assert listener.accept_event(own_nested) == "self_message"
    assert listener.stats()["queued"] == 0


def test_listener_deduplicates_events_and_enforces_message_url_allowlist():
    listener = NapCatInboundListener(
        config_provider=lambda: _config(),
        event_token="event-secret",
    )

    assert listener.accept_event(_event(message_id=10)) == "queued"
    assert listener.accept_event(_event(message_id=10)) == "duplicate"
    assert listener.accept_event(
        _event(message_id=11, url="https://evil.example/status/11")
    ) == "no_social_url"
    job = listener._queue.get_nowait()
    assert isinstance(job, NapCatInboundJob)
    assert job.group_id == "123456"
    assert job.url == "https://x.com/example/status/1"
    assert job.self_id == "111222"


@pytest.mark.asyncio
async def test_processing_uses_shared_service_without_translation_by_default():
    calls = []

    class Service:
        def process_url(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(
                completed=True,
                delivery=SimpleNamespace(outcome="success"),
            )

    listener = NapCatInboundListener(
        config_provider=lambda: _config(),
        service_provider=lambda: Service(),
        event_token="event-secret",
    )
    job = NapCatInboundJob(
        group_id="123456",
        user_id="987654",
        message_id="10",
        url="https://x.com/example/status/1",
        received_at=0,
        self_id="111222",
    )

    await listener._process_job(job)

    assert calls and calls[0][0] == job.url
    assert calls[0][1]["translate"] is False
    assert calls[0][1]["archive"] is False
    target = calls[0][1]["targets"][0]
    assert target.channel == "napcat"
    assert target.target_id == "123456"
    assert target.scope == "groups"
    assert target.runtime == {"group_id": 123456, "self_id": "111222"}


@pytest.mark.asyncio
async def test_listener_requires_event_token_before_binding_port(monkeypatch):
    config = _config()
    monkeypatch.setenv("NAPCAT_EVENT_TOKEN", "")
    listener = NapCatInboundListener(config_provider=lambda: config)
    assert await listener.start() is False
    assert not listener.running


@pytest.mark.asyncio
async def test_http_endpoint_authenticates_before_enqueueing_and_filters_groups():
    port_socket = socket.socket()
    port_socket.bind(("127.0.0.1", 0))
    port = port_socket.getsockname()[1]
    port_socket.close()
    config = _config(listen_host="127.0.0.1", listen_port=port)
    calls = []

    class Service:
        def process_url(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(
                completed=True,
                delivery=SimpleNamespace(outcome="success"),
            )

    listener = NapCatInboundListener(
        config_provider=lambda: config,
        service_provider=lambda: Service(),
        event_token="event-secret",
    )
    assert await listener.start() is True
    try:
        endpoint = f"http://127.0.0.1:{port}/api/napcat/events"
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                endpoint,
                json=_event(message_id=20),
                headers={"Authorization": "Bearer wrong"},
            )
            assert response.status_code == 401

            response = await client.post(
                endpoint,
                json=_event(message_id=21),
                headers={"Authorization": "Bearer event-secret"},
            )
            assert response.status_code == 200
            assert response.json()["accepted"] is True

            response = await client.post(
                endpoint,
                json=_event(group_id=999, message_id=22),
                headers={"X-OneBot-Token": "event-secret"},
            )
            assert response.status_code == 200
            assert response.json()["accepted"] is False

        for _ in range(40):
            if calls:
                break
            await asyncio.sleep(0.025)
        assert calls and calls[0][0] == "https://x.com/example/status/1"
        assert listener.stats()["rejected"] >= 2
    finally:
        await listener.stop()
