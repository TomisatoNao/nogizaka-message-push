"""社媒统一业务服务。

所有社媒入口都经由本模块完成「解析 → 准备 → 投递 → 归档」编排。核心接口
只接受 ``DeliveryTarget``，旧版字符串只允许在输入适配层转换。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from src.social.contracts import DeliveryResult, DeliveryTarget
from src.social.delivery_service import DeliveryService
from src.social.downloader import MediaDownloader
from src.social.errors import (
    SocialAuthRequired,
    SocialDownloadError,
    SocialError,
    SocialParseError,
    SocialTranslationError,
)
from src.social.models import Post, PreparedSocialPost
from src.social.preparation import MessagePreparationService


@dataclass(frozen=True)
class SocialOperationResult:
    """一次社媒操作的统一结果。"""

    post: Post
    delivery: DeliveryResult | None = None
    # 兼容旧 WebUI/插件读取准备详情；核心入口以 post.extra 中的准备结果为准。
    prepared: PreparedSocialPost | None = None
    archived: bool = False
    completed: bool = False


def delivery_to_dict(result: DeliveryResult | None) -> dict | None:
    """把内部投递结果转换为 WebUI/日志可用的稳定字典。"""
    if result is None:
        return None
    payload = {
        "outcome": result.outcome,
        "matched_routes": result.matched_routes,
        "attempted_routes": result.attempted_routes,
        "success_routes": result.success_routes,
        "failed_routes": result.failed_routes,
        "skipped_routes": result.skipped_routes,
        "errors": list(result.errors),
    }
    if result.media_total:
        payload.update(
            media_sent=result.media_sent,
            media_total=result.media_total,
        )
    return payload


class SocialService:
    """社媒应用层门面，统一所有入口的生命周期和依赖。"""

    def __init__(
        self,
        config: dict,
        *,
        parser=None,
        downloader: MediaDownloader | None = None,
        forwarder=None,
        store=None,
        preparation: MessagePreparationService | None = None,
        delivery_service: DeliveryService | None = None,
    ):
        self._config = config
        self._parser = parser
        self._downloader = downloader or getattr(forwarder, "_dl", None) or MediaDownloader(config)
        self._preparation = preparation or getattr(
            forwarder, "preparation_service", None
        ) or MessagePreparationService(config)
        self._delivery_service = delivery_service or getattr(
            forwarder, "delivery_service", None
        ) or DeliveryService(config, store=store)
        # 只为现有插件读取属性保留；核心流程不再委托 Forwarder 编排。
        self._forwarder = forwarder
        self._legacy_forwarder = (
            forwarder
            if forwarder is not None
            and not hasattr(forwarder, "delivery_service")
            else None
        )

    @property
    def downloader(self) -> MediaDownloader:
        return self._downloader

    @property
    def forwarder(self):
        return self._forwarder

    @property
    def preparation_service(self) -> MessagePreparationService:
        return self._preparation

    @property
    def delivery_service(self) -> DeliveryService:
        return self._delivery_service

    def parse_url(self, url: str, *, request_id: str = "") -> Post:
        """只解析单条社媒链接，不下载、翻译或投递。"""
        parser = self._parser
        if parser is None:
            from src.social.single_fetcher import SocialUrlParser

            parser = SocialUrlParser(self._config)
        try:
            post = parser.parse(url)
        except SocialError:
            raise
        except Exception as exc:
            name = type(exc).__name__
            if name in {"InstagramAuthRequired", "InstagramSessionRejected"}:
                raise SocialAuthRequired(
                    "社交内容需要有效登录态", request_id=request_id
                ) from exc
            raise SocialParseError(
                "社交链接解析失败", request_id=request_id
            ) from exc
        if not isinstance(post, Post):
            raise SocialParseError(
                "解析器返回了无效动态对象", request_id=request_id
            )
        post.request_id = request_id or post.request_id
        return post

    def _download_post(self, post: Post) -> None:
        try:
            self._downloader.download(post)
        except SocialError:
            raise
        except Exception as exc:
            raise SocialDownloadError(
                "社交媒体下载失败",
                request_id=post.request_id,
                post_id=post.post_id,
            ) from exc

    def _prepare_details(
        self,
        post: Post,
        *,
        translate: bool,
    ) -> PreparedSocialPost:
        if self._legacy_forwarder is not None and hasattr(
            self._legacy_forwarder, "prepare_post"
        ):
            prepared = self._legacy_forwarder.prepare_post(
                post, translate=translate
            )
        else:
            try:
                prepared = self._preparation.prepare(post, translate=translate)
            except SocialError:
                raise
            except Exception as exc:
                raise SocialTranslationError(
                    "社交内容翻译或格式化失败",
                    request_id=post.request_id,
                    post_id=post.post_id,
                ) from exc
        # 格式化正文属于 Post 的准备结果，供所有投递目标复用一次。
        post.extra["_social_full_text"] = prepared.full_text
        return prepared

    def prepare_post(self, post: Post, *, translate: bool = True) -> Post:
        """准备翻译和正文格式并返回同一个 ``Post`` 对象。"""
        self._prepare_details(post, translate=translate)
        return post

    def deliver_post(
        self,
        post: Post,
        targets: list[DeliveryTarget] | None = None,
        *,
        archive: bool = True,
    ) -> DeliveryResult:
        """使用统一投递服务发送已经准备好的 ``Post``。"""
        if self._legacy_forwarder is not None:
            self._legacy_forwarder.forward_post(post, targets, archive=archive)
            return (
                getattr(self._legacy_forwarder, "last_delivery_result", None)
                or DeliveryResult(
                    outcome="success",
                    matched_routes=1,
                    attempted_routes=1,
                    success_routes=1,
                    failed_routes=0,
                )
            )

        if targets is not None and any(
            not isinstance(target, DeliveryTarget) for target in targets
        ):
            raise TypeError("targets 只能包含 DeliveryTarget")
        full_text = str(post.extra.get("_social_full_text") or post.text or "")
        return self._delivery_service.deliver_post(
            post,
            full_text,
            targets,
            archive=archive,
        )

    def process_post(
        self,
        post: Post,
        *,
        targets: list[DeliveryTarget] | None = None,
        translate: bool = True,
        download: bool = True,
        archive: bool = True,
    ) -> SocialOperationResult:
        """处理已经解析出的动态（定时监控和批处理入口使用）。"""
        if not post.request_id:
            # 定时监控没有 HTTP/QQ 请求上下文，也必须能在日志中串起一次
            # 解析、准备、投递和状态写入。
            post.request_id = f"social-{uuid4().hex[:12]}"
        if download:
            self._download_post(post)
        prepared = self._prepare_details(post, translate=translate)
        delivery = self.deliver_post(post, targets, archive=archive)
        return SocialOperationResult(
            post=post,
            delivery=delivery,
            prepared=prepared,
            archived=archive,
            completed=delivery.complete,
        )

    def process_url(
        self,
        url: str,
        *,
        targets: list[DeliveryTarget] | None = None,
        translate: bool = True,
        archive: bool = True,
        request_id: str = "",
    ) -> SocialOperationResult:
        """完整执行解析、下载、准备、投递和归档。"""
        post = self.parse_url(url, request_id=request_id)
        return self.process_post(
            post,
            targets=targets,
            translate=translate,
            download=True,
            archive=archive,
        )

    async def process_url_to_qq(
        self,
        url: str,
        bot: Any,
        scope: str,
        target_id: str,
        *,
        translate: bool = True,
        archive: bool = True,
        request_id: str = "",
    ) -> SocialOperationResult:
        """旧 QQ 调用兼容包装器，实际委托同一个 ``process_url``。"""
        if self._legacy_forwarder is not None:
            # 仅兼容旧插件注入的 Forwarder double；生产 Forwarder 具备
            # delivery_service，永远走下方统一 process_url 路径。
            post = await asyncio.to_thread(
                self.parse_url, url, request_id=request_id
            )
            if not translate:
                post.extra["_skip_translate"] = True
            await asyncio.to_thread(self._download_post, post)
            prepared = await asyncio.to_thread(
                self._legacy_forwarder.prepare_post,
                post,
                translate=translate,
            )
            delivery = await self._legacy_forwarder.send_qq_target(
                post,
                bot,
                scope,
                target_id,
                prepared=prepared,
                archive=archive,
            )
            return SocialOperationResult(
                post=post,
                delivery=delivery,
                prepared=prepared,
                archived=archive,
                completed=delivery.complete,
            )
        from src.social.adapters import OfficialTarget
        normalized_scope = "groups" if scope == "groups" else "users"
        target = DeliveryTarget(
            channel="qq_official",
            target_id=str(target_id),
            scope=normalized_scope,
            bot_name=str(getattr(bot, "name", "") or ""),
        ).bind_runtime(
            OfficialTarget(
                bot,
                scope=normalized_scope,
                target_id=str(target_id),
                allow_missing_media=True,
            ),
            route_id=f"official:direct:{normalized_scope}:{target_id}",
        )
        return await asyncio.to_thread(
            self.process_url,
            url,
            targets=[target],
            translate=translate,
            archive=archive,
            request_id=request_id,
        )


__all__ = [
    "DeliveryResult",
    "DeliveryTarget",
    "SocialOperationResult",
    "SocialService",
    "delivery_to_dict",
]
