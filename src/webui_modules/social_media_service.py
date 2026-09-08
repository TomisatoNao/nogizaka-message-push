"""受控的社交媒体文件分发，供远程 NapCat 读取 NAS 媒体。"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

import config.config as cfg
from src.webui_modules.media_service import serve_file_range

_PROCESS_SECRET = secrets.token_bytes(32)
_MAX_URL_AGE = 600


def _root() -> Path:
    # 不依赖进程启动时的 cwd；部署脚本、systemd 和 Docker 的 cwd 可能不同。
    # 此模块位于 <project>/src/webui_modules/，因此 parents[2] 始终是项目根。
    return (Path(__file__).resolve().parents[2] / "data" / "social_media").resolve()


def _secret() -> bytes:
    raw = (getattr(cfg, "NAPCAT_MEDIA_SIGNING_SECRET", "") or os.getenv("NAPCAT_MEDIA_SIGNING_SECRET", "")
           or os.getenv("WEB_ADMIN_TOKEN", ""))
    return raw.encode("utf-8") if raw else _PROCESS_SECRET


def _signature(relative_path: str, expires: int) -> str:
    payload = f"{relative_path}\n{expires}".encode("utf-8")
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def _relative_media_path(local_path: str) -> str | None:
    if not local_path:
        return None
    root = _root()
    candidate = Path(local_path).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return relative.as_posix()


def build_napcat_media_uri(local_path: str) -> str | None:
    """将 NAS 本地媒体转换为 NapCat 可访问的短时效 HTTP URL。"""
    base = str(getattr(cfg, "NAPCAT_MEDIA_BASE_URL", "") or os.getenv("NAPCAT_MEDIA_BASE_URL", "")).strip().rstrip("/")
    relative = _relative_media_path(local_path)
    if not base or not relative:
        return None
    expires = int(time.time()) + 300
    encoded = "/".join(quote(part) for part in relative.split("/"))
    return f"{base}/api/social/media/{encoded}?expires={expires}&sig={_signature(relative, expires)}"


def serve_signed_social_media(handler, request_path: str) -> bool:
    """校验签名并分发社媒媒体；返回是否命中该路由。"""
    prefix = "/api/social/media/"
    if not request_path.startswith(prefix):
        return False
    relative = unquote(request_path[len(prefix):]).replace("\\", "/")
    query = parse_qs(urlsplit(handler.path).query)
    try:
        expires = int((query.get("expires") or [""])[0])
    except ValueError:
        expires = 0
    supplied = (query.get("sig") or [""])[0]
    valid_time = int(time.time()) <= expires <= int(time.time()) + _MAX_URL_AGE
    if not valid_time or not hmac.compare_digest(supplied, _signature(relative, expires)):
        handler.send_error(403, "Invalid or expired media URL")
        return True
    root = _root()
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        handler.send_error(404, "Media not found")
        return True
    serve_file_range(handler, target)
    return True


__all__ = ["build_napcat_media_uri", "serve_signed_social_media"]
