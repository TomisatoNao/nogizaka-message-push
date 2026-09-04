import asyncio
from dataclasses import FrozenInstanceError
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config.config as cfg
from config.config import CycleSnapshot, _mutate_container, get_cycle_snapshot
from src import fetcher
from src.app_modules import message_worker


def test_mutate_container_dict_non_destructive():
    old = {'a': 1, 'b': 2}
    old_id = id(old)
    new = {'b': 20, 'c': 30}

    _mutate_container(old, new)
    assert id(old) == old_id
    assert old == {'b': 20, 'c': 30}


def test_mutate_container_dict_concurrency_no_empty_window():
    container = {'key_1': 'val_1', 'key_2': 'val_2'}
    saw_empty = False
    stop_event = threading.Event()

    def reader():
        nonlocal saw_empty
        while not stop_event.is_set():
            if len(container) == 0:
                saw_empty = True
                break

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()

    for i in range(500):
        new_dict = {f'k_{i}': i, 'common': i}
        _mutate_container(container, new_dict)

    stop_event.set()
    for t in threads:
        t.join()

    assert not saw_empty, '读线程观测到了瞬时空字典窗口！'


def test_mutate_container_list_and_set():
    old_list = [1, 2, 3]
    list_id = id(old_list)
    _mutate_container(old_list, [4, 5])
    assert id(old_list) == list_id
    assert old_list == [4, 5]

    old_set = {'a', 'b'}
    set_id = id(old_set)
    _mutate_container(old_set,
{'b', 'c'})
    assert id(old_set) == set_id
    assert old_set == {'b', 'c'}


def test_get_cycle_snapshot_immutability():
    from collections.abc import Mapping
    snapshot = get_cycle_snapshot()
    assert isinstance(snapshot, CycleSnapshot)
    assert isinstance(snapshot.monitor_list, tuple)
    assert isinstance(snapshot.accounts, Mapping)
    assert isinstance(snapshot.skip_publish_types, tuple)

    # 1. 顶层字段 frozen 保护
    with pytest.raises(FrozenInstanceError):
        snapshot.backtrack_hours = 99  # type: ignore[misc]

    # 2. 嵌套字典深度只读保护（避免直接修改内部映射）
    if snapshot.accounts:
        first_key = next(iter(snapshot.accounts.keys()))
        with pytest.raises(TypeError):
            snapshot.accounts[first_key]["api_base"] = "malicious_mutation"  # type: ignore[index]
    if snapshot.monitor_list:
        with pytest.raises(TypeError):
            snapshot.monitor_list[0]["account_id"] = "malicious_mutation"  # type: ignore[index]

    # 3. 隔离副本：全局修改不污染已生成的快照
    orig_accounts = snapshot.accounts
    with patch.dict(cfg.ACCOUNTS, {'temp_test_acc': {'dummy': 123}}, clear=False):
        new_snap = get_cycle_snapshot()
        assert 'temp_test_acc' in new_snap.accounts
        assert 'temp_test_acc' not in orig_accounts


@pytest.mark.asyncio
async def test_fetcher_honors_explicit_snapshot_params():
    custom_account = {
        'auth_method': 'mobile',
        'group_type': 'nogizaka46',
        'api_base': 'https://custom-test-api.example.com',
    }
    member = {
        'account_id': 'test_account',
        'group_type': 'nogizaka46',
        'm_id': '12345',
        'm_name': '测试成员',
    }

    from unittest.mock import MagicMock
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        'messages': [
            {
                'id': 'msg_1',
                'updated_at': '2026-09-02T00:00:00Z',
                'publish_type': 'filtered_by_snapshot',
            },
            {
                'id': 'msg_2',
                'updated_at': '2026-09-02T01:00:00Z',
                'publish_type': 'normal',
            },
        ]
    }
    mock_resp.text = '{"messages": []}'
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch.dict(fetcher.ACCOUNT_CREDS, {'test_account': {'token': 'test_token'}}), \
         patch('src.fetcher.is_account_fetch_available', return_value=(True, '')), \
         patch('src.archive.get_timeline_watermark', return_value='2026-09-01T00:00:00Z'), \
         patch('src.member_directory.is_member_active_subscription', return_value=True), \
         patch.object(fetcher, '_semaphore', asyncio.Semaphore(1)), \
         patch.object(fetcher, '_http_client', mock_client):

        res = await fetcher.fetch_member_messages(
            member,
            account_cfg=custom_account,
            skip_publish_types=('filtered_by_snapshot',),
        )

        assert res is not None
        new_msgs, *_ = res
        assert len(new_msgs) == 1
        assert new_msgs[0]['id'] == 'msg_2'
        assert mock_client.get.called
        call_url = mock_client.get.call_args[0][0]
        assert 'https://custom-test-api.example.com' in call_url


@pytest.mark.asyncio
async def test_message_worker_run_cycle_passes_snapshot(monkeypatch):
    test_snapshot = CycleSnapshot(
        monitor_list=({'account_id': 'acc1', 'm_id': '101', 'm_name': 'MemberA'},),
        accounts={'acc1': {'api_base': 'https://snapshot-api.test'}},
        backtrack_hours=12,
        skip_publish_types=('skip_me',),
        day_interval=(10, 20),
        night_interval=(30, 60),
        day_start_hour=8,
        night_start_hour=23,
        sleep_start_hour=2,
        sleep_end_hour=6,
        message_monitor_enabled=True,
    )

    monkeypatch.setattr(cfg, 'get_cycle_snapshot', lambda: test_snapshot)
    monkeypatch.setattr(message_worker, '_is_message_monitor_enabled', lambda: True)
    mock_proactive = AsyncMock()
    monkeypatch.setattr(message_worker, 'proactive_refresh_if_expiring', mock_proactive)

    passed_kwargs = {}

    async def fake_fetch(member, **kwargs):
        passed_kwargs.update(kwargs)
        return None

    monkeypatch.setattr(fetcher, 'fetch_member_messages_', fake_fetch, raising=False)
    monkeypatch.setattr(fetcher, 'fetch_member_messages', fake_fetch)

    await message_worker._run_cycle()

    # 1. 验证抓取链路接收快照配置
    assert passed_kwargs.get('account_cfg') == {'api_base': 'https://snapshot-api.test'}
    assert passed_kwargs.get('backtrack_hours') == 12
    assert passed_kwargs.get('skip_publish_types') == ('skip_me',)

    # 2. 验证主动续期链路接收同一份快照账号配置
    assert mock_proactive.called
    proactive_kwargs = mock_proactive.call_args[1]
    assert proactive_kwargs.get('account_cfg') == {'api_base': 'https://snapshot-api.test'}


@pytest.mark.asyncio
async def test_401_retry_uses_snapshot_account_cfg_consistently(monkeypatch):
    """验证遇到 401 时的续期重试路径同样强制使用快照传入的配置，即使全局配置已变动。"""
    snapshot_account = {
        'auth_method': 'web',
        'group_type': 'nogizaka46',
        'api_base': 'https://snapshot-web-api.example.com',
        'app_tag': 'snap_tag',
        'web_origin': 'https://snapshot-web-api.example.com',
    }
    member = {
        'account_id': 'renew_account',
        'group_type': 'nogizaka46',
        'm_id': '999',
        'm_name': '续期测试成员',
        'target_groups': [12345],
    }

    # 模拟全局配置已被热重载为另一套完全不同的配置（如账号已被删除或变更为新地址）
    monkeypatch.setattr(cfg, 'ACCOUNTS', {'renew_account': {'api_base': 'https://MODIFIED-GLOBAL-API.com'}})

    mock_client = AsyncMock()
    # 第一次返回 401，第二次返回 200
    resp_401 = MagicMock()
    resp_401.status_code = 401
    resp_401.text = '{"error": "unauthorized"}'

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {'messages': []}
    resp_200.text = '{"messages": []}'

    mock_client.get.side_effect = [resp_401, resp_200]

    renewal_received_account_cfg = None

    async def fake_refresh_token(account_id, target_group, old_token=None, account_cfg=None):
        nonlocal renewal_received_account_cfg
        renewal_received_account_cfg = account_cfg
        return True

    monkeypatch.setattr('src.fetcher.refresh_token', fake_refresh_token)

    with patch.dict(fetcher.ACCOUNT_CREDS, {'renew_account': {'token': 'old_tok', 'cookies': {'sess': 'abc'}}}), \
         patch('src.fetcher.is_account_fetch_available', return_value=(True, '')), \
         patch('src.archive.get_timeline_watermark', return_value='2026-09-01T00:00:00Z'), \
         patch('src.member_directory.is_member_active_subscription', return_value=True), \
         patch.object(fetcher, '_semaphore', asyncio.Semaphore(1)), \
         patch.object(fetcher, '_http_client', mock_client):

        res = await fetcher.fetch_member_messages(
            member,
            account_cfg=snapshot_account,
        )

        assert res is not None
        # 核心断言：401 续期收到的配置必须是快照中的 snapshot_account，而不是被修改的全局配置
        assert renewal_received_account_cfg == snapshot_account
        assert renewal_received_account_cfg.get('api_base') == 'https://snapshot-web-api.example.com'

