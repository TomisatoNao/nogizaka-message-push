"""同源代理官方原图，规避浏览器下载时的跨域限制。"""

from __future__ import annotations

import ipaddress
import socket
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.webui_modules.archive.common import _send_json_resp


_OFFICIAL_IMAGE_DOMAINS = (
    "hinatazaka46.com",
    "nogizaka46.com",
    "sakurazaka46.com",
)
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 20
_MAX_REDIRECTS = 3
_IMAGE_TYPES = {
    "image/jpeg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/png": (".png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/webp": (".webp", lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"),
    "image/gif": (".gif", lambda data: data.startswith((b"GIF87a", b"GIF89a"))),
    "image/avif": (".avif", lambda data: len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}),
}


class OriginalDownloadError(ValueError):
    """远程原图不符合代理策略或无法下载。"""


def _validate_remote_url(url: str) -> str:
    """只允许 HTTPS 官方域名，并拒绝解析到非公网地址的目标。"""
    try:
        parts = urlsplit(str(url or "").strip())
        hostname = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except (TypeError, ValueError):
        raise OriginalDownloadError("原图地址无效") from None

    if (
        parts.scheme.lower() != "https"
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or not any(hostname == domain or hostname.endswith("." + domain) for domain in _OFFICIAL_IMAGE_DOMAINS)
    ):
        raise OriginalDownloadError("仅支持坂道官方 HTTPS 图片地址")

    try:
        resolved = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise OriginalDownloadError("官方图片域名暂时无法解析") from None
    if not resolved:
        raise OriginalDownloadError("官方图片域名暂时无法解析")
    for record in resolved:
        try:
            address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
        except (IndexError, ValueError):
            raise OriginalDownloadError("官方图片域名解析结果无效") from None
        if not address.is_global:
            raise OriginalDownloadError("图片地址解析到非公网网络，已拒绝")

    return parts.geturl()


class _OfficialRedirectHandler(HTTPRedirectHandler):
    max_redirections = _MAX_REDIRECTS
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_remote_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_filename(requested: str, source_url: str, extension: str) -> str:
    raw = str(requested or "").strip()
    if not raw:
        raw = unquote(urlsplit(source_url).path.rsplit("/", 1)[-1])
    raw = raw.replace("\\", "_").replace("/", "_")
    raw = "".join("_" if ord(char) < 32 or char in ':*?"<>|' else char for char in raw)
    raw = raw.strip(" .")[:160]
    if not raw:
        raw = "photo"
    if raw.lower().endswith(extension):
        raw = raw[: -len(extension)]
    else:
        # 不信任客户端给出的后缀，最终后缀由已验证的图片类型决定。
        raw = raw.rsplit(".", 1)[0] if "." in raw else raw
    return (raw or "photo") + extension


def _fetch_official_image(url: str) -> tuple[bytes, str, str]:
    checked_url = _validate_remote_url(url)
    request = Request(
        checked_url,
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 (compatible; SakamichiArchive/1.0)",
        },
        method="GET",
    )
    opener = build_opener(_OfficialRedirectHandler())
    try:
        with opener.open(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            _validate_remote_url(response.geturl())
            content_length = response.headers.get("Content-Length", "").strip()
            if content_length:
                try:
                    parsed_length = int(content_length)
                    if parsed_length < 0:
                        raise OriginalDownloadError("原图响应长度无效")
                    if parsed_length > _MAX_IMAGE_BYTES:
                        raise OriginalDownloadError("原图超过 40 MB 下载上限")
                except ValueError:
                    raise OriginalDownloadError("原图响应长度无效") from None
            data = response.read(_MAX_IMAGE_BYTES + 1)
            if len(data) > _MAX_IMAGE_BYTES:
                raise OriginalDownloadError("原图超过 40 MB 下载上限")
            if not data:
                raise OriginalDownloadError("原图内容为空")

            content_type = response.headers.get_content_type().lower()
            image_info = _IMAGE_TYPES.get(content_type)
            if image_info is None and content_type in {"image/jpg", "image/pjpeg"}:
                image_info = _IMAGE_TYPES["image/jpeg"]
            if image_info is None:
                raise OriginalDownloadError("远程响应不是支持的图片格式")
            extension, signature_check = image_info
            if not signature_check(data):
                raise OriginalDownloadError("远程图片内容校验失败")
            return data, content_type, extension
    except OriginalDownloadError:
        raise
    except (HTTPError, URLError, HTTPException, TimeoutError, OSError, ValueError) as exc:
        raise OriginalDownloadError(f"官方原图下载失败：{type(exc).__name__}") from None


def _send_download(handler, data: bytes, content_type: str, filename: str) -> None:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace("\\", "_").replace('"', "_") or "photo.jpg"
    encoded_name = quote(filename.encode("utf-8"), safe="")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header(
        "Content-Disposition",
        f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}',
    )
    handler.send_header("Cache-Control", "private, no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def handle_original_download(handler, sub: str, guard_fn) -> bool:
    """处理 GET /api/archive/download-original?url=... 的同源原图下载。"""
    if sub != "download-original":
        return False
    if not guard_fn(need_admin=False):
        return True

    try:
        query = parse_qs(handler.path.partition("?")[2], keep_blank_values=True, max_num_fields=4)
        source_url = (query.get("url") or [""])[0]
        requested_name = (query.get("filename") or [""])[0]
        if not source_url:
            raise OriginalDownloadError("缺少原图地址")
        data, content_type, extension = _fetch_official_image(source_url)
        filename = _safe_filename(requested_name, source_url, extension)
        _send_download(handler, data, content_type, filename)
    except OriginalDownloadError as exc:
        _send_json_resp(handler, {"ok": False, "errors": [str(exc)]}, 400)
    except ValueError:
        _send_json_resp(handler, {"ok": False, "errors": ["原图下载参数无效"]}, 400)
    return True
