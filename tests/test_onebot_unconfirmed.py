"""Unknown forward receipts are not success and never automatically resent."""
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
