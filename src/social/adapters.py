"""社交投递通道适配器。

适配器只知道如何调用对应平台的发送 API；路由匹配、重试状态和归档均由
``RoutePlanner`` / ``DeliveryService`` 负责。每个适配器同时提供细粒度的
``send_text`` / ``send_media`` 协议方法，以及 ``send_post`` 以保留旧版
NapCat 消息链的一次性发送语义。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from src.platforms import napcat
from src.social.contracts import DeliveryTarget
from src.social.models import MediaItem


@dataclass(frozen=True)
class OfficialTarget:
    """QQ 官方 Bot 的私聊/群聊目标描述。"""

    bot: Any
    scope: str | None = None
    target_id: str = ""
    send_private: bool = False
    send_group: bool = False
    allow_missing_media: bool = False


@runtime_checkable
class ChannelAdapter(Protocol):
    """所有实际发送通道必须实现的最小协议。"""

    async def send_text(self, target: DeliveryTarget, text: str) -> bool:
        ...

    async def send_media(self, target: DeliveryTarget, media: MediaItem) -> bool:
        ...


def _target_value(target: DeliveryTarget) -> Any:
    if isinstance(target, DeliveryTarget):
        return target.runtime if target.runtime is not None else target
    return target


def _media_type(media: MediaItem) -> str:
    if media.type == "image":
        return "image"
    if media.type == "video":
        return "video"
    if media.type == "audio":
        return "record"
    return "image"


class TelegramAdapter:
    """Telegram Bot 发送适配器。"""

    def __init__(self, logger: Callable[..., None] | None = None):
        self._log = logger or (lambda *_args, **_kwargs: None)

    async def send_text(self, target: DeliveryTarget, text: str) -> bool:
        bot = _target_value(target)
        return bool(await bot._post_message(bot.target_chat, text))

    async def send_media(self, target: DeliveryTarget, media: MediaItem) -> bool:
        bot = _target_value(target)
        path = media.local_path
        if not path or not os.path.exists(path):
            # 旧版 Telegram 路径对缺少本地文件采取“跳过”，保持兼容。
            return True
        if media.type not in {"image", "video", "audio"}:
            return True

        try:
            with open(path, "rb") as media_file:
                if media.type == "image":
                    await bot._bot.send_photo(
                        chat_id=bot.target_chat,
                        photo=media_file,
                    )
                elif media.type == "video":
                    await bot._bot.send_video(
                        chat_id=bot.target_chat,
                        video=media_file,
                    )
                else:
                    await bot._bot.send_audio(
                        chat_id=bot.target_chat,
                        audio=media_file,
                    )
            return True
        except (OSError, ValueError) as exc:
            self._log(
                f"⚠️ TG Bot 发送{media.type}失败: {type(exc).__name__}",
                is_error=True,
            )
            return False

    async def send_post(
        self,
        target: DeliveryTarget,
        text: str,
        media: list[MediaItem],
    ) -> bool:
        if not await self.send_text(target, text):
            return False
        for item in media:
            if not await self.send_media(target, item):
                return False
        return True


class NapCatAdapter:
    """NapCat / OneBot11 群消息适配器。"""

    def __init__(self, logger: Callable[..., None] | None = None):
        self._log = logger or (lambda *_args, **_kwargs: None)

    @staticmethod
    def _group_id(target: DeliveryTarget) -> Any:
        route = _target_value(target)
        return route.get("group_id") if isinstance(route, Mapping) else route

    @staticmethod
    def _chain_item(media: MediaItem) -> dict | None:
        path = media.local_path
        if not path or not os.path.exists(path):
            return None
        file_uri = "file:///" + os.path.abspath(path).replace("\\", "/")
        kind = {
            "image": "image",
            "video": "video",
            "audio": "record",
        }.get(media.type)
        if not kind:
            return None
        return {"type": kind, "data": {"file": file_uri}}

    async def send_text(self, target: DeliveryTarget, text: str) -> bool:
        try:
            return bool(
                await napcat.send_qq_message(
                    self._group_id(target),
                    [{"type": "text", "data": {"text": text}}],
                )
            )
        except (OSError, ValueError) as exc:
            self._log(
                f"⚠️ NapCat 发送文本失败: {type(exc).__name__}",
                is_error=True,
            )
            return False

    async def send_media(self, target: DeliveryTarget, media: MediaItem) -> bool:
        item = self._chain_item(media)
        if item is None:
            # 与旧版广播一致：媒体文件尚未落地时仍允许正文路由成功。
            return True
        try:
            return bool(
                await napcat.send_qq_message(
                    self._group_id(target),
                    [item],
                )
            )
        except (OSError, ValueError) as exc:
            self._log(
                f"⚠️ NapCat 发送媒体失败: {type(exc).__name__}",
                is_error=True,
            )
            return False

    async def send_post(
        self,
        target: DeliveryTarget,
        text: str,
        media: list[MediaItem],
    ) -> bool:
        chain = [{"type": "text", "data": {"text": text}}]
        for item in media:
            chain_item = self._chain_item(item)
            if chain_item:
                chain.append(chain_item)
        try:
            return bool(await napcat.send_qq_message(self._group_id(target), chain))
        except (OSError, ValueError) as exc:
            self._log(
                f"⚠️ NapCat 发送动态失败: {type(exc).__name__}",
                is_error=True,
            )
            return False


class QQOfficialAdapter:
    """QQ 官方 Bot 私聊/群聊适配器。"""

    def __init__(self, logger: Callable[..., None] | None = None):
        self._log = logger or (lambda *_args, **_kwargs: None)

    @staticmethod
    def _targets(target: DeliveryTarget) -> list[tuple[str, str]]:
        descriptor = _target_value(target)
        if isinstance(descriptor, OfficialTarget):
            if descriptor.scope and descriptor.target_id:
                return [(descriptor.scope, descriptor.target_id)]
            out = []
            if descriptor.send_private and getattr(descriptor.bot, "target_openid", None):
                out.append(("users", descriptor.bot.target_openid))
            if descriptor.send_group and getattr(descriptor.bot, "group_openid", None):
                out.append(("groups", descriptor.bot.group_openid))
            return out

        # 兜底：直接从 target 领域的 scope 与 target_id 读取
        if target.scope and target.target_id:
            return [(target.scope, target.target_id)]

        # 兜底：descriptor 是 Bot 实例
        bot = descriptor
        out = []
        if getattr(bot, "target_openid", None):
            out.append(("users", str(bot.target_openid)))
        if getattr(bot, "group_openid", None):
            out.append(("groups", str(bot.group_openid)))
        return out

    @staticmethod
    def _bot(target: DeliveryTarget) -> Any:
        descriptor = _target_value(target)
        if isinstance(descriptor, OfficialTarget):
            return descriptor.bot
        if descriptor is not None and descriptor is not target:
            return descriptor
        # 从 target.bot_name 查找已配置的 bot
        from src.platforms import qq_official
        bots = qq_official.get_configured_bots()
        if target.bot_name:
            for b in bots:
                if getattr(b, "name", "") == target.bot_name:
                    return b
        return bots[0] if bots else None

    async def send_text(self, target: DeliveryTarget, text: str) -> bool:
        bot = self._bot(target)
        if not bot:
            return False
        targets = self._targets(target)
        if not targets:
            return False
        results = []
        for scope, target_id in targets:
            if scope == "groups":
                results.append(bool(await bot.send_group_text(target_id, text)))
            else:
                results.append(bool(await bot.send_private_text(target_id, text)))
        return all(results)

    async def send_media(self, target: DeliveryTarget, media: MediaItem) -> bool:
        path = media.local_path
        if not path or not os.path.exists(path):
            descriptor = _target_value(target)
            return bool(
                getattr(descriptor, "allow_missing_media", False)
            )
        try:
            with open(path, "rb") as media_file:
                content = media_file.read()
            if not content:
                return False
            bot = self._bot(target)
            if not bot:
                return False
            targets = self._targets(target)
            if not targets:
                return False
            results = []
            for scope, target_id in targets:
                results.append(
                    bool(
                        await bot.send_media_file(
                            scope,
                            target_id,
                            _media_type(media),
                            content,
                            filename=os.path.basename(path),
                        )
                    )
                )
            return all(results)
        except Exception as exc:
            self._log(
                f"⚠️ QQ 官方 Bot 发送媒体失败: {type(exc).__name__}",
                is_error=True,
            )
            return False

    async def send_post(
        self,
        target: DeliveryTarget,
        text: str,
        media: list[MediaItem],
    ) -> bool:
        if not await self.send_text(target, text):
            return False
        for item in media:
            if not await self.send_media(target, item):
                return False
        return True


__all__ = [
    "ChannelAdapter",
    "DeliveryTarget",
    "NapCatAdapter",
    "OfficialTarget",
    "QQOfficialAdapter",
    "TelegramAdapter",
]
