"""语音消息 AI 识别与翻译（付费 Gemini 主层 -> 原 Key 自动回退 + 推送链路）单元测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx

import src.config.config as cfg
from src import fetcher, translator
from src.platforms.napcat import _split_video_record_chain, build_message_chain


def test_sniff_audio_mime_magic_and_fallback():
    assert translator.sniff_audio_mime(b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00") == "audio/mp4"
    assert translator.sniff_audio_mime(b"ID3\x03\x00\x00\x00\x00\x00\x00") == "audio/mpeg"
    assert translator.sniff_audio_mime(b"OggS\x00\x02\x00\x00") == "audio/ogg"
    assert translator.sniff_audio_mime(b"RIFF\x24\x00\x00\x00WAVEfmt ") == "audio/wav"
    assert translator.sniff_audio_mime(b"\xff\xf1\x50\x80\x00\x1f\xfc") == "audio/aac"
    assert translator.sniff_audio_mime(b"unknownbytes", declared_mime="audio/x-m4a") == "audio/mp4"
    assert translator.sniff_audio_mime(b"unknownbytes", filename="voice.mp3?sig=1") == "audio/mpeg"


def test_parse_voice_json_response():
    ja, zh = translator._parse_voice_json_response(
        '```json\n{"transcript": "おはよう〜！", "translation": "早安～！"}\n```'
    )
    assert ja == "おはよう〜！"
    assert zh == "早安～！"

    ja2, zh2 = translator._parse_voice_json_response('{"transcript": "", "translation": ""}')
    assert ja2 == "" and zh2 == ""


def test_transcribe_audio_primary_success_and_cache(monkeypatch):
    translator._voice_cache.clear()
    translator._model_cooldowns.clear()
    translator.clear_primary_key_cooldown()

    monkeypatch.setattr(cfg, "GEMINI_PRIMARY_API_KEY", "AQ.PRIMARY_TEST", raising=False)
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "AIzaFALLBACK_TEST", raising=False)
    monkeypatch.setattr(cfg, "ZHIPU_API_KEY", "zhipu-key", raising=False)
    monkeypatch.setattr(
        cfg,
        "GEMINI_PRIMARY_MODELS",
        [{"name": "gemini-4-neon", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-neon:generateContent"}],
        raising=False,
    )
    monkeypatch.setattr(
        cfg,
        "GEMINI_MODELS",
        [
            {"name": "glm-4-flash", "provider": "zhipu", "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions"},
            {"name": "gemini-3.1-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"},
        ],
        raising=False,
    )
    monkeypatch.setattr(cfg, "GEMINI_MIN_INTERVAL", 0.0, raising=False)

    calls: list[tuple[str, str, str]] = []

    async def fake_post_json(url, payload, headers=None, custom_client=None, timeout=None):
        api_key = (headers or {}).get("x-goog-api-key", "")
        mime = payload["contents"][0]["parts"][0]["inlineData"]["mimeType"]
        calls.append((url, api_key, mime))
        body = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": json.dumps({"transcript": "こんばんは、なおなおです！", "translation": "晚上好，我是なおなお！"})}
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(translator, "_post_json", fake_post_json)

    sample = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00sample_audio"
    ja, zh, model = asyncio.run(
        translator.transcribe_and_translate_audio_with_model(
            sample, member_name="冨里奈央", group_type="nogizaka46"
        )
    )
    assert ja == "こんばんは、なおなおです！"
    assert zh == "晚上好，我是なおなお！"
    assert model == "gemini-4-neon"
    assert len(calls) == 1
    assert calls[0][1] == "AQ.PRIMARY_TEST"
    assert calls[0][2] == "audio/mp4"

    # 第二次调用命中内存缓存
    ja2, zh2, model2 = asyncio.run(
        translator.transcribe_and_translate_audio_with_model(
            sample, member_name="冨里奈央", group_type="nogizaka46"
        )
    )
    assert (ja2, zh2, model2) == (ja, zh, model)
    assert len(calls) == 1


def test_transcribe_audio_primary_401_fallback_to_default_gemini_skipping_zhipu(monkeypatch):
    translator._voice_cache.clear()
    translator._model_cooldowns.clear()
    translator.clear_primary_key_cooldown()

    monkeypatch.setattr(cfg, "GEMINI_PRIMARY_API_KEY", "AQ.EXPIRED", raising=False)
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "AIzaFALLBACK_OK", raising=False)
    monkeypatch.setattr(cfg, "ZHIPU_API_KEY", "zhipu-key", raising=False)
    monkeypatch.setattr(
        cfg,
        "GEMINI_PRIMARY_MODELS",
        [{"name": "gemini-4-neon", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-neon:generateContent"}],
        raising=False,
    )
    monkeypatch.setattr(
        cfg,
        "GEMINI_MODELS",
        [
            {"name": "glm-4-flash", "provider": "zhipu", "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions"},
            {"name": "gemini-3.1-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"},
        ],
        raising=False,
    )
    monkeypatch.setattr(cfg, "GEMINI_MIN_INTERVAL", 0.0, raising=False)

    called_urls: list[tuple[str, str]] = []

    async def fake_post_json(url, payload, headers=None, custom_client=None, timeout=None):
        api_key = (headers or {}).get("x-goog-api-key", "")
        called_urls.append((url, api_key))
        if api_key == "AQ.EXPIRED":
            return httpx.Response(401, text='{"error":{"status":"UNAUTHENTICATED"}}', request=httpx.Request("POST", url))
        body = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": json.dumps({"transcript": "ありがとう！", "translation": "谢谢！"})}
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(translator, "_post_json", fake_post_json)

    sample = b"ID3\x03\x00\x00\x00fallback_audio"
    ja, zh, model = asyncio.run(
        translator.transcribe_and_translate_audio_with_model(
            sample, member_name="冨里奈央", group_type="nogizaka46"
        )
    )
    assert ja == "ありがとう！"
    assert zh == "谢谢！"
    assert model == "gemini-3.1-flash-lite"
    assert translator.is_primary_key_cooling() is True
    # 不应调用智谱文本模型
    assert all("bigmodel.cn" not in u for u, _ in called_urls)
    assert len(called_urls) == 2


def test_build_message_chain_and_split_for_voice_transcript():
    msg = {
        "id": "v1",
        "type": "voice",
        "file": "https://example.com/voice.m4a",
        "text": "おはよう〜",
        "_voice_transcript": "おはよう〜",
    }
    chain = build_message_chain(
        "冨里奈央",
        "2026-11-27T12:00:00Z",
        msg,
        translated_text="早安～",
        model_name="gemini-4-neon",
    )
    assert chain[0]["type"] == "text"
    assert "🎙️ 语音原文：\nおはよう〜" in chain[0]["data"]["text"]
    assert chain[1]["type"] == "record"
    assert chain[1]["data"]["file"] == "https://example.com/voice.m4a"
    assert chain[2]["type"] == "text"
    assert "早安～" in chain[2]["data"]["text"]
    assert "gemini-4-neon" in chain[2]["data"]["text"]

    # NapCat 拆分后：第一批合并文本（含日语原文+中文译文），第二批发送原语音 record
    batches = _split_video_record_chain(chain)
    assert len(batches) == 2
    assert [seg["type"] for seg in batches[0]] == ["text", "text"]
    assert [seg["type"] for seg in batches[1]] == ["record"]


def test_fetcher_handle_voice_message_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "ENABLE_TRANSLATION", True, raising=False)
    monkeypatch.setattr(cfg, "ENABLE_VOICE_TRANSCRIPTION", True, raising=False)
    monkeypatch.setattr(cfg, "QQ_SEND_INTERVAL", 0.0, raising=False)
    monkeypatch.setenv("MEMBER_DELIVERY_INTENTS_ENABLED", "0")

    voice_file = tmp_path / "20261127_210000_v100.m4a"
    voice_file.write_bytes(b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00test_voice")

    archived_calls: list[tuple[dict, str]] = []

    async def fake_archive_message(member, msg, translated=""):
        archived_calls.append((dict(msg), translated))

    monkeypatch.setattr(fetcher.archive, "archive_message", fake_archive_message)
    monkeypatch.setattr(fetcher.archive, "resolve_local_media_path", lambda member, msg: voice_file)

    async def fake_transcribe(audio_bytes, mime_type="", member_name="", group_type="", filename="", custom_client=None):
        assert audio_bytes == voice_file.read_bytes()
        return "今日もありがとう！", "今天也谢谢你！", "gemini-4-neon"

    monkeypatch.setattr(fetcher, "transcribe_and_translate_audio_with_model", fake_transcribe)
    monkeypatch.setattr(fetcher, "successful_routes", lambda *a, **k: set())
    monkeypatch.setattr(fetcher, "mark_successful_routes", lambda *a, **k: None)
    monkeypatch.setattr(fetcher, "save_sent_id", lambda *a, **k: None)

    sent_chains: list[list[dict]] = []

    async def fake_send_detailed(member, chain, **kwargs):
        sent_chains.append(chain)
        return SimpleNamespace(
            attempts=[SimpleNamespace(route_id="napcat:1", ok=True, error_code="")],
            matched_route_ids=["napcat:1"],
            failure_count=0,
        )

    monkeypatch.setattr(fetcher, "send_member_message_detailed", fake_send_detailed)

    member = {"m_id": "37", "m_name": "冨里奈央", "group_type": "nogizaka46"}
    msg = {
        "id": "v100",
        "type": "voice",
        "text": "",
        "file": "https://example.com/v100.m4a",
        "updated_at": "2026-11-27T12:00:00Z",
    }
    l_time_ref = [""]
    ok = asyncio.run(fetcher._handle_message(member, msg, [], set(), l_time_ref))
    assert ok is True
    assert len(archived_calls) == 2
    assert archived_calls[1][0]["text"] == "今日もありがとう！"
    assert archived_calls[1][0]["_voice_transcript"] == "今日もありがとう！"
    assert archived_calls[1][1] == "今天也谢谢你！"

    assert len(sent_chains) == 1
    chain = sent_chains[0]
    assert "🎙️ 语音原文：\n今日もありがとう！" in chain[0]["data"]["text"]
    assert chain[1]["type"] == "record"
    assert "今天也谢谢你！" in chain[2]["data"]["text"]
