import asyncio
import base64
from pathlib import Path
import time

import httpx

# 统一通过 cfg.X 访问，热重载后标量值（超时、限速间隔等）才能生效
import config.config as cfg
from src.logger import log_all
from src.utils import RateLimiter

_MEDIA_FILE_TYPES = {
    "image": 1,
    "video": 2,
    "record": 3,
    "file": 4,
}


def _compress_video_if_needed(content: bytes, max_bytes: int = int(7.8 * 1024 * 1024)) -> bytes:
    """如果视频体积超过腾讯开放平台直传限制(8MB)，自动通过 ffmpeg 动态计算码率快速压制。"""
    if not content or len(content) <= max_bytes:
        return content
    import os
    import shutil
    import subprocess  # nosec B404
    import tempfile
    ffmpeg = shutil.which("ffmpeg") or ""
    if not ffmpeg and os.path.exists(r"D:\Scoop\User\apps\ffmpeg\current\bin\ffmpeg.EXE"):
        ffmpeg = r"D:\Scoop\User\apps\ffmpeg\current\bin\ffmpeg.EXE"
    if not ffmpeg:
        return content

    ffprobe = shutil.which("ffprobe") or ""
    if not ffprobe and os.path.exists(r"D:\Scoop\User\apps\ffmpeg\current\bin\ffprobe.EXE"):
        ffprobe = r"D:\Scoop\User\apps\ffmpeg\current\bin\ffprobe.EXE"

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
            except Exception:
                pass

        target_bitrate_k = max(200, int((max_bytes * 0.85 * 8) / dur / 1000))
        video_bitrate_k = max(150, target_bitrate_k - 64)

        cmd = [
            ffmpeg, "-y", "-i", in_f_path,
            "-c:v", "libx264", "-b:v", f"{video_bitrate_k}k",
            "-maxrate", f"{int(video_bitrate_k * 1.25)}k",
            "-bufsize", f"{int(video_bitrate_k * 2)}k",
            "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k", out_f_path
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
        
        # 依次尝试优化保存 / 渐进式 JPEG / 质量递减 (85 -> 78 -> 70 -> 60)
        img_rgb = img.convert("RGB") if img.mode != "RGB" else img
        for quality in (85, 78, 70, 60):
            buf = io.BytesIO()
            img_rgb.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
            res = buf.getvalue()
            if len(res) <= max_bytes:
                return res
        
        # 如果质量降到 60 仍然超标（极长博客长图），按比例微调缩放宽度
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


# ──────────────────────────────────────────────
# QQ 官方 Bot 实例类
# ──────────────────────────────────────────────
class QQOfficialBot:
    """单个 QQ 官方 Bot 实例，独立管理 token 和发送状态。"""

    def __init__(self, name: str, app_id: str, client_secret: str, target_openid: str,
                 group_openid: str = "", remark: str = "", member_filter: list[str] | None = None,
                 blog_filter: list[str] | None = None,
                 social_filter: list[str] | None = None,
                 push_message: bool = True,
                 push_blog: bool = False,
                 push_x: bool = True,
                 push_instagram: bool = True,
                 push_tiktok: bool = True,
                 push_live: bool = True,
                 push_alert: bool = False,
                 blog_card_mode: str = "card_and_images"):
        self.name = name
        self.remark = remark
        self.app_id = app_id
        self.client_secret = client_secret
        self.target_openid = target_openid
        self.group_openid = group_openid
        self.member_filter: list[str] = member_filter or []
        self.blog_filter: list[str] = blog_filter or []
        self.social_filter: list[str] = social_filter or []
        self.push_message: bool = push_message
        self.push_blog: bool = push_blog
        self.push_x: bool = push_x
        self.push_instagram: bool = push_instagram
        self.push_tiktok: bool = push_tiktok
        self.push_live: bool = push_live
        self.push_alert: bool = push_alert
        self.blog_card_mode: str = blog_card_mode

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
            except Exception:
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

    async def send_text(self, text: str, max_retries: int = 3) -> bool:
        """向配置的目标 openid 发送单聊纯文本消息。"""
        if not text.strip():
            return False

        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False

            url = f"{cfg.QQ_OFFICIAL_API_BASE}/v2/users/{self.target_openid}/messages"
            payload = {
                "content": text[:1900],
                "msg_type": 0,
            }
            return await self._post_json(url, payload, max_retries) is not None

    def _target_base(self, scope: str, target_openid: str) -> str:
        """scope: 'users' | 'groups'。构造 v2 目标基础 URL。"""
        return f"{cfg.QQ_OFFICIAL_API_BASE}/v2/{scope}/{target_openid}"

    async def _upload_media_chunked(self, media_type: str, content: bytes,
                                    *, scope: str = "users", target_openid: str | None = None,
                                    filename: str = "") -> str | None:
        """使用腾讯开放平台官方分片上传 (upload_prepare -> PUT -> upload_part_finish -> files 合并)。"""
        if not await self.ensure_access_token():
            return None

        file_type = _MEDIA_FILE_TYPES.get(media_type, 1)
        openid = target_openid or self.target_openid
        size_bytes = len(content)

        import hashlib
        f_md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
        f_sha1 = hashlib.sha1(content, usedforsecurity=False).hexdigest()
        f_md5_10m = hashlib.md5(content[:10002432], usedforsecurity=False).hexdigest()

        prep_url = f"{self._target_base(scope, openid)}/upload_prepare"
        prep_payload = {
            "file_type": file_type,
            "file_size": str(size_bytes),
            "file_name": filename or f"media_{int(time.time())}.mp4",
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
            "file_name": filename or f"media_{int(time.time())}.mp4",
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
                            filename: str = "") -> str | None:
        if not await self.ensure_access_token():
            return None

        size_bytes = len(content) if content else 0

        # 直传大小限制检查与自适应压缩：
        # 腾讯 QQ 开放平台直接 Base64 接口 (/files) 针对图片直传有 ~3MB 严格限制 (超出报 40093011 上传文件大小超过限制)
        if media_type == "image" and size_bytes > int(2.8 * 1024 * 1024):
            compressed = _compress_image_if_needed(content, max_bytes=int(2.8 * 1024 * 1024))
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
                content = _compress_video_if_needed(content)
                size_bytes = len(content)
            elif media_type == "image":
                content = _compress_image_if_needed(content)
                size_bytes = len(content)

        # 自动嗅探文件真实 Magic Number，防止文件名或 media_type 误标导致 850019 格式不支持
        if content:
            header = content[:32]
            if header.startswith(b"\xff\xd8\xff") or header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"GIF8") or (header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WEBP"):
                media_type = "image"
                if filename and not filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                    filename = f"{Path(filename).stem}.jpg"
            elif (len(header) >= 8 and (b"ftyp" in header[4:12] or header.startswith(b"\x00\x00\x00"))) or header.startswith(b"\x1a\x45\xdf\xa3"):
                media_type = "video"
                if filename and not filename.lower().endswith((".mp4", ".mov", ".webm", ".m4v")):
                    filename = f"{Path(filename).stem}.mp4"
            elif header.startswith(b"ID3") or header.startswith(b"\xff\xfb") or header.startswith(b"OggS") or (header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WAVE"):
                media_type = "record"
                if filename and not filename.lower().endswith((".mp3", ".wav", ".ogg", ".silk")):
                    filename = f"{Path(filename).stem}.mp3"

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
        if not file_info:
            err_code = data.get("code")
            # 850031 超限 或 850019 格式不支持时，尝试降级为 file_type=4 (文件模式) 重试
            if file_type != 4 and err_code in (850031, 850019):
                log_all(f"ℹ️ 官方 QQ Bot [{self.name}] 媒体超软限制(code {err_code})，自动降级为文件类型重传...", is_debug=True)
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

    async def _send_chain(self, scope: str, target_openid: str, member: dict,
                          message_chain: list[dict],
                          media_payloads: list[tuple[str, bytes | None]] | None = None) -> bool:
        """共享的链式发送核心：文字 + 媒体。scope='users'|'groups'。"""
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False

            ok = True
            text = chain_to_text(message_chain)
            if text:
                url = f"{self._target_base(scope, target_openid)}/messages"
                if await self._post_json(url, {"content": text[:1900], "msg_type": 0}) is None:
                    ok = False

            if media_payloads is None:
                media_payloads = await download_media_payloads(member, message_chain)
            for media_type, content in media_payloads:
                if content is None:
                    ok = False
                    continue
                file_info = await self._upload_media(media_type, content, scope=scope, target_openid=target_openid)
                if not file_info or not await self._send_uploaded_media(file_info, scope=scope, target_openid=target_openid):
                    ok = False

            return ok

    async def send_message_chain(self, member: dict, message_chain: list[dict],
                                 media_payloads: list[tuple[str, bytes | None]] | None = None) -> bool:
        """向配置的目标 openid 发送单聊完整消息链。"""
        return await self._send_chain("users", self.target_openid, member, message_chain, media_payloads)

    async def send_message_chain_to_group(self, group_openid: str, member: dict,
                                          message_chain: list[dict],
                                          media_payloads: list[tuple[str, bytes | None]] | None = None) -> bool:
        """向指定群聊发送完整消息链。"""
        return await self._send_chain("groups", group_openid, member, message_chain, media_payloads)

    async def send_group_text(self, group_openid: str, text: str, max_retries: int = 3) -> bool:
        """向指定群聊发送纯文本消息。"""
        if not text.strip():
            return False
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False
            url = f"{self._target_base('groups', group_openid)}/messages"
            return await self._post_json(url, {"content": text[:1900], "msg_type": 0}, max_retries) is not None

    async def send_private_text(self, target_openid: str, text: str, max_retries: int = 3) -> bool:
        """向指定用户发送纯文本消息。"""
        if not text.strip():
            return False
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False
            url = f"{self._target_base('users', target_openid)}/messages"
            return await self._post_json(url, {"content": text[:1900], "msg_type": 0}, max_retries) is not None

    # 兼容别名
    _send_c2c_text = send_private_text
    _send_group_text = send_group_text

    async def send_media_file(self, scope: str, target_openid: str, media_type: str, content: bytes, filename: str = "") -> bool:
        """向指定用户/群聊发送单张图片或视频媒体文件。scope: 'users' | 'groups'。"""
        if not content or not target_openid:
            return False
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False
            file_info = await self._upload_media(media_type, content, scope=scope, target_openid=target_openid, filename=filename)
            if not file_info:
                return False
            return await self._send_uploaded_media(file_info, scope=scope, target_openid=target_openid)

    async def send_translation_qq(self, scope: str, target_openid: str, pairs: list[tuple[str, str]]) -> bool:
        """发送 QQ 中日对照正文（日文斜体*，中文常规体，双语对之间零宽空格行，切分<=1800字符）。"""
        if not pairs or not target_openid:
            return True
            
        import re
        def _esc_md(t: str) -> str:
            t = t.replace('*', '＊').replace('_', '＿').replace('`', '｀')
            t = re.sub(r'^#', '＃', t, flags=re.MULTILINE)
            t = re.sub(r'^> ', '＞ ', t, flags=re.MULTILINE)
            t = re.sub(r'^- ', '－ ', t, flags=re.MULTILINE)
            return t
            
        def _format_lines(text: str, symbol: str) -> str:
            lines = text.split('\n')
            res = []
            for line in lines:
                l_str = line.strip()
                if not l_str:
                    continue
                if re.match(r'^\[写真\d+\]$', l_str):
                    res.append(l_str)
                else:
                    res.append(f"{symbol}{_esc_md(l_str)}{symbol}")
            return "\n".join(res)
            
        blocks = []
        for item in pairs:
            if isinstance(item, tuple):
                ja, zh = item
                ja_fmt = _format_lines(ja, "*")
                zh_fmt = _format_lines(zh, "")
                block = ja_fmt
                if zh_fmt:
                    block += f"\n{zh_fmt}"
                blocks.append(block)
            elif isinstance(item, str):
                blocks.append(item)
            
        MAX_LEN = 1800
        parts = []
        buf = ""
        for block in blocks:
            if len(buf) + len(block) + 4 <= MAX_LEN:
                buf = (buf + "\n​\n" + block) if buf else block
            else:
                if buf:
                    parts.append(buf)
                buf = block
        if buf:
            parts.append(buf)
            
        all_ok = True
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False
            url = f"{self._target_base(scope, target_openid)}/messages"
            for part in parts:
                resp = await self._post_json(url, {"msg_type": 2, "markdown": {"content": part}})
                if resp is None:
                    plain_part = part.replace("**", "").replace("*", "")
                    resp = await self._post_json(url, {"msg_type": 0, "content": plain_part})
                    if resp is None:
                        all_ok = False
                await asyncio.sleep(0.5)
        return all_ok


# ──────────────────────────────────────────────
# 模块级 Bot 注册表 & 媒体下载
# ──────────────────────────────────────────────
_bots: list[QQOfficialBot] = []
_client: httpx.AsyncClient | None = None   # 媒体下载用（与各 Bot 共享同一实例）


async def _download_media(file_url: str, source_headers: dict[str, str]) -> bytes | None:
    """下载 message 私有媒体资源。用独立的媒体超时 —— 25MB 视频跑不进 API 的 15s。"""
    try:
        curr_loop = asyncio.get_running_loop()
    except RuntimeError:
        curr_loop = None

    client_to_use = None
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


async def download_media_payloads(member: dict,
                                  message_chain: list[dict]) -> list[tuple[str, bytes | None]]:
    """把消息链中的媒体段逐个下载为 (type, bytes|None)，None 表示该项下载失败。
    优先复用本地已下载的归档素材，避免同一文件重复网络请求。"""
    items = media_items(message_chain)
    if not items:
        return []

    from config.credentials import get_source_headers_for_account

    headers = get_source_headers_for_account(member.get("account_id", ""), member.get("group_type", ""))
    m_name = member.get("m_name") or member.get("name") or ""
    payloads: list[tuple[str, bytes | None]] = []

    for media_type, file_url in items:
        # 1. 优先尝试从本地归档磁盘直接读取，零网络开销
        local_bytes = None
        if m_name:
            try:
                from src import archive
                local_bytes = archive.find_media_bytes_by_url(m_name, file_url)
            except Exception:
                pass

        if local_bytes is not None and len(local_bytes) > 0:
            log_all(f"📦 [官方Bot] 媒体直接复用本地归档 ({len(local_bytes)} 字节): {file_url[:60]}", is_debug=True)
            payloads.append((media_type, local_bytes))
        else:
            downloaded = await _download_media(file_url, headers)
            payloads.append((media_type, downloaded))
    return payloads


def initialize(client: httpx.AsyncClient) -> None:
    """初始化所有配置的官方 Bot 实例。"""
    global _bots, _client
    _client = client
    _bots = []
    for i, bot_cfg in enumerate(cfg.QQ_OFFICIAL_BOTS):
        if not bot_cfg.get("app_id"):
            continue  # 跳过未配置的 Bot
        bot = QQOfficialBot(
            name=bot_cfg.get("name", f"official_{i}"),
            app_id=bot_cfg["app_id"],
            client_secret=bot_cfg.get("client_secret", ""),
            target_openid=bot_cfg.get("target_openid", ""),
            group_openid=bot_cfg.get("group_openid", ""),
            remark=bot_cfg.get("remark", ""),
            member_filter=bot_cfg.get("member_filter"),
            blog_filter=bot_cfg.get("blog_filter"),
            social_filter=bot_cfg.get("social_filter"),
            push_message=bool(bot_cfg.get("push_message", True)),
            push_blog=bool(bot_cfg.get("push_blog", False)),
            push_x=bool(bot_cfg.get("push_x", True)),
            push_instagram=bool(bot_cfg.get("push_instagram", True)),
            push_tiktok=bool(bot_cfg.get("push_tiktok", True)),
            push_live=bool(bot_cfg.get("push_live", True)),
            push_alert=bool(bot_cfg.get("push_alert", False)),
            blog_card_mode=bot_cfg.get("blog_card_mode", "card_and_images")
        )
        bot.initialize(client)
        _bots.append(bot)
        display_name = f"{bot.name} ({bot.remark})" if bot.remark else bot.name
        log_all(f"📝 注册官方 QQ Bot: {display_name}")


def reload() -> None:
    """热重载：更新配置但继承已有的 client 和 access_token。"""
    global _bots
    old_bots = {b.app_id: b for b in _bots}
    new_bots = []
    for i, bot_cfg in enumerate(cfg.QQ_OFFICIAL_BOTS):
        if not bot_cfg.get("app_id"):
            continue
        bot = QQOfficialBot(
            name=bot_cfg.get("name", f"official_{i}"),
            app_id=bot_cfg["app_id"],
            client_secret=bot_cfg.get("client_secret", ""),
            target_openid=bot_cfg.get("target_openid", ""),
            group_openid=bot_cfg.get("group_openid", ""),
            remark=bot_cfg.get("remark", ""),
            member_filter=bot_cfg.get("member_filter"),
            blog_filter=bot_cfg.get("blog_filter"),
            social_filter=bot_cfg.get("social_filter"),
            push_message=bool(bot_cfg.get("push_message", True)),
            push_blog=bool(bot_cfg.get("push_blog", False)),
            push_x=bool(bot_cfg.get("push_x", True)),
            push_instagram=bool(bot_cfg.get("push_instagram", True)),
            push_tiktok=bool(bot_cfg.get("push_tiktok", True)),
            push_live=bool(bot_cfg.get("push_live", True)),
            push_alert=bool(bot_cfg.get("push_alert", False)),
            blog_card_mode=bot_cfg.get("blog_card_mode", "card_and_images")
        )
        bot.initialize(_client)
        if bot.app_id in old_bots:
            old = old_bots[bot.app_id]
            bot._access_token = old._access_token
            bot._token_expire_at = old._token_expire_at
        new_bots.append(bot)
    _bots = new_bots
    log_all("📝 官方 QQ Bot 已热重载")


def get_bots() -> list[QQOfficialBot]:
    """返回所有已初始化的 Bot 实例。"""
    return _bots


def get_configured_bots() -> list[QQOfficialBot]:
    """返回所有配置完整、可用于发送的 Bot 实例。"""
    return [bot for bot in _bots if bot.is_configured()]


def has_bots() -> bool:
    """返回是否存在至少一个配置完整的 Bot。"""
    return any(bot.is_configured() for bot in _bots)


# ──────────────────────────────────────────────
# 工具函数（保持原有接口兼容）
# ──────────────────────────────────────────────
def chain_to_text(message_chain: list[dict]) -> str:
    """提取官方 Bot 文本消息内容。"""
    parts: list[str] = []
    for item in message_chain:
        msg_type = item.get("type")
        data = item.get("data", {})
        if msg_type == "text":
            text = data.get("text", "")
            if text:
                parts.append(text)
    return "".join(parts).strip()


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


async def health_check() -> bool:
    """启动时检查所有 Bot 凭证是否可用。"""
    if not _bots:
        return False
    all_ok = True
    for bot in _bots:
        if await bot.ensure_access_token():
            log_all(f"🟢 官方 QQ Bot [{bot.name}] 凭证正常")
        else:
            log_all(f"🔴 官方 QQ Bot [{bot.name}] 凭证无效", is_error=True)
            all_ok = False
    return all_ok


async def send_text(text: str, max_retries: int = 3) -> bool:
    """向所有 Bot 发送纯文本消息（用于报警）。"""
    if not _bots:
        return False
    all_ok = True
    for bot in _bots:
        if not await bot.send_text(text, max_retries):
            all_ok = False
    return all_ok
