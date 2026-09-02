"""QQ 官方 Bot 媒体元数据、类型识别与上传回归测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.platforms import qq_official
from src import archive


def test_safe_media_filename_uses_url_basename_and_blocks_path_syntax() -> None:
    name = qq_official._safe_media_filename(
        "https://cdn.example/a/测试视频.mp4?Expires=123",
        "video",
    )
    assert name == "测试视频.mp4"

    fallback = qq_official._safe_media_filename("../../secret", "record")
    assert fallback == "secret.m4a"
    assert "/" not in fallback and "\\" not in fallback


def test_m4a_ftyp_is_not_reclassified_as_video() -> None:
    # M4A/AAC 使用 MP4 容器头，必须尊重 record 声明和音频扩展名。
    m4a_header = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00"
    assert qq_official._resolve_media_type("record", "voice.m4a", m4a_header) == "record"
    assert qq_official._resolve_media_type("file", "voice.m4a", m4a_header) == "record"
    assert qq_official._resolve_media_type("file", "movie.mp4", m4a_header) == "video"


def test_media_payload_keeps_legacy_two_item_iteration() -> None:
    payload = qq_official.MediaPayload("video", b"data", "clip.mp4")
    assert tuple(payload) == ("video", b"data")
    assert payload[0] == "video"
    assert payload[1] == b"data"
    assert payload.filename == "clip.mp4"


@pytest.mark.asyncio
async def test_download_media_payload_preserves_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_download(*args, **kwargs):
        return b"m4a-bytes"

    monkeypatch.setattr(qq_official, "_download_media", fake_download)
    monkeypatch.setattr(
        "config.credentials.get_source_headers_for_account",
        lambda *_args, **_kwargs: {},
    )

    chain = [{
        "type": "record",
        "data": {"file": "https://cdn.example/voice/20260902_0823.m4a?sig=1"},
    }]
    payloads = await qq_official.download_media_payloads({}, chain)

    assert len(payloads) == 1
    assert payloads[0].media_type == "record"
    assert payloads[0].content == b"m4a-bytes"
    assert payloads[0].filename == "20260902_0823.m4a"


@pytest.mark.asyncio
async def test_upload_payload_contains_video_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = qq_official.QQOfficialBot("test", "app", "secret", "user")
    monkeypatch.setattr(bot, "ensure_access_token", _async_true)
    calls: list[tuple[str, dict]] = []

    async def fake_post(url: str, payload: dict, **kwargs):
        calls.append((url, payload))
        return SimpleNamespace(status_code=200, json=lambda: {"file_info": "FI_VIDEO"})

    monkeypatch.setattr(bot, "_post_json", fake_post)
    ok = await bot._upload_media("video", b"\x00\x00\x00\x18ftypisom", filename="clip.mp4")

    assert ok == "FI_VIDEO"
    assert calls[0][1]["file_type"] == 2
    assert calls[0][1]["file_name"] == "clip.mp4"


@pytest.mark.asyncio
async def test_upload_record_prefers_original_audio_without_transcoding(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = qq_official.QQOfficialBot("test", "app", "secret", "user")
    monkeypatch.setattr(bot, "ensure_access_token", _async_true)
    transcoded = False

    def unexpected_transcode(_content: bytes, _filename: str):
        nonlocal transcoded
        transcoded = True
        return b"\x02#!SILK_V3", "voice.amr"

    monkeypatch.setattr(
        qq_official,
        "_transcode_audio_to_silk",
        unexpected_transcode,
    )
    calls: list[dict] = []

    async def fake_post(url: str, payload: dict, **kwargs):
        calls.append(payload)
        return SimpleNamespace(status_code=200, json=lambda: {"file_info": "FI_VOICE"})

    monkeypatch.setattr(bot, "_post_json", fake_post)
    ok = await bot._upload_media("record", b"m4a-bytes", filename="voice.m4a")

    assert ok == "FI_VOICE"
    assert transcoded is False
    assert calls[0]["file_type"] == 3
    assert calls[0]["file_name"] == "voice.m4a"


@pytest.mark.asyncio
async def test_upload_record_transcodes_only_after_format_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = qq_official.QQOfficialBot("test", "app", "secret", "user")
    monkeypatch.setattr(bot, "ensure_access_token", _async_true)
    monkeypatch.setattr(
        qq_official,
        "_transcode_audio_to_silk",
        lambda _content, _filename: (b"\x02#!SILK_V3", "voice.amr"),
    )
    calls: list[dict] = []
    responses = iter([
        SimpleNamespace(status_code=400, json=lambda: {"code": 850019, "message": "unsupported audio"}),
        SimpleNamespace(status_code=200, json=lambda: {"file_info": "FI_VOICE_SILK"}),
    ])

    async def fake_post(url: str, payload: dict, **kwargs):
        calls.append(payload.copy())
        return next(responses)

    monkeypatch.setattr(bot, "_post_json", fake_post)
    ok = await bot._upload_media("record", b"m4a-bytes", filename="voice.m4a")

    assert ok == "FI_VOICE_SILK"
    assert len(calls) == 2
    assert calls[0]["file_type"] == 3
    assert calls[0]["file_name"] == "voice.m4a"
    assert calls[1]["file_type"] == 3
    assert calls[1]["file_name"] == "voice.amr"


@pytest.mark.asyncio
async def test_upload_record_keeps_named_file_when_silk_fallback_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = qq_official.QQOfficialBot("test", "app", "secret", "user")
    monkeypatch.setattr(bot, "ensure_access_token", _async_true)
    monkeypatch.setattr(qq_official, "_transcode_audio_to_silk", lambda _content, _filename: None)
    calls: list[dict] = []
    responses = iter([
        SimpleNamespace(status_code=400, json=lambda: {"code": 850019, "message": "unsupported audio"}),
        SimpleNamespace(status_code=200, json=lambda: {"file_info": "FI_FILE"}),
    ])

    async def fake_post(url: str, payload: dict, **kwargs):
        calls.append(payload.copy())
        return next(responses)

    monkeypatch.setattr(bot, "_post_json", fake_post)
    ok = await bot._upload_media("record", b"m4a-bytes", filename="voice.m4a")

    assert ok == "FI_FILE"
    assert len(calls) == 2
    assert calls[1]["file_type"] == 4
    assert calls[1]["file_name"] == "voice.m4a"


@pytest.mark.asyncio
async def test_send_chain_passes_media_metadata_to_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = qq_official.QQOfficialBot("test", "app", "secret", "user")
    monkeypatch.setattr(bot, "ensure_access_token", _async_true)
    uploaded: list[dict] = []

    async def fake_upload(media_type: str, content: bytes, **kwargs):
        uploaded.append({"media_type": media_type, "content": content, **kwargs})
        return "FI"

    async def fake_send(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(bot, "_upload_media", fake_upload)
    monkeypatch.setattr(bot, "_send_uploaded_media", fake_send)
    ok = await bot.send_message_chain(
        {"m_name": "测试"},
        [{"type": "record", "data": {"file": "https://x/voice.m4a"}}],
        [qq_official.MediaPayload("record", b"audio", "voice.m4a", "audio/mp4", "https://x/voice.m4a")],
    )

    assert ok is True
    assert uploaded[0]["filename"] == "voice.m4a"
    assert uploaded[0]["mime_type"] == "audio/mp4"


def test_archive_ftyp_sniff_respects_voice_declaration(tmp_path: Path) -> None:
    path = tmp_path / "voice.bin"
    path.write_bytes(b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00")
    assert archive._sniff_content_type(path, "voice") == "audio/mp4"
    assert archive._sniff_content_type(path, "video") == "video/mp4"


async def _async_true() -> bool:
    return True
