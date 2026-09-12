"""社媒链接识别与安全校验。

网页端单条解析工具和 NapCat 群消息入口共享同一套 URL 边界。这样新增
事件入口时不会因为复制一份域名白名单而产生行为漂移，也能在进入解析器
之前拒绝任意 URL，避免把群消息入口变成 SSRF 代理。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


SOCIAL_HOSTS = frozenset({
    "instagram.com", "www.instagram.com",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "vxtwitter.com", "www.vxtwitter.com", "fixupx.com", "www.fixupx.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "douyin.com", "www.douyin.com", "v.douyin.com",
})

# 不使用 ``\b``：Python 将中文视为 word 字符，导致“看这里https://…”
# 这类常见群消息无法命中。URL 本身只扫描 ASCII URI 字符，避免把紧随
# 链接的中文说明文字也吞进路径；括号/中日韩标点在后面统一裁剪。
_URL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])https?://[A-Za-z0-9][A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]*"
)
_TRAILING_PUNCTUATION = ".,!?;:)]}，。！？；：）》」』】"
MAX_URL_LENGTH = 4096


class SocialUrlValidationError(ValueError):
    """社媒 URL 不符合允许的输入边界。"""


def validate_social_url(value: object) -> str:
    """校验并返回干净的社媒 URL。

    只允许明确列出的社媒域名、HTTP(S) 协议和不带用户信息的 URL。
    解析器本身仍会继续校验具体平台路径；本函数负责入口层安全边界。
    """

    url = str(value or "").strip()
    if not url:
        raise SocialUrlValidationError("请先输入社媒链接")
    if len(url) > MAX_URL_LENGTH:
        raise SocialUrlValidationError("社媒链接过长")
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise SocialUrlValidationError("社媒链接格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise SocialUrlValidationError("社媒链接必须使用 http 或 https")
    if parsed.username or parsed.password or hostname not in SOCIAL_HOSTS:
        raise SocialUrlValidationError("不支持的社媒链接域名")
    return url


def extract_social_urls(value: object, *, max_urls: int = 1) -> tuple[str, ...]:
    """从群消息文本中提取去重后的允许社媒链接。

    发现未知域名或损坏 URL 时静默跳过；群消息入口不应因为一段普通文本
    触发错误回复。调用方可通过 ``max_urls`` 设置单条消息的处理上限。
    """

    try:
        limit = max(0, min(16, int(max_urls)))
    except (TypeError, ValueError):
        limit = 1
    if limit == 0:
        return ()

    text = str(value or "")
    if not text:
        return ()

    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if not candidate:
            continue
        try:
            clean = validate_social_url(candidate)
            parsed = urlsplit(clean)
            key = clean.rstrip("/").lower()
            # URL 的查询参数可能包含大小写不同的追踪参数；这里仅做精确
            # 小写去重，不擅自删除参数，避免改变平台短链语义。
            if parsed.hostname:
                key = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}{parsed.query and '?' + parsed.query or ''}"
        except SocialUrlValidationError:
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(clean)
        if len(found) >= limit:
            break
    return tuple(found)


__all__ = [
    "MAX_URL_LENGTH",
    "SOCIAL_HOSTS",
    "SocialUrlValidationError",
    "extract_social_urls",
    "validate_social_url",
]
