"""NapCat 冨里奈央 AI 拟人对话模块与 CPA 接入测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.platforms.napcat_chat import NapCatChatService
from src.platforms.napcat_listener import NapCatInboundListener


def _chat_config(**chat_overrides):
    return {
        "enable_napcat_qq": True,
        "napcat_routes": [{"group_id": 123456}],
        "napcat_inbound": {
            "enabled": True,
            "queue_size": 4,
            "workers": 1,
            "cooldown_seconds": 0,
        },
        "napcat_ai_chat": {
            "enabled": True,
            "cpa_base_url": "http://127.0.0.1:8317",
            "cpa_api_key": "test-key",
            "cpa_model": "luna",
            "require_at": True,
            "trigger_keywords": ["奈央", "奈央酱", "なおなお"],
            "cooldown_user_seconds": 2.0,
            "cooldown_group_seconds": 1.0,
            "max_context_turns": 4,
            "context_ttl_seconds": 30.0,
            **chat_overrides,
        },
    }


def test_extract_trigger_and_text():
    service = NapCatChatService(config_provider=lambda: _chat_config(require_at=True))

    # 1. OneBot segment @ 触发
    event_seg = {
        "message": [
            {"type": "at", "data": {"qq": "111222"}},
            {"type": "text", "data": {"text": "  今天开心吗？  "}},
        ],
        "raw_message": "[CQ:at,qq=111222] 今天开心吗？",
    }
    triggered, text = service.extract_trigger_and_text(event_seg, "", self_id="111222")
    assert triggered is True
    assert text == "今天开心吗？"

    # 2. CQ 码 @ 触发
    event_cq = {
        "message": "[CQ:at,qq=111222] 奈央酱吃草莓大福了吗？",
        "raw_message": "[CQ:at,qq=111222] 奈央酱吃草莓大福了吗？",
    }
    triggered, text = service.extract_trigger_and_text(event_cq, event_cq["raw_message"], self_id="111222")
    assert triggered is True
    assert text == "吃草莓大福了吗？"

    # 3. 关键词触发（require_at=False 时无需@）
    service_no_at = NapCatChatService(config_provider=lambda: _chat_config(require_at=False))
    event_kw = {
        "message": [{"type": "text", "data": {"text": "奈央 早上好呀！"}}],
        "raw_message": "奈央 早上好呀！",
    }
    triggered, text = service_no_at.extract_trigger_and_text(event_kw, event_kw["raw_message"], self_id="111222")
    assert triggered is True
    assert text == "早上好呀！"

    # 3.1 纯关键词但在 require_at=True 时不触发（严格要求@）
    triggered_strict, _ = service.extract_trigger_and_text(event_kw, event_kw["raw_message"], self_id="111222")
    assert triggered_strict is False

    # 3.2 @ 且携带关键词前缀时，剥离关键词保留有效提问
    event_at_kw = {
        "message": [
            {"type": "at", "data": {"qq": "111222"}},
            {"type": "text", "data": {"text": " 奈央 早上好呀！"}},
        ],
        "raw_message": "[CQ:at,qq=111222] 奈央 早上好呀！",
    }
    triggered_at_kw, text_at_kw = service.extract_trigger_and_text(event_at_kw, event_at_kw["raw_message"], self_id="111222")
    assert triggered_at_kw is True
    assert text_at_kw == "早上好呀！"

    # 4. 仅 @ 机器人，无提问文字（快捷打招呼）
    event_empty = {
        "message": [{"type": "at", "data": {"qq": "111222"}}],
        "raw_message": "[CQ:at,qq=111222]",
    }
    triggered, text = service.extract_trigger_and_text(event_empty, event_empty["raw_message"], self_id="111222")
    assert triggered is True
    assert text == ""

    # 5. 未提及且无关键词
    event_none = {
        "message": [{"type": "text", "data": {"text": "今天乃木坂有live吗？"}}],
        "raw_message": "今天乃木坂有live吗？",
    }
    triggered, text = service.extract_trigger_and_text(event_none, event_none["raw_message"], self_id="111222")
    assert triggered is False
    assert text == ""


def test_chat_rate_limiting():
    service = NapCatChatService(config_provider=lambda: _chat_config())

    # 首次不限流
    assert service.check_rate_limit("123456", "999") is False

    # 立即再次触发应被单人限流
    assert service.check_rate_limit("123456", "999") is True

    # 同群不同人应被单群限流 (group cooldown = 1.0s)
    assert service.check_rate_limit("123456", "888") is True


def test_context_sliding_window_and_ttl():
    service = NapCatChatService(config_provider=lambda: _chat_config(max_context_turns=4, context_ttl_seconds=1.0))

    # 初始上下文为空
    assert service.get_context("123456", "999") == []

    # 追加两轮（4条消息）
    service.append_context("123456", "999", "你好", "你好呀～")
    service.append_context("123456", "999", "今天吃了什么", "吃了甜甜圈！")

    ctx = service.get_context("123456", "999")
    assert len(ctx) == 4
    assert ctx[0]["content"] == "你好"

    # 追加第3轮，由于 max_context_turns=4，最早的应被截断
    service.append_context("123456", "999", "喜欢拍照吗", "最喜欢胶片机啦！")
    ctx2 = service.get_context("123456", "999")
    assert len(ctx2) == 4
    assert ctx2[0]["content"] == "今天吃了什么"
    assert ctx2[-1]["content"] == "最喜欢胶片机啦！"


@pytest.mark.asyncio
async def test_empty_query_fast_greeting():
    service = NapCatChatService(config_provider=lambda: _chat_config())
    reply = await service.call_cpa("123456", "999", "")
    assert "戳戳脸颊" in reply


@pytest.mark.asyncio
async def test_cpa_call_and_code_sanitizing():
    service = NapCatChatService(config_provider=lambda: _chat_config())

    mock_cpa_resp = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "```python\ndef foo(): pass\n``` 诶嘿嘿，奈央不写代码，奈央想吃草莓大福！",
                }
            }
        ]
    }

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: mock_cpa_resp
    mock_resp.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        reply = await service.call_cpa("123456", "999", "你能帮我写个脚本吗？")
        assert "```" not in reply
        assert "草莓大福" in reply


@pytest.mark.asyncio
async def test_listener_dispatches_to_chat_when_no_urls():
    sent_messages = []

    async def mock_sender(gid, chain):
        sent_messages.append((gid, chain))

    config = _chat_config()
    listener = NapCatInboundListener(
        config_provider=lambda: config,
        event_token="test-secret",
    )
    listener.chat_service._sender = mock_sender

    assert await listener.start() is True
    try:
        # 发送仅包含 @机器人 的打招呼事件（触发快速回帖）
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123456,
            "user_id": 987654,
            "self_id": 111222,
            "message_id": 1001,
            "message": [
                {"type": "at", "data": {"qq": "111222"}},
            ],
            "raw_message": "[CQ:at,qq=111222]",
        }

        result = listener.accept_event(event, source="test")
        assert result == "chat_queued"

        for _ in range(40):
            if sent_messages:
                break
            await asyncio.sleep(0.025)

        assert len(sent_messages) == 1
        gid, chain = sent_messages[0]
        assert gid == 123456
        assert any("戳戳脸颊" in str(seg.get("data", {}).get("text", "")) for seg in chain)
    finally:
        await listener.stop()


def test_missing_credentials_disables_chat(monkeypatch):
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    # 缺少 API Key
    s1 = NapCatChatService(config_provider=lambda: _chat_config(cpa_api_key=""))
    assert s1.is_enabled() is False

    # 缺少 Base URL
    s2 = NapCatChatService(config_provider=lambda: _chat_config(cpa_base_url=""))
    assert s2.is_enabled() is False


def test_allowed_groups_filtering():
    service = NapCatChatService(config_provider=lambda: _chat_config(allowed_groups=[123456]))

    # 白名单内的群允许
    event_ok = {
        "message": [{"type": "at", "data": {"qq": "111222"}}],
        "raw_message": "[CQ:at,qq=111222] 你好",
        "group_id": 123456,
        "user_id": 999,
        "self_id": 111222,
    }
    accepted, reason = service.try_accept_event(event_ok)
    assert accepted is True
    assert reason == "queued"

    # 白名单外的群拒绝
    event_rejected = dict(event_ok, group_id=999999)
    accepted2, reason2 = service.try_accept_event(event_rejected)
    assert accepted2 is False
    assert reason2 == "group_not_allowed"


def test_message_id_deduplication():
    service = NapCatChatService(config_provider=lambda: _chat_config(cooldown_user_seconds=0, cooldown_group_seconds=0))
    event = {
        "message": [{"type": "at", "data": {"qq": "111222"}}, {"type": "text", "data": {"text": "你好"}}],
        "raw_message": "[CQ:at,qq=111222] 你好",
        "group_id": 123456,
        "user_id": 999,
        "self_id": 111222,
        "message_id": "msg_duplicate_test_1001",
    }
    # 第一次正常入队
    acc1, r1 = service.try_accept_event(event)
    assert acc1 is True
    assert r1 == "queued"

    # 相同 message_id 再次上报被拒绝
    acc2, r2 = service.try_accept_event(event)
    assert acc2 is False
    assert r2 == "duplicate"


def test_query_length_truncation():
    service = NapCatChatService(config_provider=lambda: _chat_config(max_query_length=50))
    event = {
        "message": [{"type": "at", "data": {"qq": "111222"}}, {"type": "text", "data": {"text": "A" * 200}}],
        "raw_message": "[CQ:at,qq=111222] " + "A" * 200,
        "group_id": 123456,
        "user_id": 999,
        "self_id": 111222,
        "message_id": "msg_len_1",
    }
    acc, r = service.try_accept_event(event)
    assert acc is True
    job = service._queue.get_nowait()
    assert len(job.text) == 50


@pytest.mark.asyncio
async def test_cpa_http_error_handling():
    service = NapCatChatService(config_provider=lambda: _chat_config())

    # 1. 模拟 401 鉴权失败
    mock_401 = AsyncMock()
    mock_401.status_code = 401
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_401
        res_401 = await service.call_cpa("123456", "999", "你好")
        assert "记忆小本本" in res_401

    # 2. 模拟 429 速率限制
    mock_429 = AsyncMock()
    mock_429.status_code = 429
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_429
        res_429 = await service.call_cpa("123456", "999", "你好")
        assert "休息两秒钟" in res_429

    # 3. 模拟 404 接口不存在
    mock_404 = AsyncMock()
    mock_404.status_code = 404
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_404
        res_404 = await service.call_cpa("123456", "999", "你好")
        assert "找不到回家的路" in res_404


@pytest.mark.asyncio
async def test_service_start_stop_cleans_queue():
    service = NapCatChatService(config_provider=lambda: _chat_config(workers=2))
    assert await service.start() is True
    assert service.running is True

    # 停止服务，确保任务队列清空
    await service.stop()
    assert service.running is False
    assert service._queue.empty() is True
