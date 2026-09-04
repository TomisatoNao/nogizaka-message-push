"""QQ 官方 OpenAPI 底层 HTTP 客户端与富媒体上传。"""

import asyncio
import base64
import hashlib
import time

import httpx

import config.config as cfg
from src.logger import log_all
from src.platforms.qq_official_media import (
    _MEDIA_FILE_TYPES,
    _compress_image_if_needed,
    _compress_video_if_needed,
    _resolve_media_type,
    _safe_media_filename,
    _transcode_audio_to_silk,
)
from src.utils import RateLimiter


def _get_helper(name: str, default):
    import sys
    mod = sys.modules.get("src.platforms.qq_official")
    if mod and hasattr(mod, name):
        return getattr(mod, name)
    return default


class QQOfficialClient:
    """QQ 官方 OpenAPI 底层客户端，独立管理凭证刷新、限速、请求重试与富媒体文件上传。"""

    def __init__(self, app_id: str, client_secret: str, name: str = "", target_openid: str = ""):
        self.name = name
        self.app_id = app_id
        self.client_secret = client_secret
        self.target_openid = target_openid

        # 实例级状态
        self._client: httpx.AsyncClient | None = None
        self._send_limiter = RateLimiter(lambda: getattr(cfg, "QQ_SEND_INTERVAL", 1.5))
        self._access_token: str = ""
        self._token_expire_at: float = 0.0
        self._last_send_ts: float = 0.0

    def initialize(self, client: httpx.AsyncClient) -> None:
        """注入共享的 AsyncClient 实例。"""
        self._client = client

    def is_configured(self) -> bool:
        """凭证完整即可（target_openid 允许为空——群推送专用 Bot 不需要单聊目标）。"""
        return bool(self.app_id and self.client_secret)

    async def _safe_post(self, url: str, json_body: dict, headers: dict | None = None, timeout: float | None = None) -> httpx.Response:
        t = timeout or cfg.QQ_OFFICIAL_TIMEOUT
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if self._client is not None and not getattr(self._client, "is_closed", False) and curr_loop is not None and curr_loop.is_running():
            try:
                return await self._client.post(url, json=json_body, headers=headers, timeout=t)
            except RuntimeError as ex:
                if "different event loop" not in str(ex).lower() and "event loop" not in str(ex).lower():
                    raise
            except Exception:  # nosec B110
                pass

        async with httpx.AsyncClient(timeout=t) as client:
            return await client.post(url, json=json_body, headers=headers)

    async def ensure_access_token(self) -> bool:
        """获取并缓存 access_token。"""
        if not self.is_configured():
            log_all(
                f"🚨 官方 QQ Bot [{self.name}] 配置不完整，请检查 APP_ID / CLIENT_SECRET",
                is_error=True,
            )
            return False

        now = time.time()
        if self._access_token and now < self._token_expire_at - 60:
            return True

        try:
            resp = await self._safe_post(
                cfg.QQ_OFFICIAL_TOKEN_URL,
                json_body={
                    "appId": self.app_id,
                    "clientSecret": self.client_secret,
                },
                headers={"Content-Type": "application/json"},
                timeout=cfg.QQ_OFFICIAL_TIMEOUT,
            )
        except Exception as e:
            log_all(
                f"🔥 官方 QQ Bot [{self.name}] 获取 access_token 异常: {type(e).__name__}: {e}",
                is_error=True,
            )
            return False

        if resp.status_code != 200:
            log_all(
                f"🚨 官方 QQ Bot [{self.name}] 获取 access_token 失败: HTTP {resp.status_code} | {resp.text[:200]}",
                is_error=True,
            )
            return False

        try:
            data = resp.json()
        except ValueError:
            log_all(f"🚨 官方 QQ Bot [{self.name}] token 响应不是合法 JSON", is_error=True)
            return False

        token = data.get("access_token")
        if not token:
            log_all(
                f"🚨 官方 QQ Bot [{self.name}] token 响应缺少 access_token: {data}",
                is_error=True,
            )
            return False

        expires_in = int(data.get("expires_in", 7200))
        self._access_token = token
        self._token_expire_at = now + expires_in
        log_all(
            f"✅ 官方 QQ Bot [{self.name}] access_token 已更新，有效期约 {expires_in}s",
            is_debug=True,
        )
        return True

    async def _wait_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_send_ts
        if elapsed < cfg.QQ_OFFICIAL_MIN_INTERVAL:
            await asyncio.sleep(cfg.QQ_OFFICIAL_MIN_INTERVAL - elapsed)

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"QQBot {self._access_token}",
            "Content-Type": "application/json",
        }

    async def _post_json(self, url: str, payload: dict, max_retries: int = 3, timeout: float | None = None) -> httpx.Response | None:
        t = timeout or cfg.QQ_OFFICIAL_TIMEOUT
        for attempt in range(max_retries):
            await self._wait_rate_limit()
            try:
                resp = await self._safe_post(
                    url,
                    json_body=payload,
                    headers=self._auth_headers(),
                    timeout=t,
                )
                self._last_send_ts = time.monotonic()

                if resp.status_code in {200, 201}:
                    return resp

                if resp.status_code == 400 and "/files" in url:
                    # 允许 /files 接口携带业务错误码 (如 850019/850031) 返回给上层做降级重试
                    return resp

                log_all(
                    f"⚠️ 官方 QQ Bot [{self.name}] 请求失败 ({attempt + 1}/{max_retries}): "
                    f"HTTP {resp.status_code} | {resp.text[:200]}",
                    is_error=True,
                )
                if resp.status_code == 401:
                    self._access_token = ""  # nosec B105
                    if not await self.ensure_access_token():
                        return None
                elif resp.status_code in {429, 500, 502, 503, 504} and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None

            except Exception as e:
                log_all(
                    f"🔥 官方 QQ Bot [{self.name}] 请求异常 ({attempt + 1}/{max_retries}): "
                    f"{type(e).__name__}: {e}",
                    is_error=True,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        return None

    def _target_base(self, scope: str, target_openid: str) -> str:
        """scope: 'users' | 'groups'。构造 v2 目标基础 URL。"""
        return f"{cfg.QQ_OFFICIAL_API_BASE}/v2/{scope}/{target_openid}"

    async def _upload_media_chunked(self, media_type: str, content: bytes,
                                    *, scope: str = "users", target_openid: str | None = None,
                                    filename: str = "") -> str | None:
        """使用腾讯开放平台官方分片上传 (upload_prepare -> PUT -> upload_part_finish -> files 合并)。"""
        if not await self.ensure_access_token():
            return None

        resolve_media_type = _get_helper("_resolve_media_type", _resolve_media_type)
        safe_media_filename = _get_helper("_safe_media_filename", _safe_media_filename)
        media_type = resolve_media_type(media_type, filename, content)
        filename = safe_media_filename(filename, media_type)
        file_type = _MEDIA_FILE_TYPES.get(media_type, 1)
        openid = target_openid or self.target_openid
        size_bytes = len(content)

        f_md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        f_sha1 = hashlib.sha1(content, usedforsecurity=False).hexdigest()
        f_md5_10m = hashlib.md5(content[:10002432], usedforsecurity=False).hexdigest()

        prep_url = f"{self._target_base(scope, openid)}/upload_prepare"
        prep_payload = {
            "file_type": file_type,
            "file_size": str(size_bytes),
            "file_name": filename,
            "md5": f_md5,
            "sha1": f_sha1,
            "md5_10m": f_md5_10m,
        }

        resp = await self._post_json(prep_url, prep_payload)
        if not resp or resp.status_code != 200:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] upload_prepare 失败", is_debug=True)
            return None

        try:
            prep_data = resp.json()
            upload_id = prep_data.get("upload_id")
            parts = prep_data.get("parts") or []
        except Exception as ex:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 解析 upload_prepare 异常: {ex}", is_debug=True)
            return None

        if not upload_id or not parts:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] upload_prepare 未返回有效 parts", is_debug=True)
            return None

        log_all(f"📤 官方 QQ Bot [{self.name}] 启动分片上传 (共 {len(parts)} 片, {size_bytes/1024/1024:.1f}MB)", is_debug=True)

        offset = 0
        loop = asyncio.get_running_loop()
        import requests

        def _sync_put(url: str, chunk_data: bytes) -> bool:
            try:
                res = requests.put(url, data=chunk_data, timeout=90)
                return res.status_code in (200, 204)
            except Exception as ex:
                log_all(f"⚠️ PUT 分片失败: {ex}", is_debug=True)
                return False

        for p in parts:
            p_idx = p["index"]
            p_url = p["presigned_url"]
            p_size = int(p["block_size"])
            chunk = content[offset : offset + p_size]
            offset += p_size

            put_ok = await loop.run_in_executor(None, _sync_put, p_url, chunk)
            if not put_ok:
                log_all(f"⚠️ 官方 QQ Bot [{self.name}] 分片 {p_idx} PUT 上传失败", is_error=True)
                return None

            finish_url = f"{self._target_base(scope, openid)}/upload_part_finish"
            finish_payload = {
                "upload_id": upload_id,
                "part_index": p_idx,
                "block_size": str(len(chunk)),
                "md5": hashlib.md5(chunk, usedforsecurity=False).hexdigest(),
            }
            finish_resp = await self._post_json(finish_url, finish_payload)
            if not finish_resp or finish_resp.status_code != 200:
                log_all(f"⚠️ 官方 QQ Bot [{self.name}] 分片 {p_idx} upload_part_finish 失败", is_error=True)
                return None

        merge_url = f"{self._target_base(scope, openid)}/files"
        merge_payload = {
            "file_type": file_type,
            "upload_id": upload_id,
            "file_name": filename,
            "srv_send_msg": False,
        }
        merge_resp = await self._post_json(merge_url, merge_payload)
        if not merge_resp or merge_resp.status_code != 200:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 分片合并失败", is_error=True)
            return None

        try:
            merge_data = merge_resp.json()
            file_info = merge_data.get("file_info") or merge_data.get("data", {}).get("file_info")
            if file_info:
                log_all(f"✅ 官方 QQ Bot [{self.name}] 大文件分片上传合并成功 ({size_bytes/1024/1024:.1f}MB)", is_debug=True)
                return file_info
        except Exception as ex:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 解析合并响应异常: {ex}", is_error=True)
        return None

    async def _upload_media(self, media_type: str, content: bytes,
                            *, scope: str = "users", target_openid: str | None = None,
                            filename: str = "", mime_type: str = "") -> str | None:
        if not await self.ensure_access_token():
            return None

        if not content:
            return None

        resolve_media_type = _get_helper("_resolve_media_type", _resolve_media_type)
        safe_media_filename = _get_helper("_safe_media_filename", _safe_media_filename)
        compress_image_if_needed = _get_helper("_compress_image_if_needed", _compress_image_if_needed)
        compress_video_if_needed = _get_helper("_compress_video_if_needed", _compress_video_if_needed)
        transcode_audio_to_silk = _get_helper("_transcode_audio_to_silk", _transcode_audio_to_silk)

        media_type = resolve_media_type(media_type, filename, content, mime_type)
        filename = safe_media_filename(filename, media_type)

        size_bytes = len(content) if content else 0

        # 直传大小限制检查与自适应压缩：
        # 腾讯 QQ 开放平台直接 Base64 接口 (/files) 针对图片直传有 ~3MB 严格限制 (超出报 40093011 上传文件大小超过限制)
        if media_type == "image" and size_bytes > int(2.8 * 1024 * 1024):
            compressed = compress_image_if_needed(content, max_bytes=int(2.8 * 1024 * 1024))
            if len(compressed) < size_bytes:
                log_all(f"📦 官方 QQ Bot [{self.name}] 图片超出直传限制 ({size_bytes/1024/1024:.2f}MB)，已自动高保真压缩至 {len(compressed)/1024/1024:.2f}MB", is_debug=True)
                content = compressed
                size_bytes = len(content)

        # 如果文件大于 7.8MB，优先使用官方分片上传（保持 100% 原画质，最大支持 200MB）
        if size_bytes > int(7.8 * 1024 * 1024):
            file_info = await self._upload_media_chunked(
                media_type, content, scope=scope, target_openid=target_openid, filename=filename
            )
            if file_info:
                return file_info
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 分片上传未成功，降级尝试压制后直传", is_debug=True)
            if media_type == "video":
                content = compress_video_if_needed(content)
                size_bytes = len(content)
            elif media_type == "image":
                content = compress_image_if_needed(content)
                size_bytes = len(content)

        file_type = _MEDIA_FILE_TYPES.get(media_type, 1)
        size_bytes = len(content) if content else 0

        # 根据腾讯开放平台规范：
        # 1=图片(20MB), 2=视频(30MB), 3=语音(20MB), 4=文件(200MB)
        # 超出软限制时降级为文件类型 4 上传
        if media_type == "image" and size_bytes > 20 * 1024 * 1024:
            file_type = 4
        elif media_type == "video" and size_bytes > 30 * 1024 * 1024:
            file_type = 4
        elif media_type in ("record", "voice") and size_bytes > 20 * 1024 * 1024:
            file_type = 4

        openid = target_openid or self.target_openid
        url = f"{self._target_base(scope, openid)}/files"
        payload = {
            "file_type": file_type,
            "file_data": base64.b64encode(content).decode("ascii"),
            "srv_send_msg": False,
        }
        if filename:
            payload["file_name"] = filename

        # 动态计算超时：大文件基础 60s，按 100KB/s 保障充足上传窗口（上限 300s）
        upload_timeout = min(300.0, max(60.0, size_bytes / (100 * 1024)))
        resp = await self._post_json(url, payload, timeout=upload_timeout)
        if resp is None:
            return None

        try:
            data = resp.json()
        except ValueError:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 媒体上传响应不是合法 JSON", is_error=True)
            return None

        file_info = data.get("file_info") or data.get("data", {}).get("file_info")
        err_code = data.get("code")

        # 850019 (格式不支持) SILK 转码兜底
        if not file_info and media_type == "record" and file_type == 3 and err_code == 850019:
            log_all(
                f"ℹ️ 官方 QQ Bot [{self.name}] 原格式语音上传被拒绝(code 850019)，尝试 SILK 语音重试",
                is_debug=True,
            )
            voice_result = await asyncio.to_thread(transcode_audio_to_silk, content, filename)
            if voice_result:
                voice_content, voice_filename = voice_result
                voice_payload = {
                    "file_type": 3,
                    "file_data": base64.b64encode(voice_content).decode("ascii"),
                    "srv_send_msg": False,
                    "file_name": voice_filename,
                }
                voice_timeout = min(300.0, max(60.0, len(voice_content) / (100 * 1024)))
                voice_resp = await self._post_json(url, voice_payload, timeout=voice_timeout)
                if voice_resp is not None:
                    try:
                        voice_data = voice_resp.json()
                    except ValueError:
                        voice_data = {}
                    file_info = voice_data.get("file_info") or voice_data.get("data", {}).get("file_info")
                    if file_info:
                        log_all(
                            f"✅ 官方 QQ Bot [{self.name}] SILK 语音重试成功",
                            is_debug=True,
                        )
                        return file_info

        # 850031 超限或 850019 格式不支持且 SILK 兜底不可用时，降级为普通附件
        if not file_info and file_type != 4 and err_code in (850031, 850019):
            log_all(
                f"ℹ️ 官方 QQ Bot [{self.name}] 媒体上传降级为文件类型(code {err_code})",
                is_debug=True,
            )
            payload["file_type"] = 4
            resp2 = await self._post_json(url, payload, timeout=upload_timeout)
            if resp2:
                try:
                    data2 = resp2.json()
                    file_info = data2.get("file_info") or data2.get("data", {}).get("file_info")
                except ValueError:
                    pass

        if not file_info:
            log_all(
                f"⚠️ 官方 QQ Bot [{self.name}] 媒体上传响应缺少 file_info: {data}",
                is_error=True,
            )
            return None
        return file_info

    async def _send_uploaded_media(self, file_info: str,
                                    *, scope: str = "users", target_openid: str | None = None) -> bool:
        openid = target_openid or self.target_openid
        url = f"{self._target_base(scope, openid)}/messages"
        payload = {
            "msg_type": 7,
            "media": {
                "file_info": file_info,
            },
        }
        return await self._post_json(url, payload) is not None


__all__ = [
    "QQOfficialClient",
]
