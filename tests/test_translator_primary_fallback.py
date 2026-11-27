"""验证主用付费 Gemini Key + 顶级模型优先序列与原 Key 自动回退机制。"""

from __future__ import annotations

import httpx
import pytest

from src import translator
from src.config import config as cfg


def _make_response(status_code: int, body: dict | str) -> httpx.Response:
    req = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/test:generateContent")
    if isinstance(body, dict):
        return httpx.Response(status_code, json=body, request=req)
    return httpx.Response(status_code, text=body, request=req)


@pytest.mark.asyncio
async def test_primary_paid_key_used_first_and_falls_back_to_default_on_403(monkeypatch):
    translator.clear_primary_key_cooldown()
    translator._model_cooldowns.clear()

    monkeypatch.setattr(cfg, "ENABLE_TRANSLATION", True, raising=False)
    monkeypatch.setattr(cfg, "GEMINI_MIN_INTERVAL", 0.0, raising=False)
    monkeypatch.setattr(cfg, "GEMINI_PRIMARY_API_KEY", "AQ.fake-paid-key", raising=False)
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "AIzaSy-original-key", raising=False)
    monkeypatch.setattr(cfg, "ZHIPU_API_KEY", "", raising=False)
    monkeypatch.setattr(
        cfg,
        "GEMINI_PRIMARY_MODELS",
        [
            {"name": "gemini-4-neon", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-neon:generateContent"},
            {"name": "gemini-4-carbon", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-carbon:generateContent"},
        ],
        raising=False,
    )
    monkeypatch.setattr(
        cfg,
        "GEMINI_MODELS",
        [
            {"name": "gemini-3.1-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"},
            {"name": "gemini-2.5-flash", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"},
        ],
        raising=False,
    )

    calls: list[tuple[str, str]] = []

    async def fake_post_json(url, payload, headers=None, custom_client=None, timeout=None):
        key = (headers or {}).get("x-goog-api-key", "")
        calls.append((url, key))
        if key == "AQ.fake-paid-key":
            return _make_response(200, {"candidates": [{"content": {"parts": [{"text": "付费旗舰译文"}]}}]})
        return _make_response(200, {"candidates": [{"content": {"parts": [{"text": "原Key兜底译文"}]}}]})

    monkeypatch.setattr(translator, "_post_json", fake_post_json)

    # 1. 正常情况：命中主用付费 Key + gemini-4-neon
    out1, model1 = await translator.translate_text_with_model("今日もありがとう！")
    assert out1 == "付费旗舰译文"
    assert model1 == "gemini-4-neon"
    assert calls == [
        ("https://generativelanguage.googleapis.com/v1beta/models/gemini-4-neon:generateContent", "AQ.fake-paid-key")
    ]

    # 2. 主用付费 Key 失效（HTTP 403）：同一轮立刻跳过其余主用模型，自动切回原 Key + 原模型优先级序列
    calls.clear()

    async def fake_post_json_403(url, payload, headers=None, custom_client=None, timeout=None):
        key = (headers or {}).get("x-goog-api-key", "")
        calls.append((url, key))
        if key == "AQ.fake-paid-key":
            return _make_response(403, {"error": {"code": 403, "message": "API_KEY_INVALID"}})
        return _make_response(200, {"candidates": [{"content": {"parts": [{"text": "原Key兜底译文"}]}}]})

    monkeypatch.setattr(translator, "_post_json", fake_post_json_403)
    out2, model2 = await translator.translate_text_with_model("おやすみなさい！")
    assert out2 == "原Key兜底译文"
    assert model2 == "gemini-3.1-flash-lite"
    assert translator.is_primary_key_cooling() is True
    # 不应再浪费请求调用 primary 的第二个模型 gemini-4-carbon
    assert [k for _, k in calls] == ["AQ.fake-paid-key", "AIzaSy-original-key"]
    assert "gemini-3.1-flash-lite" in calls[-1][0]

    # 3. 冷却期间后续请求直接走原 Key + 原优先级序列
    calls.clear()
    out3, model3 = await translator.translate_text_with_model("おはようございます！")
    assert out3 == "原Key兜底译文"
    assert model3 == "gemini-3.1-flash-lite"
    assert calls == [
        ("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent", "AIzaSy-original-key")
    ]
    translator.clear_primary_key_cooldown()


def test_no_primary_key_preserves_legacy_order(monkeypatch):
    translator.clear_primary_key_cooldown()
    monkeypatch.setattr(cfg, "GEMINI_PRIMARY_API_KEY", "", raising=False)
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "AIzaSy-original-key", raising=False)
    monkeypatch.setattr(cfg, "ZHIPU_API_KEY", "", raising=False)
    monkeypatch.setattr(
        cfg,
        "GEMINI_MODELS",
        [
            {"name": "gemini-3.1-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"},
        ],
        raising=False,
    )
    active = translator._get_active_models("message")
    assert [m["name"] for m in active] == ["gemini-3.1-flash-lite"]
    assert active[0]["api_key_tier"] == "default"
