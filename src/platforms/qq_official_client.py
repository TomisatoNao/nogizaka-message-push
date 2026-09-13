"""QQ 官方 OpenAPI 底层 HTTP 客户端与富媒体上传。"""

import asyncio
import base64
import hashlib
import time
from urllib.parse import urlparse

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


# QQ 官方 Bot 的简单 ``file_data`` 接口限制的是整个 JSON 请求体，
# Base64 会让原始字节额外膨胀约三分之一。留出 JSON 字段空间后，2 MiB
# 是一个保守的直传上限；超过它统一走官方 upload_prepare 分片链路。
_DIRECT_FILE_DATA_MAX_BYTES = 2 * 1024 * 1024
_MEDIA_SIZE_ERROR_CODES = frozenset({40093011, 850031})


def _is_http_url(value: str) -> bool:
    """只接受可由 QQ 服务端拉取的 HTTP(S) 媒体地址。"""
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _safe_url_for_log(value: str) -> str:
    """日志只保留 URL 路径，避免把签名 query/token 写入日志。"""
    parsed = urlparse(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return "<invalid-url>"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"[:120]


def _extract_file_info(data: object) -> str | None:
    """从不同网关版本的上传响应中提取文件句柄。"""
    if not isinstance(data, dict):
        return None
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    value = (
        data.get("file_info")
        or data.get("file_uuid")
        or nested.get("file_info")
        or nested.get("file_uuid")
    )
    return str(value) if value else None



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
                # 只有确定是事件循环绑定错误时才换用临时 client；网络
                # 超时/连接失败必须原样抛出，不能悄悄再发一次非幂等请求。

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
        total_attempts = max(1, int(max_retries or 1))
        for attempt in range(total_attempts):
            await self._wait_rate_limit()
            try:
                resp = await self._safe_post(
                    url,
                    json_body=payload,
                    headers=self._auth_headers(),
                    timeout=t,
                )
                self._last_send_ts = time.monotonic()

                if resp.status_code in {200, 201, 204}:
                    return resp

                if resp.status_code == 400 and "/files" in url:
                    # 允许 /files 接口携带业务错误码 (如 850019/850031) 返回给上层做降级重试
                    return resp

                log_all(
                    f"⚠️ 官方 QQ Bot [{self.name}] 请求失败 ({attempt + 1}/{total_attempts}): "
                    f"HTTP {resp.status_code} | {resp.text[:200]}",
                    is_error=True,
                )
                if resp.status_code == 401:
                    self._access_token = ""  # nosec B105
                    if not await self.ensure_access_token():
                        return None
                elif resp.status_code in {429, 500, 502, 503, 504} and attempt < total_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None

            except Exception as e:
                log_all(
                    f"🔥 官方 QQ Bot [{self.name}] 请求异常 ({attempt + 1}/{total_attempts}): "
                    f"{type(e).__name__}: {e}",
                    is_error=True,
                )
                if attempt < total_attempts - 1:
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

        media_type = _resolve_media_type(media_type, filename, content)
        filename = _safe_media_filename(filename, media_type)
        file_type = _MEDIA_FILE_TYPES.get(media_type, 1)
        openid = target_openid or self.target_openid
        size_bytes = len(content)

        f_md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        f_sha1 = hashlib.sha1(content, usedforsecurity=False).hexdigest()
        f_md5_10m = hashlib.md5(content[:10002432], usedforsecurity=False).hexdigest()

        prep_url = f"{self._target_base(scope, openid)}/upload_prepare"
        prep_payload = {
            "file_type": file_type,
            "file_size": size_bytes,
            "file_name": filename,
            "md5": f_md5,
            "sha1": f_sha1,
            "md5_10m": f_md5_10m,
        }

        # prepare 本身不发送群消息，失败时不重复创建上传任务；分片 PUT
        # 与 complete 仍允许有限重试，因为它们都带有同一个 upload_id。
        resp = await self._post_json(prep_url, prep_payload, max_retries=1)
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

        log_all(
            f"📤 官方 QQ Bot [{self.name}] 启动官方分片上传 "
            f"(共 {len(parts)} 片, {size_bytes/1024/1024:.2f}MB)",
            is_debug=True,
        )

        # 官方返回的字段在不同网关版本中曾出现过 upload_url / presigned_url
        # 两种命名；同时接受两者，避免准备成功后在 PUT 阶段误判失败。
        try:
            default_block_size = int(prep_data.get("block_size") or 0)
        except (TypeError, ValueError, AttributeError):
            default_block_size = 0
        normalized_parts: list[tuple[int, str, int]] = []
        for raw_part in parts:
            try:
                p_idx = int(raw_part.get("index", raw_part.get("part_index", 0)))
                p_url = str(
                    raw_part.get("presigned_url")
                    or raw_part.get("upload_url")
                    or raw_part.get("url")
                    or ""
                )
                p_size = int(raw_part.get("block_size") or default_block_size)
            except (TypeError, ValueError, AttributeError):
                continue
            if p_idx > 0 and p_url and p_size > 0:
                normalized_parts.append((p_idx, p_url, p_size))

        normalized_parts.sort(key=lambda item: item[0])

        if not normalized_parts:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 分片列表字段无效", is_error=True)
            return None

        offset = 0
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as upload_client:
            for p_idx, p_url, p_size in normalized_parts:
                # part_index 是 1-based；当服务端提供了总 block_size 时，
                # 用它定位分片而不是依赖响应数组顺序，避免漏片或错位拼接。
                expected_offset = (
                    (p_idx - 1) * default_block_size
                    if default_block_size > 0
                    else offset
                )
                if expected_offset != offset:
                    log_all(
                        f"⚠️ 官方 QQ Bot [{self.name}] 分片索引不连续 "
                        f"(part={p_idx}, offset={expected_offset}, expected={offset})",
                        is_error=True,
                    )
                    return None
                chunk = content[expected_offset : expected_offset + p_size]
                offset += len(chunk)
                if not chunk:
                    log_all(f"⚠️ 官方 QQ Bot [{self.name}] 分片 {p_idx} 内容为空", is_error=True)
                    return None

                put_ok = False
                for put_attempt in range(2):
                    try:
                        put_resp = await upload_client.put(
                            p_url,
                            content=chunk,
                            headers={"Content-Length": str(len(chunk))},
                        )
                        if put_resp.status_code in (200, 201, 204):
                            put_ok = True
                            break
                        log_all(
                            f"⚠️ 官方 QQ Bot [{self.name}] 分片 {p_idx} PUT HTTP {put_resp.status_code} "
                            f"({put_attempt + 1}/2)",
                            is_debug=True,
                        )
                    except (httpx.HTTPError, OSError) as ex:
                        log_all(
                            f"⚠️ 官方 QQ Bot [{self.name}] 分片 {p_idx} PUT 异常 "
                            f"({put_attempt + 1}/2): {type(ex).__name__}",
                            is_debug=True,
                        )
                    if put_attempt == 0:
                        await asyncio.sleep(1.0)

                if not put_ok:
                    log_all(f"⚠️ 官方 QQ Bot [{self.name}] 分片 {p_idx} PUT 上传失败", is_error=True)
                    return None

                finish_url = f"{self._target_base(scope, openid)}/upload_part_finish"
                finish_payload = {
                    "upload_id": upload_id,
                    "part_index": p_idx,
                    "block_size": len(chunk),
                    "md5": hashlib.md5(chunk, usedforsecurity=False).hexdigest(),
                }
                finish_resp = await self._post_json(finish_url, finish_payload, max_retries=2)
                if not finish_resp or finish_resp.status_code not in {200, 201, 204}:
                    log_all(
                        f"⚠️ 官方 QQ Bot [{self.name}] 分片 {p_idx} upload_part_finish 失败",
                        is_error=True,
                    )
                    return None

        if offset != size_bytes:
            log_all(
                f"⚠️ 官方 QQ Bot [{self.name}] 分片上传字节数不一致 "
                f"(已处理 {offset} / 应为 {size_bytes})",
                is_error=True,
            )
            return None

        merge_url = f"{self._target_base(scope, openid)}/files"
        # 分片完成接口只需要 upload_id；不要再次携带 file_data 或其它直传字段。
        merge_resp = await self._post_json(merge_url, {"upload_id": upload_id}, max_retries=2)
        if not merge_resp or merge_resp.status_code != 200:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 分片合并失败", is_error=True)
            return None

        try:
            merge_data = merge_resp.json()
            file_info = _extract_file_info(merge_data)
            if file_info:
                log_all(f"✅ 官方 QQ Bot [{self.name}] 大文件分片上传合并成功 ({size_bytes/1024/1024:.1f}MB)", is_debug=True)
                return file_info
        except Exception as ex:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 解析合并响应异常: {ex}", is_error=True)
        return None

    async def _upload_media_via_url(
        self,
        media_type: str,
        source_url: str,
        *,
        scope: str,
        target_openid: str | None,
        filename: str,
        mime_type: str = "",
    ) -> tuple[str | None, bool]:
        """让 QQ 服务端直接拉取远程媒体。

        返回 ``(file_info, safe_to_fallback)``。HTTP 明确拒绝时可以安全改用
        本地字节；网络/读取超时则结果未知，不能立刻再提交一次造成重复上传。
        """
        if not _is_http_url(source_url):
            return None, True

        resolved_type = _resolve_media_type(media_type, filename, None, mime_type)
        file_type = _MEDIA_FILE_TYPES.get(resolved_type, 1)
        openid = target_openid or self.target_openid
        url = f"{self._target_base(scope, openid)}/files"
        payload = {
            "file_type": file_type,
            "url": source_url,
            "srv_send_msg": False,
        }
        if file_type == _MEDIA_FILE_TYPES["file"]:
            payload["file_name"] = _safe_media_filename(filename, resolved_type, source_url)

        resp = await self._post_json(
            url,
            payload,
            max_retries=1,
            timeout=cfg.QQ_OFFICIAL_MEDIA_TIMEOUT,
        )
        if resp is None:
            log_all(
                f"⚠️ 官方 QQ Bot [{self.name}] URL 媒体上传无回包，结果未知；本次不重复提交",
                is_error=True,
            )
            return None, False

        try:
            data = resp.json()
        except ValueError:
            # 2xx 但没有可解析的回包时，服务端是否已经生成文件无法判断；
            # 不要再用本地字节提交第二份。明确的 4xx 才属于可安全回退。
            safe_to_fallback = resp.status_code >= 400
            log_all(
                f"⚠️ 官方 QQ Bot [{self.name}] URL 上传响应不是合法 JSON "
                f"(HTTP {resp.status_code})",
                is_error=True,
            )
            return None, safe_to_fallback
        if not isinstance(data, dict):
            safe_to_fallback = resp.status_code >= 400
            log_all(
                f"⚠️ 官方 QQ Bot [{self.name}] URL 上传响应格式异常 "
                f"(HTTP {resp.status_code})",
                is_error=True,
            )
            return None, safe_to_fallback
        file_info = _extract_file_info(data)
        if file_info:
            log_all(
                f"✅ 官方 QQ Bot [{self.name}] 使用 URL 直取媒体成功 "
                f"({resolved_type}, {_safe_url_for_log(source_url)})",
                is_debug=True,
            )
            return str(file_info), True

        # 成功状态却缺少 file_info 也按“结果未知”处理；否则本地回退
        # 可能再次提交同一个媒体，重现“日志失败但群里收到了多份”的问题。
        safe_to_fallback = resp.status_code >= 400
        log_all(
            f"⚠️ 官方 QQ Bot [{self.name}] URL 媒体上传未返回 file_info "
            f"(HTTP {resp.status_code}, code={data.get('code')})"
            + ("，改用本地上传" if safe_to_fallback else "，结果未知，不重复提交"),
            is_debug=not safe_to_fallback,
            is_error=safe_to_fallback,
        )
        return None, safe_to_fallback

    async def _upload_media(self, media_type: str, content: bytes | None,
                            *, scope: str = "users", target_openid: str | None = None,
                            filename: str = "", mime_type: str = "", source_url: str = "") -> str | None:
        if not await self.ensure_access_token():
            return None

        if not content and not source_url:
            return None

        media_type = _resolve_media_type(media_type, filename, content, mime_type)
        filename = _safe_media_filename(filename, media_type)

        if source_url:
            url_file_info, safe_to_fallback = await self._upload_media_via_url(
                media_type,
                source_url,
                scope=scope,
                target_openid=target_openid,
                filename=filename,
                mime_type=mime_type,
            )
            if url_file_info:
                return url_file_info
            if not safe_to_fallback:
                return None
            if not content:
                return None

        size_bytes = len(content)
        chunked_attempted = False

        # 本地文件超过 Base64 安全阈值时，直接走官方分片上传，避免
        # “先压缩仍超限”的循环，也保留原始画质。
        if size_bytes > _DIRECT_FILE_DATA_MAX_BYTES:
            chunked_attempted = True
            file_info = await self._upload_media_chunked(
                media_type, content, scope=scope, target_openid=target_openid, filename=filename
            )
            if file_info:
                return file_info
            log_all(
                f"⚠️ 官方 QQ Bot [{self.name}] 分片上传失败；仅在可明显缩小媒体时执行一次压缩兜底",
                is_debug=True,
            )

            compressed = content
            if media_type == "image":
                compressed = _compress_image_if_needed(content, max_bytes=_DIRECT_FILE_DATA_MAX_BYTES)
            elif media_type == "video":
                compressed = _compress_video_if_needed(content, max_bytes=_DIRECT_FILE_DATA_MAX_BYTES)
            if len(compressed) < size_bytes:
                log_all(
                    f"📦 官方 QQ Bot [{self.name}] 分片失败后单次压缩兜底 "
                    f"({size_bytes/1024/1024:.2f}MB → {len(compressed)/1024/1024:.2f}MB)",
                    is_debug=True,
                )
                content = compressed
                size_bytes = len(content)

            # 压缩后仍然超出 Base64 安全阈值时，不再把必然失败的超大
            # JSON 重复提交；错误已经由分片链路记录。
            if size_bytes > _DIRECT_FILE_DATA_MAX_BYTES:
                return None

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
        # /files + file_data 是非幂等直传。超时后服务端可能已经接收，
        # 因此严格只提交一次，避免用户看到重复媒体。
        resp = await self._post_json(url, payload, max_retries=1, timeout=upload_timeout)
        if resp is None:
            return None

        try:
            data = resp.json()
        except ValueError:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 媒体上传响应不是合法 JSON", is_error=True)
            return None

        file_info = _extract_file_info(data)
        raw_err_code = data.get("code", data.get("err_code"))
        try:
            err_code = int(raw_err_code) if raw_err_code is not None else None
        except (TypeError, ValueError):
            err_code = None

        # 直传大小错误不再改成另一份 Base64 再试；如果之前没有走过
        # 分片，则只补偿一次官方分片上传。
        if not file_info and err_code in _MEDIA_SIZE_ERROR_CODES and not chunked_attempted:
            chunked_attempted = True
            log_all(
                f"ℹ️ 官方 QQ Bot [{self.name}] 直传超限(code {err_code})，改用一次官方分片上传",
                is_debug=True,
            )
            chunked_info = await self._upload_media_chunked(
                media_type, content, scope=scope, target_openid=target_openid, filename=filename
            )
            if chunked_info:
                return chunked_info

        # 850019 (格式不支持) SILK 转码兜底
        if not file_info and media_type == "record" and file_type == 3 and err_code == 850019:
            log_all(
                f"ℹ️ 官方 QQ Bot [{self.name}] 原格式语音上传被拒绝(code 850019)，尝试 SILK 语音重试",
                is_debug=True,
            )
            voice_result = await asyncio.to_thread(_transcode_audio_to_silk, content, filename)
            if voice_result:
                voice_content, voice_filename = voice_result
                voice_payload = {
                    "file_type": 3,
                    "file_data": base64.b64encode(voice_content).decode("ascii"),
                    "srv_send_msg": False,
                    "file_name": voice_filename,
                }
                voice_timeout = min(300.0, max(60.0, len(voice_content) / (100 * 1024)))
                voice_resp = await self._post_json(url, voice_payload, max_retries=1, timeout=voice_timeout)
                if voice_resp is not None:
                    try:
                        voice_data = voice_resp.json()
                    except ValueError:
                        voice_data = {}
                    file_info = _extract_file_info(voice_data)
                    if file_info:
                        log_all(
                            f"✅ 官方 QQ Bot [{self.name}] SILK 语音重试成功",
                            is_debug=True,
                        )
                        return file_info

        # 850019 格式不支持且 SILK 兜底不可用时，降级为普通附件。
        # 40093011/850031 大小错误已在上方尝试分片，不再重复 Base64 直传。
        if not file_info and file_type != 4 and err_code == 850019:
            log_all(
                f"ℹ️ 官方 QQ Bot [{self.name}] 媒体上传降级为文件类型(code {err_code})",
                is_debug=True,
            )
            payload["file_type"] = 4
            resp2 = await self._post_json(url, payload, max_retries=1, timeout=upload_timeout)
            if resp2:
                try:
                    data2 = resp2.json()
                    file_info = _extract_file_info(data2)
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
        # 发送消息本身也是非幂等操作；响应超时不自动重发，避免“日志失败但
        # 群里已经收到”时产生重复消息。
        return await self._post_json(url, payload, max_retries=1) is not None


__all__ = [
    "QQOfficialClient",
]
