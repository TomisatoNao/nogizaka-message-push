"""社交投递兼容门面。

历史调用方仍通过 ``SocialDeliveryDispatcher.broadcast`` 获取字典结果；实际
工作已经统一委托给 ``DeliveryService``。保留平台模块导入是为了让旧插件和
测试继续通过 ``src.social.delivery.napcat`` 等路径替换 API。
"""

from __future__ import annotations

from collections.abc import Mapping

# 兼容旧的 monkeypatch / 扩展导入路径；真正的发送由 adapters 调用同一模块对象。
from src.platforms import napcat, qq_official, tgbot  # noqa: F401
from src.social.adapters import ChannelAdapter, OfficialTarget
from src.social.delivery_service import (
    ArchiveService,
    DeliveryService,
    DeliveryStateRepository,
    SocialStoreDeliveryState,
)
from src.social.contracts import DeliveryResult, DeliveryTarget, SocialDeliveryResult
from src.social.models import Post
from src.social.route_planner import PlannedRoute, RoutePlanner
from src.social.settings import RuntimeConfig
from src.social.targeting import normalize_delivery_targets


class SocialDeliveryDispatcher:
    """旧版分发器 API，内部使用统一 DeliveryService。"""

    def __init__(
        self,
        store=None,
        logger=None,
        config: Mapping | None = None,
        *,
        config_view: RuntimeConfig | None = None,
        delivery_service: DeliveryService | None = None,
    ):
        self._store = store
        self._log = logger or (lambda *_args, **_kwargs: None)
        self._runtime = config_view or RuntimeConfig(config)
        self._service = delivery_service or DeliveryService(
            config,
            config_view=self._runtime,
            store=store,
            logger=self._log,
        )

    @property
    def runtime_config(self) -> RuntimeConfig:
        return self._runtime

    @property
    def delivery_service(self) -> DeliveryService:
        return self._service

    @property
    def route_planner(self):
        """兼容调试/扩展代码读取当前路由规划器。"""
        return self._service.planner

    def _dispatch_async(self, coro):
        """保留旧分发器的事件循环辅助入口。"""
        return self._service._dispatch_async(coro)

    def broadcast(
        self,
        post: Post,
        full_text: str,
        target_channels: list[str] | None = None,
    ) -> dict:
        """保持旧字典返回形状，同时复用统一路由/状态/适配器实现。"""
        result = self._service.deliver_post(
            post,
            full_text,
            normalize_delivery_targets(target_channels, allow_legacy=True),
            archive=False,
        )
        return {
            "results": result.route_results,
            "errors": result.errors,
            "matched_routes": result.matched_routes,
            "skipped_routes": result.skipped_routes,
            "delivery": result,
        }


__all__ = [
    "ArchiveService",
    "ChannelAdapter",
    "DeliveryService",
    "DeliveryStateRepository",
    "DeliveryTarget",
    "DeliveryResult",
    "OfficialTarget",
    "PlannedRoute",
    "RoutePlanner",
    "SocialDeliveryDispatcher",
    "SocialDeliveryResult",
    "SocialStoreDeliveryState",
]
