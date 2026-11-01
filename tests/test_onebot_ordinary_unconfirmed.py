"""Integration coverage for NapCat uncertainty at the route boundary."""

import asyncio
import json

import httpx
import pytest

from src.platforms import napcat
from src.social.adapters import NapCatAdapter
from src.social.contracts import DeliveryTarget
from src.social.delivery_service import DeliveryService
from src.social.models import MediaItem, Post
from src.social.route_planner import PlannedRoute
from src.social.store import SocialStore


class _OkResponse:
    status_code = 200
    text = '{"status":"ok"}'

    @staticmethod
    def json():
        return {"status": "ok"}


class _ScriptedClient:
    def __init__(self, action):
        self.action = action
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        if isinstance(self.action, BaseException):
            raise self.action
        return self.action

    async def aclose(self):
        pass


class _Planner:
    def __init__(self, routes):
        self.routes = list(routes)

    def plan(self, *_args):
        return self.routes


def _route(target_id="100", adapter=None):
    target = DeliveryTarget("napcat", str(target_id), "groups").bind_runtime(
        {"group_id": int(target_id), "self_id": "10001"}
    )
    return PlannedRoute(
        f"napcat:{target_id}", adapter or NapCatAdapter(), target
    )


def _post(post_id="unconfirmed"):
    return Post(platform="instagram", post_id=post_id, author="tester", text="hello")


def _configure_napcat(monkeypatch, client):
    monkeypatch.setattr(napcat.cfg, "QQ_BOT_API", "http://napcat.invalid")
    monkeypatch.setattr(napcat.cfg, "QQ_SEND_INTERVAL", 0)
    # Call this from the active test loop so napcat never constructs a real
    # temporary AsyncClient for the fake transport.
    napcat.initialize(client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("read timeout"),
        httpx.WriteTimeout("write timeout"),
        httpx.ReadError("read lost"),
        httpx.WriteError("write lost"),
        httpx.RemoteProtocolError("connection closed"),
    ],
    ids=["read-timeout", "write-timeout", "read-error", "write-error", "remote-protocol"],
)
async def test_ordinary_ambiguous_transport_errors_are_single_post(monkeypatch, error):
    client = _ScriptedClient(error)
    _configure_napcat(monkeypatch, client)

    outcome = await napcat.send_qq_message_detailed(
        100, [{"type": "text", "data": {"text": "hello"}}], max_retries=4
    )

    assert outcome.error_code == "delivery_unconfirmed"
    assert outcome.ok is True  # detailed API retains the accepted projection
    assert client.calls == 1
    assert napcat.get_send_gate_snapshot()["last_error"] == "delivery_unconfirmed"


@pytest.mark.asyncio
async def test_connect_timeout_is_safe_to_retry(monkeypatch):
    class QueueClient:
        def __init__(self):
            self.responses = [httpx.ConnectTimeout("connect timeout"), _OkResponse()]
            self.calls = 0

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

        async def aclose(self):
            pass

    client = QueueClient()
    _configure_napcat(monkeypatch, client)

    async def no_backoff(_seconds):
        pass

    monkeypatch.setattr(napcat.asyncio, "sleep", no_backoff)
    progress = napcat.NapCatDeliveryProgress()
    with napcat.track_delivery_progress(progress):
        outcome = await napcat.send_qq_message_detailed(
            100, [{"type": "text", "data": {"text": "hello"}}], max_retries=2
        )

    assert outcome.ok and outcome.error_code == ""
    assert client.calls == 2
    assert progress.confirmed_batches == 1


@pytest.mark.asyncio
async def test_429_backoff_outer_timeout_stays_retryable(monkeypatch, tmp_path):
    class _RateLimitedResponse:
        status_code = 429
        text = "rate limited"

    class RateLimitedClient:
        calls = 0

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            return _RateLimitedResponse()

        async def aclose(self):
            pass

    client = RateLimitedClient()
    _configure_napcat(monkeypatch, client)
    backoff_started = asyncio.Event()

    async def hold_in_backoff(_seconds):
        backoff_started.set()
        await asyncio.Future()

    monkeypatch.setattr(napcat.asyncio, "sleep", hold_in_backoff)
    route = _route()
    store = SocialStore(str(tmp_path / "state.db"))
    service = DeliveryService({}, planner=_Planner([route]), state=store)
    monkeypatch.setattr(service, "_route_timeout_seconds", lambda _route: 0.05)

    try:
        result_task = asyncio.create_task(
            service._deliver_routes(_post("rate-limited"), "hello", [route], set())
        )
        await backoff_started.wait()
        result = await result_task

        assert result.errors == ("napcat:100: timeout",)
        assert client.calls == 1
        assert store.unconfirmed_routes("instagram", "rate-limited") == set()
        assert store.delivered_routes("instagram", "rate-limited") == set()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_known_failure_after_confirmed_video_text_batch_is_unconfirmed(monkeypatch):
    class _RejectedResponse:
        status_code = 400
        text = "bad request"

    class QueueClient:
        def __init__(self):
            self.responses = [_OkResponse(), _RejectedResponse()]
            self.calls = 0

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

        async def aclose(self):
            pass

    client = QueueClient()
    _configure_napcat(monkeypatch, client)
    progress = napcat.NapCatDeliveryProgress()

    with napcat.track_delivery_progress(progress):
        outcome = await napcat.send_qq_message_detailed(
            100,
            [
                {"type": "text", "data": {"text": "caption"}},
                {"type": "video", "data": {"file": "video.mp4"}},
                {"type": "record", "data": {"file": "audio.amr"}},
            ],
            max_retries=1,
        )

    assert outcome.ok is True
    assert outcome.error_code == "delivery_unconfirmed"
    assert client.calls == 2
    assert progress.confirmed_batches == 1


@pytest.mark.asyncio
async def test_wait_for_timeout_before_post_stays_retryable(monkeypatch):
    client = _ScriptedClient(_OkResponse())
    _configure_napcat(monkeypatch, client)
    route = _route()
    service = DeliveryService({}, planner=_Planner([route]))
    monkeypatch.setattr(service, "_route_timeout_seconds", lambda _route: 0.01)

    queued = asyncio.Event()
    release = asyncio.Event()

    async def wait_while_queued():
        queued.set()
        await release.wait()

    monkeypatch.setattr(napcat._send_gate, "wait_turn", wait_while_queued)
    attempt_task = asyncio.create_task(
        service._send_route(route, "hello", [], post=_post("queued"))
    )
    await queued.wait()
    attempt = await attempt_task

    assert attempt.error == "timeout"
    assert client.calls == 0
    assert napcat.get_send_gate_snapshot()["last_error"] == "timeout"


@pytest.mark.asyncio
async def test_concurrent_routes_keep_post_timeout_progress_isolated(monkeypatch):
    slow_started = asyncio.Event()
    calls = []
    cancelled = []

    class SelectiveClient:
        async def post(self, *_args, **kwargs):
            body = json.loads(kwargs["content"])
            text = body["message"][0]["data"]["text"]
            calls.append(text)
            if text == "slow":
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(text)
                    raise
            return _OkResponse()

        async def aclose(self):
            pass

    _configure_napcat(monkeypatch, SelectiveClient())
    slow_route = _route("101")
    fast_route = _route("102")
    service = DeliveryService({}, planner=_Planner([slow_route, fast_route]))
    monkeypatch.setattr(
        service,
        "_route_timeout_seconds",
        lambda route: 0.05 if route.target.target_id == "101" else 0.3,
    )

    slow_task = asyncio.create_task(
        service._send_route(slow_route, "slow", [], post=_post("slow"))
    )
    await slow_started.wait()
    fast_task = asyncio.create_task(
        service._send_route(fast_route, "fast", [], post=_post("fast"))
    )
    slow, fast = await asyncio.gather(slow_task, fast_task)

    assert slow.error == "delivery_unconfirmed"
    assert fast.ok and fast.error == ""
    assert cancelled == ["slow"]
    assert sorted(calls) == ["fast", "slow"]


@pytest.mark.asyncio
async def test_forward_post_outer_timeout_after_submission_is_unconfirmed(monkeypatch):
    class BlockingClient:
        def __init__(self):
            self.calls = 0
            self.cancelled = False

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def aclose(self):
            pass

    client = BlockingClient()
    _configure_napcat(monkeypatch, client)
    monkeypatch.setattr(
        NapCatAdapter,
        "_chain_item",
        classmethod(lambda _cls, _media, *, inline_budget=None: {
            "type": "image", "data": {"file": "http://media.invalid/image"}
        }),
    )
    route = _route()
    service = DeliveryService({}, planner=_Planner([route]))
    monkeypatch.setattr(service, "_route_timeout_seconds", lambda _route: 0.05)

    attempt = await service._send_route(
        route,
        "hello",
        [
            MediaItem(type="image", url="", local_path="first.jpg"),
            MediaItem(type="image", url="", local_path="second.jpg"),
        ],
        post=_post("forward-timeout"),
    )

    assert attempt.error == "delivery_unconfirmed"
    assert client.calls == 1 and client.cancelled


def test_submitted_wait_for_cancellation_is_held_after_restart(tmp_path, monkeypatch):
    class BlockingClient:
        def __init__(self):
            self.calls = 0
            self.cancelled = False

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def aclose(self):
            pass

    clients = []

    def dispatch_with_fake_client(coro):
        async def run():
            client = BlockingClient()
            clients.append(client)
            _configure_napcat(monkeypatch, client)
            return await coro

        return asyncio.run(run())

    monkeypatch.setattr(
        DeliveryService,
        "_dispatch_async",
        staticmethod(dispatch_with_fake_client),
    )
    monkeypatch.setattr(
        DeliveryService,
        "_route_timeout_seconds",
        lambda _self, _route: 0.02,
    )

    route = _route()
    planner = _Planner([route])
    database = str(tmp_path / "delivery-state.db")
    store = SocialStore(database)
    first = DeliveryService({}, planner=planner, state=store).deliver_post(
        _post("same-post"), "hello", archive=False
    )

    assert first.outcome == "unconfirmed"
    assert first.attempted_routes == 1
    assert clients[0].calls == 1 and clients[0].cancelled
    assert store.delivered_routes("instagram", "same-post") == set()
    assert store.unconfirmed_routes("instagram", "same-post") == {"napcat:100"}
    store.close()

    reopened = SocialStore(database)
    second = DeliveryService({}, planner=planner, state=reopened).deliver_post(
        _post("same-post"), "hello", archive=False
    )
    assert second.outcome == "unconfirmed"
    assert second.attempted_routes == 0
    assert second.skipped_routes == 1
    assert clients[1].calls == 0
    reopened.close()
