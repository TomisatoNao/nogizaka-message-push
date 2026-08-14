"""Unified data models for cross-platform idol activity tracking."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MediaItem:
    type: str          # "image" | "video" | "audio" | "file"
    url: str           # original remote URL
    local_path: str = ""  # absolute local path after download
    alt_text: str = "" # image description / alt text (e.g. from X)


@dataclass
class Post:
    platform: str      # "melink" | "joylink" | "showroom" | "x" | "instagram" | "tiktok"
    post_id: str       # platform-unique identifier
    author: str        # display name
    text: str = ""     # text content (may be empty for media-only posts)
    media: list[MediaItem] = field(default_factory=list)
    timestamp: str = ""  # human-readable time string
    extra: dict[str, Any] = field(default_factory=dict)  # platform-specific metadata
