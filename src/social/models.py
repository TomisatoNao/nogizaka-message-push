"""Unified data models for cross-platform idol activity tracking."""
from dataclasses import dataclass, field
from typing import Any

from src.social.contracts import DeliveryResult, DeliveryTarget, SocialDeliveryResult


@dataclass
class MediaItem:
    type: str          # "image" | "video" | "audio" | "file"
    url: str           # original remote URL
    local_path: str = ""  # absolute local path after download
    alt_text: str = "" # image description / alt text (e.g. from X)


@dataclass(frozen=True)
class PreparedSocialPost:
    """准备阶段产物，供多个投递入口复用。"""

    translated: str | None
    alt_translations: dict[int, str]
    full_text: str


@dataclass
class Post:
    platform: str      # "melink" | "joylink" | "showroom" | "x" | "instagram" | "tiktok"
    post_id: str       # platform-unique identifier
    author: str        # display name
    text: str = ""     # text content (may be empty for media-only posts)
    media: list[MediaItem] = field(default_factory=list)
    timestamp: str = ""  # human-readable time string
    extra: dict[str, Any] = field(default_factory=dict)  # platform-specific metadata
    # 统一链路标识；旧构造调用不传入时保持空字符串。
    request_id: str = ""


__all__ = [
    "DeliveryResult",
    "DeliveryTarget",
    "MediaItem",
    "Post",
    "PreparedSocialPost",
    "SocialDeliveryResult",
]
