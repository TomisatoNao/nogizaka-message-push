"""WebUI handlers for one-off social URL parsing and delivery.

The management page already exposes the two actions in its JavaScript client,
but they must be explicit server routes.  Keeping the handlers in a small
module prevents the HTTP dispatcher from accumulating platform-specific logic
and gives both actions the same validation/error contract.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit
from uuid import uuid4

from src.logger import log_all
from src.webui_modules.static_handler import send_json

_MAX_URL_LENGTH = 4096
_MAX_CHANNELS = 64
_CHANNEL_RE = re.compile(
    r"^(?:tg(?::[-A-Za-z0-9_.]{1,128})?|napcat(?::\d{1,32})?|"
    r"qq_official|official:[A-Za-z0-9_.-]{1,128}(?::(?:private|group))?)$"
)
_SOCIAL_HOSTS = frozenset({
    "instagram.com", "www.instagram.com",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "vxtwitter.com", "www.vxtwitter.com", "fixupx.com", "www.fixupx.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "douyin.com", "www.douyin.com", "v.douyin.com",
})


class SocialToolRequestError(ValueError):
    """Client supplied invalid input."""


def _request_id() -> str:
    return uuid4().hex[:12]


def _error(handler, request_id: str, message: str, *, code: int, error_code: str,
           details: dict | None = None) -> bool:
    payload = {
        "ok": False,
        "request_id": request_id,
        "error_code": error_code,
        "errors": [message],
    }
    if details:
        payload.update(details)
    send_json(handler, payload, code)
    return False


def _validate_url(value) -> str:
    url = str(value or "").strip()
    if not url:
        raise SocialToolRequestError("请先输入社媒链接")
    if len(url) > _MAX_URL_LENGTH:
        raise SocialToolRequestError("社媒链接过长")
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise SocialToolRequestError("社媒链接格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise SocialToolRequestError("社媒链接必须使用 http 或 https")
    if parsed.username or parsed.password or hostname not in _SOCIAL_HOSTS:
        raise SocialToolRequestError("不支持的社媒链接域名")
    return url


def _validate_channels(value) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise SocialToolRequestError("channels 必须是数组")
    if not value:
        raise SocialToolRequestError("请至少选择一个推送目标通道")
    if len(value) > _MAX_CHANNELS:
        raise SocialToolRequestError("推送目标通道数量过多")
    channels: list[str] = []
    for raw in value:
        target = str(raw or "").strip()
        if not target or not _CHANNEL_RE.fullmatch(target):
            raise SocialToolRequestError(f"不支持的推送目标标识: {target or '（空）'}")
        if target not in channels:
            channels.append(target)
    return channels


def _as_bool(value, default: bool = True) -> bool:
    """Parse JSON booleans while tolerating older string-valued clients."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def _public_media(post) -> list[dict[str, str]]:
    media = []
    for item in getattr(post, "media", []) or []:
        media.append({
            "type": str(getattr(item, "type", "image") or "image"),
            "url": str(getattr(item, "url", "") or ""),
            "local_path": os.path.basename(str(getattr(item, "local_path", "") or "")),
        })
    return media


def _public_extra(post, source_url: str) -> dict[str, str]:
    extra = getattr(post, "extra", {}) or {}
    # Only expose stable, non-secret metadata.  In particular, do not return
    # cookies, request headers, or the complete platform response object.
    allowed = ("source", "auth", "kind", "username", "screen_name")
    result = {key: str(extra[key]) for key in allowed if extra.get(key)}
    result["url"] = source_url
    return result


def _post_payload(post, source_url: str, translation: str | None = None) -> dict:
    payload = {
        "ok": True,
        "platform": str(getattr(post, "platform", "") or ""),
        "author": str(getattr(post, "author", "") or ""),
        "text": str(getattr(post, "text", "") or ""),
        "translation": translation,
        "media_count": len(getattr(post, "media", []) or []),
        "media": _public_media(post),
        "timestamp": str(getattr(post, "timestamp", "") or ""),
        "post_id": str(getattr(post, "post_id", "") or ""),
        "extra": _public_extra(post, source_url),
    }
    return payload


def _delivery_payload(result) -> dict | None:
    delivery = result.get("delivery") if isinstance(result, dict) else None
    return delivery if isinstance(delivery, dict) else None


def _handle_exception(handler, request_id: str, action: str, exc: BaseException) -> bool:
    """Map known parser failures without leaking URLs, cookies, or internals."""
    try:
        from src.social.single_fetcher import InstagramAuthRequired
        from src.social.instagram_embed import InstagramEmbedUnavailable
        known_auth = InstagramAuthRequired
        known_embed = InstagramEmbedUnavailable
    except ImportError:  # pragma: no cover - imports are present in production
        known_auth = RuntimeError
        known_embed = RuntimeError

    if isinstance(exc, SocialToolRequestError):
        return _error(handler, request_id, str(exc), code=400, error_code="invalid_request")
    if isinstance(exc, known_auth):
        return _error(handler, request_id, "Instagram 内容需要有效登录 Cookies（Story 不支持匿名解析）", code=422,
                      error_code="instagram_auth_required")
    if isinstance(exc, known_embed):
        return _error(handler, request_id, "Instagram 公开内容暂时无法解析，请稍后重试或检查链接权限", code=502,
                      error_code="instagram_unavailable")
    if isinstance(exc, (TimeoutError,)):  # explicit before the generic runtime mapping
        return _error(handler, request_id, "社媒解析超时，请稍后重试", code=504, error_code="upstream_timeout")
    if isinstance(exc, OSError):
        return _error(handler, request_id, "社媒媒体服务暂时不可用，请查看系统日志", code=502,
                      error_code="upstream_unavailable")
    if isinstance(exc, RuntimeError):
        return _error(handler, request_id, "社媒链接解析或推送失败，请查看系统日志", code=502,
                      error_code="social_operation_failed")

    log_all(
        f"🚨 [社媒工具] {action} 未处理异常 | request_id={request_id} | error={type(exc).__name__}",
        is_error=True,
    )
    return _error(handler, request_id, "社媒工具发生内部错误，请查看系统日志", code=500, error_code="internal_error")


def handle_parse_post(handler, body, *, load_raw_config) -> bool:
    """POST /api/social/parse_post：只解析，不下载、不推送。"""
    request_id = _request_id()
    try:
        if not isinstance(body, dict):
            raise SocialToolRequestError("请求体必须是 JSON 对象")
        url = _validate_url(body.get("url"))
        raw_config = load_raw_config()
        if not isinstance(raw_config, dict):
            raise RuntimeError("配置不是对象")

        from src.social.forwarder import SocialForwarder
        from src.social.single_fetcher import SocialUrlParser

        log_all(f"🔎 [社媒工具] 开始解析 | request_id={request_id}", is_debug=True)
        post = SocialUrlParser(raw_config).parse(url)
        translation = None
        if _as_bool(body.get("translate"), True) and getattr(post, "text", ""):
            translation = SocialForwarder(raw_config)._translate(post.text)
        payload = _post_payload(post, url, translation)
        payload["request_id"] = request_id
        log_all(
            f"✅ [社媒工具] 解析完成 | request_id={request_id} | platform={post.platform} "
            f"| media={len(post.media)}",
            is_debug=True,
        )
        send_json(handler, payload)
        return True
    except Exception as exc:
        return _handle_exception(handler, request_id, "parse_post", exc)


def handle_manual_push(handler, body, *, load_raw_config) -> bool:
    """POST /api/social/manual_push：解析、下载、定向推送并按需归档。"""
    request_id = _request_id()
    try:
        if not isinstance(body, dict):
            raise SocialToolRequestError("请求体必须是 JSON 对象")
        url = _validate_url(body.get("url"))
        channels = _validate_channels(body.get("channels"))
        raw_config = load_raw_config()
        if not isinstance(raw_config, dict):
            raise RuntimeError("配置不是对象")

        from src.social.single_fetcher import manual_push_social_url

        log_all(f"🚀 [社媒工具] 开始手动推送 | request_id={request_id}", is_debug=True)
        result = manual_push_social_url(
            url,
            raw_config,
            target_channels=channels,
            translate=_as_bool(body.get("translate"), True),
            archive=_as_bool(body.get("archive"), True),
        )
        if not isinstance(result, dict):
            raise RuntimeError("推送器返回格式无效")
        result = dict(result)
        result["request_id"] = request_id
        delivery = _delivery_payload(result)
        if delivery:
            outcome = str(delivery.get("outcome") or "")
            if outcome == "no_route":
                return _error(handler, request_id, "未找到匹配的推送路由，请检查通道是否启用", code=422,
                              error_code="no_matching_route", details={"delivery": delivery})
            if outcome in {"failed", "partial", "error"}:
                return _error(handler, request_id, "推送未完整送达，请查看系统日志并重试", code=502,
                              error_code="delivery_failed", details={
                                  "platform": result.get("platform", ""),
                                  "media_count": result.get("media_count", 0),
                                  "delivery": delivery,
                              })
        log_all(
            f"✅ [社媒工具] 手动推送完成 | request_id={request_id} "
            f"| media={result.get('media_count', 0)}",
            is_debug=True,
        )
        send_json(handler, result)
        return True
    except Exception as exc:
        return _handle_exception(handler, request_id, "manual_push", exc)


__all__ = ["handle_parse_post", "handle_manual_push"]
