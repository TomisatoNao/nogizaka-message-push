"""
src/platforms/qq_official_media.py — 官方 QQ Bot 多媒体转码、压缩与载荷下载引擎
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess  # nosec B404 - controlled local ffmpeg invocation
import sys
import tempfile
import threading
from typing import Iterator
from urllib.parse import unquote, urlparse

import httpx

import config.config as cfg
from src.logger import log_all

_MEDIA_FILE_TYPES = {
    "image": 1,
    "video": 2,
    "record": 3,
    "file": 4,
}

_MEDIA_DEFAULT_EXTENSIONS = {
    "image": ".jpg",
    "video": ".mp4",
    "record": ".m4a",
    "file": ".bin",
}
_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi", ".flv", ".ts"})
_AUDIO_EXTENSIONS = frozenset({
    ".aac", ".amr", ".flac", ".m4a", ".m4b", ".m4p", ".mp3", ".oga",
    ".ogg", ".opus", ".silk", ".wav", ".wma",
})
_QQ_VOICE_EXTENSION = ".amr"  # QQ 客户端语音文件扩展名，内容为 SILK 编码


@dataclass(frozen=True)
class MediaPayload:
    """下载后交给 QQ Bot 的媒体载荷。

    ``download_media_payloads`` 旧版返回二元组 ``(type, bytes)``。保留
    ``__iter__``/``__getitem__`` 的二元行为，避免已有调用方立即失效，新增的
    文件名和来源信息通过属性传递给上传层。
    """

    media_type: str
    content: bytes | None
    filename: str = ""
    mime_type: str = ""
    source_url: str = ""

    def __iter__(self) -> Iterator[object]:
        yield self.media_type
        yield self.content

    def __getitem__(self, index: int) -> object:
        if index in (0, -2):
            return self.media_type
        if index in (1, -1):
            return self.content
        raise IndexError(index)

    def __len__(self) -> int:
        return 2


def _filename_candidate(value: str) -> str:
    """从 URL、路径或文件名中提取不含查询参数的 basename。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        raw = parsed.path
    raw = unquote(raw).replace("\\", "/")
    return raw.rsplit("/", 1)[-1]


def _safe_media_filename(filename: str, media_type: str, source_url: str = "") -> str:
    """生成可传给 QQ Bot 的安全文件名，并按类型补齐扩展名。"""
    candidate = _filename_candidate(filename) or _filename_candidate(source_url)
    candidate = re.sub(r"[\x00-\x1f\x7f]", "_", candidate)
    # 不允许把路径、查询语法或 Windows 保留字符带入上传元数据。
    candidate = re.sub(r'[<>:"/\\|?*]+', "_", candidate).strip(" .")
    if not candidate or candidate in {".", ".."}:
        candidate = ""

    if not candidate:
        stem = {"image": "image", "video": "video", "record": "audio", "file": "file"}.get(media_type, "media")
        candidate = stem

    candidate = candidate[:180].rstrip(" .") or "media"
    if not Path(candidate).suffix:
        candidate += _MEDIA_DEFAULT_EXTENSIONS.get(media_type, ".bin")
    return candidate


def _media_extension(filename: str) -> str:
    return Path(_filename_candidate(filename)).suffix.lower()


def _looks_like_silk(content: bytes) -> bool:
    return content.startswith((b"#!SILK", b"\x02#!SILK"))


def _resolve_media_type(media_type: str, filename: str, content: bytes | None, mime_type: str = "") -> str:
    """在保留业务声明的前提下识别媒体类型。"""
    declared = media_type if media_type in _MEDIA_FILE_TYPES else "file"
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    ext = _media_extension(filename)

    if mime.startswith("audio/") or ext in _AUDIO_EXTENSIONS or declared == "record":
        return "record"
    if mime.startswith("image/") or ext in _IMAGE_EXTENSIONS:
        return "image"
    if mime.startswith("video/") or ext in _VIDEO_EXTENSIONS:
        return "video"

    header = (content or b"")[:32]
    if header.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8")):
        return "image"
    if header.startswith(b"RIFF") and len(header) >= 12:
        if header[8:12] == b"WEBP":
            return "image"
        if header[8:12] == b"WAVE":
            return "record"
    if header.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"OggS", b"fLaC")):
        return "record"
    if _looks_like_silk(header):
        return "record"

    if len(header) >= 8 and header[4:8] == b"ftyp":
        return "video" if declared != "record" else "record"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video"
    return declared


_VOICE_CACHE: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_VOICE_CACHE_BYTES = 0
_VOICE_CACHE_LIMIT = 16 * 1024 * 1024
_VOICE_CACHE_LOCK = threading.RLock()


def _cache_voice_result(key: str, result: tuple[bytes, str]) -> None:
    """缓存小型语音转码结果，避免同一媒体向私聊/群聊重复转码。"""
    global _VOICE_CACHE_BYTES
    with _VOICE_CACHE_LOCK:
        size = len(result[0])
        if size > 4 * 1024 * 1024:
            return
        old = _VOICE_CACHE.pop(key, None)
        if old:
            _VOICE_CACHE_BYTES -= len(old[0])
        _VOICE_CACHE[key] = result
        _VOICE_CACHE_BYTES += size
        while _VOICE_CACHE and _VOICE_CACHE_BYTES > _VOICE_CACHE_LIMIT:
            _, removed = _VOICE_CACHE.popitem(last=False)
            _VOICE_CACHE_BYTES -= len(removed[0])


def _transcode_audio_to_silk(content: bytes, filename: str) -> tuple[bytes, str] | None:
    """把标准音频转为 QQ Bot 语音使用的 SILK；失败时返回 None。"""
    if not content:
        return None
    if _looks_like_silk(content):
        return content, _safe_media_filename(filename, "record").rsplit(".", 1)[0] + _QQ_VOICE_EXTENSION

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log_all("ℹ️ 官方 QQ Bot 音频未转码：未找到 ffmpeg，将按命名音频附件降级", is_debug=True)
        return None
    try:
        import pysilk  # type: ignore[import-not-found]
    except ImportError:
        log_all("ℹ️ 官方 QQ Bot 音频未转码：未安装 silk-python，将按命名音频附件降级", is_debug=True)
        return None

    key = hashlib.sha256(content).hexdigest()
    with _VOICE_CACHE_LOCK:
        cached = _VOICE_CACHE.get(key)
        if cached:
            _VOICE_CACHE.move_to_end(key)
            return cached

        return _transcode_audio_to_silk_uncached(content, filename, pysilk, ffmpeg, key)


def _transcode_audio_to_silk_uncached(
    content: bytes,
    filename: str,
    pysilk: object,
    ffmpeg: str,
    key: str,
) -> tuple[bytes, str] | None:
    """在缓存锁内执行一次音频转码。"""
    safe_name = _safe_media_filename(filename, "record")
    stem = Path(safe_name).stem or "audio"
    try:
        with tempfile.TemporaryDirectory(prefix="qq-voice-") as tmp_dir:
            src_path = os.path.join(tmp_dir, safe_name)
            pcm_path = os.path.join(tmp_dir, "audio.pcm")
            silk_path = os.path.join(tmp_dir, "audio.silk")
            with open(src_path, "wb") as src_file:
                src_file.write(content)

            result = subprocess.run(  # nosec B603 - executable resolved via PATH
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src_path,
                 "-vn", "-ar", "24000", "-ac", "1", "-f", "s16le", pcm_path],
                capture_output=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0 or not os.path.exists(pcm_path) or os.path.getsize(pcm_path) == 0:
                log_all(
                    f"ℹ️ 官方 QQ Bot 音频转码失败（ffmpeg exit={result.returncode}），将按命名附件降级",
                    is_debug=True,
                )
                return None

            with open(pcm_path, "rb") as pcm_file, open(silk_path, "wb") as silk_file:
                pysilk.encode(pcm_file, silk_file, 24000, 24000, tencent=True)  # type: ignore[attr-defined]
            if not os.path.exists(silk_path) or os.path.getsize(silk_path) == 0:
                log_all("ℹ️ 官方 QQ Bot 音频转码未生成有效 SILK，将按命名附件降级", is_debug=True)
                return None
            with open(silk_path, "rb") as silk_file:
                converted = silk_file.read()
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, TimeoutError) as ex:
        log_all(f"ℹ️ 官方 QQ Bot 音频转码异常 ({type(ex).__name__})，将按命名附件降级", is_debug=True)
        return None
    except Exception as ex:
        log_all(f"ℹ️ 官方 QQ Bot 音频编码器异常 ({type(ex).__name__})，将按命名附件降级", is_debug=True)
        return None

    result = (converted, f"{stem}{_QQ_VOICE_EXTENSION}")
    _cache_voice_result(key, result)
    return result


def _compress_video_if_needed(content: bytes, max_bytes: int = int(7.8 * 1024 * 1024)) -> bytes:
    """如果视频体积超过腾讯开放平台直传限制(8MB)，自动通过 ffmpeg 动态计算码率快速压制。"""
    if not content or len(content) <= max_bytes:
        return content
    ffmpeg = shutil.which("ffmpeg") or ""
    if not ffmpeg:
        return content

    ffprobe = shutil.which("ffprobe") or ""

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as in_f:
        in_f.write(content)
        in_f_path = in_f.name

    out_f_path = in_f_path + ".compressed.mp4"
    try:
        dur = 30.0
        if ffprobe:
            try:
                probe_cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", in_f_path]
                res = subprocess.check_output(probe_cmd, timeout=10).decode("utf-8").strip()  # nosec B603
                if res:
                    dur = max(1.0, float(res))
            except Exception:  # nosec B110
                pass

        target_bitrate_k = max(200, int((max_bytes * 0.85 * 8) / dur / 1000))
        video_bitrate_k = max(150, target_bitrate_k - 64)

        cmd = [
            ffmpeg, "-y", "-i", in_f_path,
            "-c:v", "libx264", "-b:v", f"{video_bitrate_k}k",
            "-maxrate", f"{int(video_bitrate_k * 1.25)}k",
            "-bufsize", f"{int(video_bitrate_k * 2)}k",
            "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k", out_f_path,
        ]
        subprocess.run(cmd, capture_output=True, timeout=60, check=False)  # nosec B603
        if os.path.exists(out_f_path) and os.path.getsize(out_f_path) > 0:
            with open(out_f_path, "rb") as out_f:
                compressed = out_f.read()
            if len(compressed) < len(content):
                log_all(f"🎬 视频体积较大 ({len(content)/1024/1024:.1f}MB)，已自动压制至 {len(compressed)/1024/1024:.1f}MB 以适配 QQ 上传限制", is_debug=True)
                return compressed
    except Exception as ex:
        log_all(f"⚠️ 视频自动压制异常: {ex}", is_debug=True)
    finally:
        for p in (in_f_path, out_f_path):
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
    return content


def _compress_image_if_needed(content: bytes, max_bytes: int = int(2.8 * 1024 * 1024)) -> bytes:
    """如果图片体积超过腾讯开放平台直传限制(~3MB)，通过 PIL 动态无损/高保真压缩。"""
    if not content or len(content) <= max_bytes:
        return content
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(content))
        
        img_rgb = img.convert("RGB") if img.mode != "RGB" else img
        for quality in (85, 78, 70, 60):
            buf = io.BytesIO()
            img_rgb.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
            res = buf.getvalue()
            if len(res) <= max_bytes:
                return res
        
        w, h = img.size
        scale = 0.85
        while scale >= 0.5:
            new_w = max(400, int(w * scale))
            new_h = int(h * (new_w / w))
            resized = img_rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=75, optimize=True)
            res = buf.getvalue()
            if len(res) <= max_bytes:
                return res
            scale -= 0.15

        return res
    except Exception as e:
        log_all(f"⚠️ 图片自动高保真压缩异常: {e}", is_debug=True)
        return content


async def _download_media(file_url: str, source_headers: dict[str, str]) -> bytes | None:
    """下载 message 私有媒体资源。用独立的媒体超时 —— 25MB 视频跑不进 API 的 15s。"""
    try:
        curr_loop = asyncio.get_running_loop()
    except RuntimeError:
        curr_loop = None

    client_to_use = None
    qq_mod = sys.modules.get("src.platforms.qq_official")
    _client = getattr(qq_mod, "_client", None) if qq_mod else None

    if _client is not None and not getattr(_client, "is_closed", False) and curr_loop is not None and curr_loop.is_running():
        client_to_use = _client

    try:
        if client_to_use is not None:
            try:
                resp = await client_to_use.get(
                    file_url,
                    headers=source_headers,
                    follow_redirects=True,
                    timeout=cfg.QQ_OFFICIAL_MEDIA_TIMEOUT,
                )
            except RuntimeError as ex:
                if "different event loop" not in str(ex).lower() and "event loop" not in str(ex).lower():
                    raise
                client_to_use = None

        if client_to_use is None:
            async with httpx.AsyncClient(timeout=cfg.QQ_OFFICIAL_MEDIA_TIMEOUT) as fresh_client:
                resp = await fresh_client.get(
                    file_url,
                    headers=source_headers,
                    follow_redirects=True,
                )
    except Exception as e:
        log_all(f"🔥 官方 QQ Bot 下载媒体异常: {type(e).__name__}: {e}", is_error=True)
        return None

    if resp.status_code != 200:
        log_all(f"⚠️ 官方 QQ Bot 下载媒体失败: HTTP {resp.status_code}", is_error=True)
        return None

    content = resp.content
    if len(content) > cfg.QQ_OFFICIAL_MEDIA_MAX_BYTES:
        log_all(f"⚠️ 官方 QQ Bot 媒体过大，跳过 ({len(content)} bytes)", is_error=True)
        return None
    return content


def media_items(message_chain: list[dict]) -> list[tuple[str, str]]:
    """提取可发送给官方 Bot 的媒体段，返回 (type, file_url)。"""
    items: list[tuple[str, str]] = []
    for item in message_chain:
        msg_type = item.get("type")
        if msg_type not in _MEDIA_FILE_TYPES:
            continue
        file_url = item.get("data", {}).get("file", "")
        if file_url:
            items.append((msg_type, file_url))
    return items


def _media_items_with_metadata(message_chain: list[dict]) -> list[tuple[str, str, str, str]]:
    """内部版本：在不改变 ``media_items`` 旧返回结构的前提下保留文件名提示。"""
    items: list[tuple[str, str, str, str]] = []
    for item in message_chain:
        msg_type = item.get("type")
        if msg_type not in _MEDIA_FILE_TYPES:
            continue
        data = item.get("data") or {}
        file_url = data.get("file", "")
        if not file_url:
            continue
        filename = str(data.get("filename") or data.get("file_name") or data.get("name") or "")
        mime_type = str(data.get("mime_type") or data.get("content_type") or "")
        items.append((msg_type, file_url, filename, mime_type))
    return items


async def download_media_payloads(member: dict,
                                  message_chain: list[dict]) -> list[MediaPayload]:
    """把消息链中的媒体段下载为带文件名的载荷。"""
    qq_mod = sys.modules.get("src.platforms.qq_official")
    meta_fn = getattr(qq_mod, "_media_items_with_metadata", _media_items_with_metadata) if qq_mod else _media_items_with_metadata

    items = meta_fn(message_chain)
    if not items:
        return []

    from config.credentials import get_source_headers_for_account

    headers = get_source_headers_for_account(member.get("account_id", ""), member.get("group_type", ""))
    m_name = member.get("m_name") or member.get("name") or ""
    payloads: list[MediaPayload] = []

    # 支持 monkeypatch 的 _download_media
    download_fn = getattr(qq_mod, "_download_media", _download_media) if qq_mod else _download_media

    for media_type, file_url, filename_hint, mime_type in items:
        local_bytes = None
        if m_name:
            try:
                from src import archive
                local_bytes = archive.find_media_bytes_by_url(m_name, file_url)
            except Exception:  # nosec B110
                pass

        if local_bytes is not None and len(local_bytes) > 0:
            log_all(f"📦 [官方Bot] 媒体直接复用本地归档 ({len(local_bytes)} 字节): {file_url[:60]}", is_debug=True)
            payloads.append(MediaPayload(
                media_type=media_type,
                content=local_bytes,
                filename=_safe_media_filename(filename_hint or file_url, media_type, file_url),
                mime_type=mime_type,
                source_url=file_url,
            ))
        else:
            downloaded = await download_fn(file_url, headers)
            payloads.append(MediaPayload(
                media_type=media_type,
                content=downloaded,
                filename=_safe_media_filename(filename_hint or file_url, media_type, file_url),
                mime_type=mime_type,
                source_url=file_url,
            ))
    return payloads


__all__ = [
    "MediaPayload",
    "_MEDIA_FILE_TYPES",
    "_MEDIA_DEFAULT_EXTENSIONS",
    "_IMAGE_EXTENSIONS",
    "_VIDEO_EXTENSIONS",
    "_AUDIO_EXTENSIONS",
    "_QQ_VOICE_EXTENSION",
    "_filename_candidate",
    "_safe_media_filename",
    "_media_extension",
    "_looks_like_silk",
    "_resolve_media_type",
    "_VOICE_CACHE",
    "_VOICE_CACHE_BYTES",
    "_VOICE_CACHE_LIMIT",
    "_VOICE_CACHE_LOCK",
    "_cache_voice_result",
    "_transcode_audio_to_silk",
    "_transcode_audio_to_silk_uncached",
    "_compress_video_if_needed",
    "_compress_image_if_needed",
    "_download_media",
    "download_media_payloads",
    "media_items",
    "_media_items_with_metadata",
]
