"""
src/webui_modules/media_service.py — 媒体流 HTTP 206 Range 分片服务

为视频、音频拖动进度条以及高清图片分片提供标准 HTTP 206 Partial Content 支持，
支持 ETag 304 条件缓存、Range 边界校验与 416 范围溢出保护。
"""

from email.utils import formatdate, parsedate_to_datetime
import mimetypes
from pathlib import Path
import re


def serve_file_range(handler, path: Path) -> None:
    """为 HTTP 请求处理器提供媒体文件流式分片服务。"""
    st = path.stat()
    size = st.st_size
    etag = f'"{int(st.st_mtime)}-{size:x}"'
    last_modified = formatdate(st.st_mtime, usegmt=True)
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

    # 条件请求（无 Range 时才处理 304）
    if not handler.headers.get("Range"):
        fresh = False
        inm = handler.headers.get("If-None-Match", "")
        if inm:
            fresh = any(t.strip().lstrip("W/") == etag for t in inm.split(","))
        elif handler.headers.get("If-Modified-Since"):
            try:
                since = parsedate_to_datetime(handler.headers["If-Modified-Since"]).timestamp()
                fresh = int(st.st_mtime) <= int(since)
            except (TypeError, ValueError):
                fresh = False
        if fresh:
            handler.send_response(304)
            handler.send_header("ETag", etag)
            handler.send_header("Cache-Control", "private, no-cache")
            handler.end_headers()
            return

    start, end, status = 0, size - 1, 200
    m = re.match(r"bytes=(\d*)-(\d*)$", handler.headers.get("Range", "").strip())
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
        else:
            start = max(0, size - int(m.group(2)))
        if start >= size or start > end:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.end_headers()
            return
        status = 206
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(end - start + 1))
    if status == 206:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("ETag", etag)
    handler.send_header("Last-Modified", last_modified)
    handler.send_header("Cache-Control", "private, no-cache")
    handler.end_headers()
    try:
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (ConnectionError, OSError):
        pass
