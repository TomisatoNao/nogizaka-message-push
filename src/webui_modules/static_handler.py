"""
src/webui_modules/static_handler.py — WebUI 静态资源与 HTML 模板服务

提供：
  1. 静态 CSS/JS/SVG 白名单文件服务（支持 ETag 条件缓存与 Gzip 压缩）
  2. HTML 页面模板渲染（index.html, archive.html, login.html, 404.html）
  3. 智能 404 错误派发（浏览器页面 vs JSON API 错误）
  4. Gzip 压缩检测与 JSON 响应封装
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent.parent / "webui_static"

INDEX_HTML_PATH = STATIC_DIR / "index.html"
ARCHIVE_HTML_PATH = STATIC_DIR / "archive.html"
LOGIN_HTML_PATH = STATIC_DIR / "login.html"
NOT_FOUND_HTML_PATH = STATIC_DIR / "404.html"

_STATIC_MIME = {
    "theme.css": "text/css",
    "theme.js": "application/javascript",
    "archive.css": "text/css",
    "archive.js": "application/javascript",
    "admin_icon.svg": "image/svg+xml",
    "archive_icon.svg": "image/svg+xml",
}


def _gzip_accepted(value: str) -> bool:
    """按 Accept-Encoding 的 q 值判断是否可返回 gzip。"""
    wildcard_q: float | None = None
    for raw_item in value.lower().split(","):
        parts = [p.strip() for p in raw_item.split(";") if p.strip()]
        if not parts:
            continue
        coding = parts[0]
        quality = 1.0
        for param in parts[1:]:
            if param.startswith("q="):
                try:
                    quality = float(param[2:])
                except ValueError:
                    quality = 0.0
        if coding == "gzip":
            return quality > 0
        if coding == "*":
            wildcard_q = quality
    return bool(wildcard_q and wildcard_q > 0)


def compress_if_supported(handler, data: bytes) -> tuple[bytes, dict[str, str]]:
    """若客户端支持 gzip 且数据大于 512 字节，进行 gzip 压缩并返回 Content-Encoding 头。"""
    accept_encoding = handler.headers.get("Accept-Encoding", "") if hasattr(handler, "headers") and handler.headers else ""
    if _gzip_accepted(accept_encoding) and len(data) > 512:
        try:
            compressed = gzip.compress(data, compresslevel=6)
            if len(compressed) < len(data):
                return compressed, {"Content-Encoding": "gzip"}
        except (gzip.BadGzipFile, OSError, ValueError):
            pass
    return data, {}


def _take_pending_headers(handler) -> tuple[list[tuple[str, str]], list[str]]:
    """消费单次响应专用头，避免 HTTP keep-alive 重复发送 Cookie/登出头。"""
    headers = list(getattr(handler, "_pending_headers", []))
    cookies = list(getattr(handler, "_pending_set_cookies", []))
    handler._pending_headers = []
    handler._pending_set_cookies = []
    return headers, cookies


def send_json(handler, obj: dict, code: int = 200) -> None:
    """发送标准 JSON 响应。"""
    try:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        body, enc_headers = compress_if_supported(handler, body)
        pending_headers, pending_cookies = _take_pending_headers(handler)
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Vary", "Accept-Encoding")
        for k, v in enc_headers.items():
            handler.send_header(k, v)
        for k, v in pending_headers:
            handler.send_header(k, v)
        for sc in pending_cookies:
            handler.send_header("Set-Cookie", sc)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        pass


def send_static(handler, name: str) -> None:
    """主题 CSS / JS / 图标 —— 白名单文件名，不接受任意路径。"""
    ctype = _STATIC_MIME.get(name)
    if ctype is None:
        send_json(handler, {"ok": False, "errors": ["未知静态资源"]}, 404)
        return
    try:
        body = (STATIC_DIR / name).read_bytes()
    except OSError:
        send_json(handler, {"ok": False, "errors": ["静态资源缺失"]}, 404)
        return

    etag = f'"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'
    if handler.headers.get("If-None-Match") == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", "public, max-age=120, must-revalidate")
        handler.send_header("Vary", "Accept-Encoding")
        handler.end_headers()
        return

    body, enc_headers = compress_if_supported(handler, body)
    handler.send_response(200)
    handler.send_header("Content-Type", f"{ctype}; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("ETag", etag)
    handler.send_header("Cache-Control", "public, max-age=120, must-revalidate")
    handler.send_header("Vary", "Accept-Encoding")
    for k, v in enc_headers.items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def send_html(handler, file_path: Path, code: int = 200) -> None:
    """发送 HTML 页面。"""
    try:
        body = file_path.read_bytes()
    except OSError:
        send_json(handler, {"ok": False, "errors": ["页面文件缺失"]}, 500)
        return
    body, enc_headers = compress_if_supported(handler, body)
    pending_headers, pending_cookies = _take_pending_headers(handler)
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Vary", "Accept-Encoding")
    for k, v in enc_headers.items():
        handler.send_header(k, v)
    for k, v in pending_headers:
        handler.send_header(k, v)
    for sc in pending_cookies:
        handler.send_header("Set-Cookie", sc)
    handler.end_headers()
    handler.wfile.write(body)


def send_html_text(handler, title: str, message: str, code: int = 200, redirect_url: str = "") -> None:
    """生成并发送简单的标准 HTML 提示页面。"""
    from html import escape as html_escape
    meta_refresh = f'<meta http-equiv="refresh" content="3;url={html_escape(redirect_url)}">' if redirect_url else ""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <title>{html_escape(title)}</title>
    {meta_refresh}
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f8fafc; color: #1e293b; }}
        .card {{ background: white; padding: 32px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); max-width: 480px; width: 90%; text-align: center; }}
        h1 {{ font-size: 20px; margin-bottom: 12px; }}
        p {{ color: #64748b; font-size: 14px; line-height: 1.5; }}
        a {{ color: #7c3aed; text-decoration: none; font-weight: 500; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>{html_escape(title)}</h1>
        <p>{html_escape(message)}</p>
    </div>
</body>
</html>"""
    body = html.encode("utf-8")
    body, enc_headers = compress_if_supported(handler, body)
    pending_headers, pending_cookies = _take_pending_headers(handler)
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Vary", "Accept-Encoding")
    for k, v in enc_headers.items():
        handler.send_header(k, v)
    for k, v in pending_headers:
        handler.send_header(k, v)
    for sc in pending_cookies:
        handler.send_header("Set-Cookie", sc)
    handler.end_headers()
    handler.wfile.write(body)


def send_404(handler, message: str = "未知路径") -> None:
    """根据客户端 Accept 头智能返回精美 404 HTML 页面或 JSON 错误。"""
    accept = handler.headers.get("Accept", "")
    path = handler.path.split("?", 1)[0]
    if not path.startswith("/api/") and ("text/html" in accept or "*/*" in accept or not accept):
        send_html(handler, NOT_FOUND_HTML_PATH, code=404)
        return
    send_json(handler, {"ok": False, "errors": [message]}, 404)
