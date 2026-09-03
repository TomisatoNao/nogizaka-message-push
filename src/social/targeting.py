"""把旧版通道选择值转换为领域目标。

这是输入边界适配器，不属于核心投递服务。核心层只接收
``DeliveryTarget``，因此这里是项目中唯一允许解析
``official:bot1:private`` 等旧字符串的位置。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.social.contracts import DeliveryTarget


_TARGET_RE = re.compile(
    r"^(?:tg(?::[-A-Za-z0-9_.]{1,128})?|napcat(?::\d{1,32})?|"
    r"qq_official|official:[A-Za-z0-9_.-]{1,128}(?::(?:private|group))?)$"
)


class DeliveryTargetInputError(ValueError):
    """输入层目标标识无效。"""


def from_legacy_target(value: str) -> DeliveryTarget:
    """转换一个旧版字符串目标，不在核心层调用。"""
    raw = str(value or "").strip()
    if not _TARGET_RE.fullmatch(raw):
        raise DeliveryTargetInputError(
            f"不支持的推送目标标识: {raw or '（空）'}"
        )

    if raw == "tg":
        return DeliveryTarget(channel="tg", target_id="")
    if raw.startswith("tg:"):
        value = raw[3:]
        if value.startswith("-") or value.isdigit():
            return DeliveryTarget(channel="tg", target_id=value)
        return DeliveryTarget(channel="tg", target_id="", bot_name=value)
    if raw == "napcat":
        return DeliveryTarget(channel="napcat", target_id="")
    if raw.startswith("napcat:"):
        return DeliveryTarget(
            channel="napcat",
            target_id=raw.split(":", 1)[1],
            scope="groups",
        )
    if raw == "qq_official":
        return DeliveryTarget(channel="qq_official", target_id="")

    _, bot_name, *scope = raw.split(":")
    return DeliveryTarget(
        channel="qq_official",
        target_id="",
        scope={"private": "users", "group": "groups"}.get(scope[0], "")
        if scope
        else "",
        bot_name=bot_name,
    )


def normalize_delivery_targets(
    values: Iterable[DeliveryTarget | str] | None,
    *,
    allow_legacy: bool = True,
) -> list[DeliveryTarget] | None:
    """规范化输入边界目标并去重。

    ``allow_legacy`` 只给 WebUI/旧兼容包装器使用；核心服务传入
    ``allow_legacy=False``，这样任何字符串都会立即被拒绝。
    """
    if values is None:
        return None
    out: list[DeliveryTarget] = []
    for value in values:
        if isinstance(value, DeliveryTarget):
            target = value
        elif allow_legacy and isinstance(value, str):
            target = from_legacy_target(value)
        else:
            raise DeliveryTargetInputError("推送目标必须是 DeliveryTarget")
        if target not in out:
            out.append(target)
    return out


__all__ = [
    "DeliveryTargetInputError",
    "from_legacy_target",
    "normalize_delivery_targets",
]
