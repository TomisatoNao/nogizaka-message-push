"""社交平台推送兼容门面。

正文准备和多通道动态投递分别由 ``MessagePreparationService`` 与
``DeliveryService`` 承担。本类保留项目既有的同步/异步调用 API，并负责直播
录制完成通知这一种不属于 ``Post`` 的特殊消息。
"""

from __future__ import annotations

import logging
import os
import subprocess  # nosec B404 - controlled local ffmpeg invocation

from src.logger import log_all
from src.platforms import napcat, qq_official, tgbot
from src.social.delivery import SocialDeliveryDispatcher
from src.social.delivery_service import DeliveryService
from src.social.formatter import build_live_end_message
from src.social.contracts import DeliveryResult, SocialDeliveryResult
from src.social.models import Post, PreparedSocialPost
from src.social.preparation import MessagePreparationService
from src.social.qq_delivery import QQDirectDelivery
from src.social.settings import RuntimeConfig, social_settings
from src.social.targeting import normalize_delivery_targets

log = logging.getLogger("social.forwarder")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class SendFailed(RuntimeError):
    """发送失败 —— 向上抛出，让调度器不标记已同步。"""


class SocialForwarder:
    """社交平台推送兼容门面。"""

    def __init__(self, config: dict, downloader=None, store=None):
        self._config = config
        self._dl = downloader
        self._store = store
        self._last_delivery_result: DeliveryResult | None = None
        self._runtime = RuntimeConfig(config)

        # 用闭包动态查找模块级 log_all：既保持统一日志格式，也兼容现有测试
        # 和插件在运行时替换 ``src.social.forwarder.log_all`` 的行为。
        def logger(content, **kwargs):
            log_all(content, **kwargs)
        self._preparation = MessagePreparationService(
            config,
            logger=logger,
        )
        self._delivery_service = DeliveryService(
            config,
            config_view=self._runtime,
            store=store,
            logger=logger,
        )
        # 这两个属性是旧扩展的兼容入口；它们共享同一个 DeliveryService，
        # 不会重新创建路由、状态或适配器。
        self._dispatcher = SocialDeliveryDispatcher(
            store=store,
            logger=logger,
            config_view=self._runtime,
            delivery_service=self._delivery_service,
        )
        self._qq_direct = QQDirectDelivery(
            logger=logger,
            delivery_service=self._delivery_service,
        )

    @property
    def last_delivery_result(self) -> DeliveryResult | None:
        """最近一次动态投递结果。"""
        return self._last_delivery_result

    @property
    def _cfg(self) -> dict:
        return social_settings(self._config)

    @property
    def delivery_service(self) -> DeliveryService:
        """返回统一投递服务，供新入口直接复用。"""
        return self._delivery_service

    @property
    def preparation_service(self) -> MessagePreparationService:
        return self._preparation

    # ── 准备阶段兼容 API ─────────────────────────────────

    def _translate(self, text: str) -> str | None:
        """保留旧翻译方法名，实际调用 TranslationService。"""
        return self._preparation.translation.translate(text)

    def prepare_post(
        self,
        post: Post,
        *,
        translate: bool = True,
    ) -> PreparedSocialPost:
        return self._preparation.prepare(post, translate=translate)

    def archive_post(self, post: Post) -> bool:
        return self._delivery_service.archive_service.archive(post)

    # ── 多通道统一广播 ───────────────────────────────────

    def _dispatch_async(self, coro):
        """保留旧辅助方法；同步执行交给 DeliveryService。"""
        return self._delivery_service._dispatch_async(coro)

    def forward_post(
        self,
        post: Post,
        target_channels: list[str] | None = None,
        *,
        archive: bool = True,
        prepared: PreparedSocialPost | None = None,
    ) -> bool:
        """推送一条动态，返回是否已安全完成。"""
        self._last_delivery_result = None
        prepared = prepared or self.prepare_post(
            post,
            translate=not bool(post.extra.get("_skip_translate", False)),
        )
        targets = normalize_delivery_targets(target_channels, allow_legacy=True)
        result = self._delivery_service.deliver_post(
            post,
            prepared.full_text,
            targets,
            archive=archive,
        )
        self._last_delivery_result = result
        return result.complete

    async def send_qq_target(
        self,
        post: Post,
        bot,
        scope: str,
        target_id: str,
        *,
        prepared: PreparedSocialPost | None = None,
        archive: bool = True,
    ) -> DeliveryResult:
        """向 QQ 官方 Bot 指定 OpenID 投递一条已准备动态。"""
        self._last_delivery_result = None
        result = await self._qq_direct.send(
            post,
            bot,
            scope,
            target_id,
            prepared=prepared,
            prepare=self.prepare_post,
            archive=archive,
            archive_callback=self.archive_post,
        )
        self._last_delivery_result = result
        return result

    # ── 直播录制完成通知（非 Post 特殊路径）───────────────

    def send_recording(self, result) -> None:
        """发送「直播录制完成」通知。"""
        result.delivery_succeeded = False
        msg = build_live_end_message(
            author=result.display_name,
            start_time=getattr(result, "start_str", ""),
            end_time=getattr(result, "end_str", ""),
            duration=getattr(result, "duration_str", ""),
            size=getattr(result, "size_str", ""),
            save_path=result.output_dir,
            part_count=len(result.parts),
            note=result.note,
        )

        acc_name = getattr(result, "account", "") or result.display_name

        async def _do_send():
            if self._runtime.channel_enabled("tg"):
                for bot in tgbot.get_configured_bots():
                    if not bot.target_chat or not getattr(bot, "push_live", True):
                        continue
                    if bot.social_filter and acc_name not in bot.social_filter and (
                        result.display_name not in bot.social_filter
                    ):
                        continue
                    await bot._post_message(bot.target_chat, msg)

            if self._runtime.channel_enabled("napcat"):
                for route in self._runtime.list("NAPCAT_ROUTES"):
                    group_id = route.get("group_id")
                    if not group_id or not route.get("push_live", True):
                        continue
                    social_filter = route.get("social_filter") or []
                    if social_filter and acc_name not in social_filter and (
                        result.display_name not in social_filter
                    ):
                        continue
                    await napcat.send_qq_message(
                        group_id,
                        [{"type": "text", "data": {"text": msg}}],
                    )

            if self._runtime.channel_enabled("qq_official"):
                for bot in qq_official.get_configured_bots():
                    if not getattr(bot, "push_live", True):
                        continue
                    if bot.social_filter and acc_name not in bot.social_filter and (
                        result.display_name not in bot.social_filter
                    ):
                        continue
                    if bot.target_openid:
                        await bot.send_private_text(bot.target_openid, msg)
                    if getattr(bot, "group_openid", None):
                        await bot.send_group_text(bot.group_openid, msg)

        try:
            self._dispatch_async(_do_send())
            result.delivery_succeeded = True
            log_all(
                f"✅ [直播录制] 录制完成通知已分发: {result.display_name}",
                is_debug=True,
            )
        except Exception as exc:
            log_all(
                f"🔥 [直播录制] 录制通知分发失败: {type(exc).__name__}",
                is_error=True,
            )


__all__ = [
    "DeliveryResult",
    "PreparedSocialPost",
    "SendFailed",
    "SocialDeliveryResult",
    "SocialForwarder",
]
