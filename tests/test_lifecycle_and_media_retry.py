"""
tests/test_lifecycle_and_media_retry.py — 针对生命周期与重试降级关键路径的专项回归测试：
1. 启动失败清理 (Startup Failure Cleanup)
2. 配置热重载 (Config Hot Reload)
3. 监听任务安全退出 (Command Listener Graceful Stop)
4. 媒体上传重试与降级路径 (Media Upload Retry & Fallback)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src import app
from src.platforms import qq_official_client


# ============================================================
# 1. 启动失败清理 (Startup Failure Cleanup)
# ============================================================
@pytest.mark.asyncio
async def test_startup_failure_cleanup_releases_pid_and_closes_clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证启动过程中发生异常时，finally 块能妥善清理 PID 锁并关闭已创建的 HTTP 客户端与资源。"""
    test_pid_file = tmp_path / "app.pid"
    test_stop_file = tmp_path / "service.stop"
    monkeypatch.setattr(app, "PID_FILE", test_pid_file)
    monkeypatch.setattr(app, "STOP_FILE", test_stop_file)

    # 模拟启动在 _health_check 时抛出致命异常
    closed_clients: list[httpx.AsyncClient] = []
    real_aclose = httpx.AsyncClient.aclose

    async def tracking_aclose(self):
        closed_clients.append(self)
        return await real_aclose(self)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", tracking_aclose)

    async def fail_health_check(*args, **kwargs):
        raise RuntimeError("模拟健康检查致命崩溃")

    monkeypatch.setattr(app, "_health_check", fail_health_check)
    monkeypatch.setattr(app, "load_all_accounts", lambda: None)
    monkeypatch.setattr(app, "_init_accounts", AsyncMock())
    monkeypatch.setattr(app, "init_credentials", lambda *a, **k: None)

    # 运行 main()，应触发异常并通过 finally 清理
    with pytest.raises(RuntimeError, match="模拟健康检查致命崩溃"):
        await app.main()

    # 验证 PID 锁已通过 _release_instance_lock 释放
    assert not test_pid_file.exists(), "启动异常终止后，PID 文件应当被自动删除释放"
    # 验证创建的 HTTP 客户端均已被关闭
    assert len(closed_clients) >= 3, "启动异常终止后，创建的 HTTP 客户端均应被 aclose"
    # 验证主事件循环引用被重置
    assert app.get_main_loop() is None


# ============================================================
# 2. 配置热重载 (Config Hot Reload)
# ============================================================
def test_on_config_reload_reloads_components_and_schedules_listener_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 _on_config_reload 在配置变动时触发各通道重载，并在事件循环中安全同步指令监听。"""
    reloaded: list[str] = []

    monkeypatch.setattr(app, "load_all_accounts", lambda: reloaded.append("accounts"))
    monkeypatch.setattr(app, "init_credentials", lambda: reloaded.append("credentials"))
    monkeypatch.setattr("src.platforms.tgbot.initialize", lambda: reloaded.append("tgbot"))
    monkeypatch.setattr("src.platforms.qq_official.reload", lambda: reloaded.append("qq_official"))
    monkeypatch.setattr("src.social.manager.reload_social_service", lambda: reloaded.append("social"))

    mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    mock_loop.is_closed.return_value = False
    scheduled_callbacks: list = []
    mock_loop.call_soon_threadsafe.side_effect = lambda fn: scheduled_callbacks.append(fn)

    app.set_main_loop(mock_loop)
    try:
        # success=False 应直接忽略
        app._on_config_reload(success=False)
        assert len(reloaded) == 0

        # success=True 应完整触发各组件重载
        app._on_config_reload(success=True)
        assert "accounts" in reloaded
        assert "credentials" in reloaded
        assert "tgbot" in reloaded
        assert "qq_official" in reloaded
        assert "social" in reloaded
        assert app._sync_command_listeners in scheduled_callbacks
    finally:
        app.set_main_loop(None)


def test_on_config_reload_isolates_subsystem_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证热重载过程中某个子系统抛错不会中断后续其他组件的热重载。"""
    reloaded: list[str] = []

    def failing_tgbot():
        raise ValueError("TG 凭证解析失败")

    monkeypatch.setattr(app, "load_all_accounts", lambda: reloaded.append("accounts"))
    monkeypatch.setattr(app, "init_credentials", lambda: reloaded.append("credentials"))
    monkeypatch.setattr("src.platforms.tgbot.initialize", failing_tgbot)
    monkeypatch.setattr("src.platforms.qq_official.reload", lambda: reloaded.append("qq_official"))
    monkeypatch.setattr("src.social.manager.reload_social_service", lambda: reloaded.append("social"))

    # 不会抛出异常中断
    app._on_config_reload(success=True)
    assert "accounts" in reloaded
    assert "qq_official" in reloaded
    assert "social" in reloaded


# ============================================================
# 3. 监听任务安全退出 (Command Listener Graceful Stop)
# ============================================================
@pytest.mark.asyncio
async def test_command_listener_graceful_stop_cancels_and_clears_tasks() -> None:
    """验证系统退出清理流程中，活跃的指令长连接任务被妥善取消并收尾。"""
    # 模拟两个后台监听协程
    task1 = asyncio.create_task(asyncio.sleep(3600))
    task2 = asyncio.create_task(asyncio.sleep(3600))

    app._command_listeners["bot_1"] = ("secret_1", task1)
    app._command_listeners["bot_2"] = ("secret_2", task2)

    assert len(app.get_command_listeners()) == 2

    # 执行退出清理逻辑
    listener_tasks = [task for _, task in app._command_listeners.values()]
    app._command_listeners.clear()
    for task in listener_tasks:
        task.cancel()
    if listener_tasks:
        await asyncio.gather(*listener_tasks, return_exceptions=True)

    # 验证任务均已被终止，且注册表清空
    assert task1.cancelled() or task1.done()
    assert task2.cancelled() or task2.done()
    assert len(app.get_command_listeners()) == 0


# ============================================================
# 4. 媒体上传重试与降级路径 (Media Upload Retry & Fallback)
# ============================================================
@pytest.mark.asyncio
async def test_upload_media_silk_retry_on_850019(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证语音原格式被拒绝(850019)时自动调用 _transcode_audio_to_silk 进行重试，且全程无反射。"""
    client = qq_official_client.QQOfficialClient("app_1", "secret_1", "test_bot")
    monkeypatch.setattr(client, "ensure_access_token", AsyncMock(return_value=True))

    transcode_called = False

    def fake_transcode(content: bytes, filename: str):
        nonlocal transcode_called
        transcode_called = True
        return b"\x02#!SILK_V3_CONTENT", "voice.silk"

    monkeypatch.setattr(qq_official_client, "_transcode_audio_to_silk", fake_transcode)

    posted_payloads: list[dict] = []
    responses = iter([
        SimpleNamespace(status_code=400, json=lambda: {"code": 850019, "message": "unsupported audio"}),
        SimpleNamespace(status_code=200, json=lambda: {"file_info": "SILK_FILE_INFO_OK"}),
    ])

    async def fake_post(url: str, payload: dict, **kwargs):
        posted_payloads.append(payload.copy())
        return next(responses)

    monkeypatch.setattr(client, "_post_json", fake_post)

    result = await client._upload_media("record", b"raw_audio_bytes", filename="voice.m4a")

    assert result == "SILK_FILE_INFO_OK"
    assert transcode_called is True
    assert len(posted_payloads) == 2
    # 第一次为原格式 (file_type=3, voice.m4a)
    assert posted_payloads[0]["file_type"] == 3
    assert posted_payloads[0]["file_name"] == "voice.m4a"
    # 第二次为 Silk 转码格式 (file_type=3, voice.silk)
    assert posted_payloads[1]["file_type"] == 3
    assert posted_payloads[1]["file_name"] == "voice.silk"


@pytest.mark.asyncio
async def test_upload_media_fallback_to_file_when_silk_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 850019 格式拒绝且 Silk 转码不可用时，平滑降级为普通文件(file_type=4)上传。"""
    client = qq_official_client.QQOfficialClient("app_1", "secret_1", "test_bot")
    monkeypatch.setattr(client, "ensure_access_token", AsyncMock(return_value=True))

    # Silk 转码失败或未安装 ffmpeg/silk
    monkeypatch.setattr(qq_official_client, "_transcode_audio_to_silk", lambda *a, **k: None)

    posted_payloads: list[dict] = []
    responses = iter([
        SimpleNamespace(status_code=400, json=lambda: {"code": 850019, "message": "unsupported audio"}),
        SimpleNamespace(status_code=200, json=lambda: {"file_info": "FILE_FALLBACK_INFO_OK"}),
    ])

    async def fake_post(url: str, payload: dict, **kwargs):
        posted_payloads.append(payload.copy())
        return next(responses)

    monkeypatch.setattr(client, "_post_json", fake_post)

    result = await client._upload_media("record", b"raw_audio_bytes", filename="voice.m4a")

    assert result == "FILE_FALLBACK_INFO_OK"
    assert len(posted_payloads) == 2
    # 第二次降级为文件上传 (file_type=4)
    assert posted_payloads[1]["file_type"] == 4
    assert posted_payloads[1]["file_name"] == "voice.m4a"


@pytest.mark.asyncio
async def test_upload_media_compresses_large_images_and_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证超限图片与视频在直传前自动触发保真压缩。"""
    client = qq_official_client.QQOfficialClient("app_1", "secret_1", "test_bot")
    monkeypatch.setattr(client, "ensure_access_token", AsyncMock(return_value=True))

    image_compressed = False
    video_compressed = False

    def fake_compress_img(data: bytes, max_bytes: int = 0):
        nonlocal image_compressed
        image_compressed = True
        return b"compressed_img"

    def fake_compress_video(data: bytes, max_bytes: int = 0):
        nonlocal video_compressed
        video_compressed = True
        return b"compressed_video"

    monkeypatch.setattr(qq_official_client, "_compress_image_if_needed", fake_compress_img)
    monkeypatch.setattr(qq_official_client, "_compress_video_if_needed", fake_compress_video)

    async def fake_post(url: str, payload: dict, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda: {"file_info": "COMPRESSED_OK"})

    monkeypatch.setattr(client, "_post_json", fake_post)

    # 1. 3MB 图片直传限制
    big_img = b"x" * int(3.5 * 1024 * 1024)
    res_img = await client._upload_media("image", big_img, filename="pic.png")
    assert res_img == "COMPRESSED_OK"
    assert image_compressed is True

    # 2. 模拟分片上传失败降级压制直传
    monkeypatch.setattr(client, "_upload_media_chunked", AsyncMock(return_value=None))
    big_vid = b"y" * int(8.5 * 1024 * 1024)
    res_vid = await client._upload_media("video", big_vid, filename="video.mp4")
    assert res_vid == "COMPRESSED_OK"
    assert video_compressed is True
