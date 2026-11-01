"""Unknown OneBot receipts are held and never automatically resent."""
from types import SimpleNamespace

import httpx
import pytest

from src import health
from src.platforms import napcat
from src.social.adapters import NapCatAdapter
from src.social.contracts import DeliveryTarget
from src.social.delivery_service import DeliveryService
from src.social.errors import SocialDeliveryUnconfirmed
from src.social.models import MediaItem, Post
from src.social.route_planner import PlannedRoute
from src.social.store import SocialStore


class Planner:
    def __init__(self, routes):
        self.routes = routes

    def plan(self, *_args):
        return self.routes


class UncertainAdapter:
    calls = 0

    async def send_post(self, *_args):
        self.calls += 1
        raise SocialDeliveryUnconfirmed('delivery_unconfirmed')


class _OkResponse:
    status_code = 200
    text = '{"status":"ok"}'

    @staticmethod
    def json():
        return {"status": "ok"}


class _ScriptedClient:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        action = self.actions.pop(0) if self.actions else _OkResponse()
        if callable(action):
            return await action()
        if isinstance(action, BaseException):
            raise action
        return action

    async def aclose(self):
        pass


def _napcat_target(group_id="100"):
    return DeliveryTarget("napcat", group_id, "groups").bind_runtime(
        {"group_id": int(group_id), "self_id": "10001"}
    )


def test_unknown_receipt_is_held_after_restart_not_marked_success(tmp_path):
    path = str(tmp_path / 'state.db')
    post = Post(platform='instagram', post_id='uncertain', author='test', text='test')
    target = DeliveryTarget('napcat', '100', 'groups')
    adapter = UncertainAdapter()
    route = PlannedRoute('napcat:100', adapter, target)
    state = SocialStore(path)
    first = DeliveryService({}, planner=Planner([route]), state=state).deliver_post(post, 'test', archive=False)
    assert first.outcome == 'unconfirmed'
    assert not first.complete and not first.any_success
    assert first.success_routes == first.failed_routes == 0
    assert state.delivered_routes('instagram', 'uncertain') == set()
    assert state.unconfirmed_routes('instagram', 'uncertain') == {'napcat:100'}
    state.close()
    reopened = SocialStore(path)
    second = DeliveryService({}, planner=Planner([route]), state=reopened).deliver_post(post, 'test', archive=False)
    assert second.outcome == 'unconfirmed' and not second.complete
    assert second.attempted_routes == 0 and second.media_sent == 0
    assert adapter.calls == 1
    reopened.close()


@pytest.mark.asyncio
async def test_adapter_propagates_unknown_forward_receipt(tmp_path, monkeypatch):
    paths = [tmp_path / 'one.jpg', tmp_path / 'two.jpg']
    for path in paths:
        path.write_bytes(b'test')
    async def unknown(*_args):
        return napcat.NapCatSendOutcome(True, 'delivery_unconfirmed')
    monkeypatch.setattr(napcat, 'send_group_forward_message_detailed', unknown)
    target = DeliveryTarget('napcat', '100', 'groups').bind_runtime({'group_id':100,'self_id':'10001'})
    with pytest.raises(SocialDeliveryUnconfirmed):
        await NapCatAdapter().send_post(target, 'test', [MediaItem(type='image',url='',local_path=str(p)) for p in paths])


@pytest.mark.asyncio
async def test_detailed_timeout_preserves_unknown_health_and_no_retry(monkeypatch):
    class Client:
        calls = 0
        async def post(self, *_args, **_kwargs):
            self.calls += 1
            raise httpx.ReadTimeout('test')
    client = Client()
    monkeypatch.setattr(napcat.cfg, 'QQ_BOT_API', 'http://gateway:3000')
    monkeypatch.setattr(napcat.cfg, 'QQ_SEND_INTERVAL', 0)
    napcat.initialize(client)
    before = health.get_tracker().napcat_send_snapshot().get('last_success_at')
    result = await napcat.send_group_forward_message_detailed(100, [{'type':'node','data':{}}], 3)
    assert result.error_code == 'delivery_unconfirmed' and client.calls == 1
    snapshot = health.get_tracker().napcat_send_snapshot()
    assert snapshot['state'] == 'unconfirmed'
    assert snapshot['last_success_at'] == before


def test_inbound_summary_does_not_label_unknown_as_success():
    from src.platforms.napcat_listener import NapCatInboundListener
    service = object.__new__(NapCatInboundListener)
    lines = []
    service._emit = lambda line, **kwargs: lines.append((line, kwargs))
    job = SimpleNamespace(group_id='100')
    result = SimpleNamespace(outcome='unconfirmed',success_routes=0,matched_routes=1,media_sent=0)
    operation = SimpleNamespace(completed=False,delivery=result,post=None)
    service._emit_job_summary(job,'request',operation,35)
    assert '送达未确认' in lines[0][0]
    assert '成功' not in lines[0][0]
    assert lines[0][1]['is_warning']


@pytest.mark.asyncio
async def test_ordinary_read_timeout_is_one_post_and_legacy_false(monkeypatch):
    client = _ScriptedClient([httpx.ReadTimeout("reply lost")])
    monkeypatch.setattr(napcat.cfg, "QQ_BOT_API", "http://gateway:3000")
    monkeypatch.setattr(napcat.cfg, "QQ_SEND_INTERVAL", 0)
    napcat.initialize(client)

    detailed = await napcat.send_qq_message_detailed(
        100, [{"type": "text", "data": {"text": "hello"}}], max_retries=3
    )
    assert detailed.error_code == "delivery_unconfirmed"
    assert detailed.ok is True  # accepted projection retained for detailed callers
    assert client.calls == 1
    assert napcat.get_send_gate_snapshot()["last_error"] == "delivery_unconfirmed"

    client = _ScriptedClient([httpx.ReadTimeout("reply lost")])
    napcat.initialize(client)
    legacy = await napcat.send_qq_message(
        100, [{"type": "text", "data": {"text": "hello"}}], max_retries=3
    )
    assert legacy is False
    assert client.calls == 1
    assert health.get_tracker().napcat_send_snapshot()["state"] == "unconfirmed"


@pytest.mark.asyncio
async def test_read_timeout_in_split_message_halts_remaining_batches(monkeypatch):
    client = _ScriptedClient([_OkResponse(), httpx.ReadTimeout("video reply lost")])
    monkeypatch.setattr(napcat.cfg, "QQ_BOT_API", "http://gateway:3000")
    monkeypatch.setattr(napcat.cfg, "QQ_SEND_INTERVAL", 0)
    napcat.initialize(client)
    progress = napcat.NapCatDeliveryProgress()

    with napcat.track_delivery_progress(progress):
        outcome = await napcat.send_qq_message_detailed(
            100,
            [
                {"type": "text", "data": {"text": "caption"}},
                {"type": "video", "data": {"file": "video-1"}},
                {"type": "record", "data": {"file": "audio-2"}},
            ],
            max_retries=3,
        )
    assert outcome.error_code == "delivery_unconfirmed"
    assert client.calls == 2
    assert progress.confirmed_batches == 1


@pytest.mark.asyncio
async def test_direct_adapter_propagates_ordinary_unknown_receipt(monkeypatch):
    client = _ScriptedClient([httpx.ReadTimeout("reply lost")])
    monkeypatch.setattr(napcat.cfg, "QQ_BOT_API", "http://gateway:3000")
    monkeypatch.setattr(napcat.cfg, "QQ_SEND_INTERVAL", 0)
    napcat.initialize(client)

    with pytest.raises(SocialDeliveryUnconfirmed):
        await NapCatAdapter().send_text(_napcat_target(), "hello")
    assert client.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("read"),
        httpx.WriteTimeout("write timeout"),
        httpx.ReadError("read lost"),
        httpx.WriteError("write lost"),
        httpx.RemoteProtocolError("connection closed"),
    ],
)
async def test_ambiguous_transport_error_is_not_retried(monkeypatch, error):
    client = _ScriptedClient([error])
    monkeypatch.setattr(napcat.cfg, "QQ_BOT_API", "http://gateway:3000")
    monkeypatch.setattr(napcat.cfg, "QQ_SEND_INTERVAL", 0)
    napcat.initialize(client)

    outcome = await napcat.send_qq_message_detailed(
        100, [{"type": "text", "data": {"text": "hello"}}], max_retries=3
    )
    assert outcome.error_code == "delivery_unconfirmed"
    assert client.calls == 1
