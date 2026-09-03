"""社交动态路由规划。

RoutePlanner 只负责“这条动态应该投递到哪些目标”，不执行网络请求，也不
写入投递状态。这样路由规则可以在没有真实平台连接的情况下独立测试。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.platforms import qq_official, tgbot
from src.social.adapters import (
    ChannelAdapter,
    NapCatAdapter,
    OfficialTarget,
    QQOfficialAdapter,
    TelegramAdapter,
)
from src.social.contracts import DeliveryTarget
from src.social.models import Post
from src.social.settings import RuntimeConfig
from src.utils import match_member_filter


@dataclass(frozen=True)
class PlannedRoute:
    """一个已经匹配、等待 DeliveryService 执行的路由。"""

    route_id: str
    adapter: ChannelAdapter
    target: DeliveryTarget


class RoutePlanner:
    """按通道开关、平台过滤器和显式目标生成路由计划。"""

    def __init__(
        self,
        config=None,
        *,
        config_view: RuntimeConfig | None = None,
        telegram_provider: Callable[[], Iterable[Any]] | None = None,
        official_provider: Callable[[], Iterable[Any]] | None = None,
        napcat_routes_provider: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
        adapters: Mapping[str, ChannelAdapter] | None = None,
        logger: Callable[..., None] | None = None,
    ):
        self._runtime = config_view or RuntimeConfig(config)
        self._telegram_provider = telegram_provider or tgbot.get_configured_bots
        self._official_provider = official_provider or qq_official.get_configured_bots
        self._napcat_routes_provider = napcat_routes_provider or (
            lambda: self._runtime.list("NAPCAT_ROUTES")
        )
        provided = dict(adapters or {})
        self._adapters: dict[str, ChannelAdapter] = {
            "tg": provided.get("tg") or TelegramAdapter(logger=logger),
            "napcat": provided.get("napcat") or NapCatAdapter(logger=logger),
            "qq_official": provided.get("qq_official")
            or QQOfficialAdapter(logger=logger),
        }

    @property
    def runtime_config(self) -> RuntimeConfig:
        return self._runtime

    @property
    def adapters(self) -> Mapping[str, ChannelAdapter]:
        """只读适配器映射；发送仍由 DeliveryService 编排。"""
        return self._adapters

    @staticmethod
    def _target_matches(
        candidate: DeliveryTarget,
        requested: DeliveryTarget,
    ) -> bool:
        """按领域字段匹配目标；不解析旧字符串。"""
        if requested.channel and candidate.channel != requested.channel:
            return False
        if requested.target_id and candidate.target_id != requested.target_id:
            return False
        if requested.scope and candidate.scope != requested.scope:
            return False
        if requested.bot_name and candidate.bot_name != requested.bot_name:
            return False
        return True

    @classmethod
    def _selected(
        cls,
        requested: list[DeliveryTarget] | None,
        candidate: DeliveryTarget,
    ) -> bool:
        return requested is None or any(
            cls._target_matches(candidate, target) for target in requested
        )

    def plan(
        self,
        post: Post,
        targets: list[DeliveryTarget] | None = None,
    ) -> list[PlannedRoute]:
        if targets is not None:
            # QQ 指令的目标已经绑定了真实 Bot 实例，直接生成一个路由；
            # WebUI 目标没有运行时对象，继续从 provider 中解析配置。
            direct_routes: list[PlannedRoute] = []
            for target in targets:
                if target.runtime is None:
                    continue
                adapter = self._adapters.get(target.channel)
                if adapter is None:
                    continue
                direct_routes.append(
                    PlannedRoute(
                        route_id=target.route_id,
                        adapter=adapter,
                        target=target,
                    )
                )
            if direct_routes:
                return direct_routes

        member_name = post.extra.get("member_name")
        account_name = post.extra.get("account") or post.author
        platform = post.platform.lower()
        routes: list[PlannedRoute] = []

        if self._runtime.channel_enabled("tg"):
            for bot in self._telegram_provider() or ():
                if not getattr(bot, "token", None) or not getattr(
                    bot, "target_chat", None
                ):
                    continue
                candidate = DeliveryTarget(
                    channel="tg",
                    target_id=str(bot.target_chat),
                    bot_name=str(getattr(bot, "name", "") or ""),
                )
                if not self._selected(targets, candidate):
                    continue
                if targets is None and not self._matches_filters(
                    bot,
                    platform,
                    member_name,
                    account_name,
                ):
                    continue
                route_id = f"tg:{getattr(bot, 'name', bot.target_chat)}"
                target = DeliveryTarget(
                    channel="tg",
                    target_id=str(bot.target_chat),
                    bot_name=str(getattr(bot, "name", "") or ""),
                ).bind_runtime(bot, route_id=route_id)
                routes.append(
                    PlannedRoute(
                        route_id=route_id,
                        adapter=self._adapters["tg"],
                        target=target,
                    )
                )

        if self._runtime.channel_enabled("napcat"):
            for route in self._napcat_routes_provider() or ():
                if not isinstance(route, Mapping):
                    continue
                group_id = route.get("group_id")
                if not group_id:
                    continue
                candidate = DeliveryTarget(
                    channel="napcat",
                    target_id=str(group_id),
                    scope="groups",
                )
                if not self._selected(targets, candidate):
                    continue
                if targets is None and not self._matches_filters(
                    route,
                    platform,
                    member_name,
                    account_name,
                ):
                    continue
                route_id = f"napcat:{group_id}"
                target = DeliveryTarget(
                    channel="napcat",
                    target_id=str(group_id),
                    scope="groups",
                ).bind_runtime(route, route_id=route_id)
                routes.append(
                    PlannedRoute(
                        route_id=route_id,
                        adapter=self._adapters["napcat"],
                        target=target,
                    )
                )

        if self._runtime.channel_enabled("qq_official"):
            for bot in self._official_provider() or ():
                send_private = bool(getattr(bot, "target_openid", None))
                send_group = bool(getattr(bot, "group_openid", None))
                if targets is None and not self._matches_filters(
                    bot,
                    platform,
                    member_name,
                    account_name,
                ):
                    continue
                # 私聊和群聊是两个独立路由，分别持久化成功状态；一个目标
                # 失败时不会阻塞或重复发送另一个目标。
                if send_private:
                    candidate = DeliveryTarget(
                        channel="qq_official",
                        target_id=str(bot.target_openid),
                        scope="users",
                        bot_name=str(getattr(bot, "name", "") or ""),
                    )
                    if not self._selected(targets, candidate):
                        send_private = False
                if send_group:
                    candidate = DeliveryTarget(
                        channel="qq_official",
                        target_id=str(bot.group_openid),
                        scope="groups",
                        bot_name=str(getattr(bot, "name", "") or ""),
                    )
                    if not self._selected(targets, candidate):
                        send_group = False
                if send_private:
                    route_id = f"official:{bot.name}:private"
                    target = DeliveryTarget(
                        channel="qq_official",
                        target_id=str(bot.target_openid),
                        scope="users",
                        bot_name=str(getattr(bot, "name", "") or ""),
                    ).bind_runtime(
                        OfficialTarget(
                            bot,
                            scope="users",
                            target_id=str(bot.target_openid),
                            allow_missing_media=True,
                        ),
                        route_id=route_id,
                    )
                    routes.append(
                        PlannedRoute(
                            route_id=route_id,
                            adapter=self._adapters["qq_official"],
                            target=target,
                        )
                    )
                if send_group:
                    route_id = f"official:{bot.name}:group"
                    target = DeliveryTarget(
                        channel="qq_official",
                        target_id=str(bot.group_openid),
                        scope="groups",
                        bot_name=str(getattr(bot, "name", "") or ""),
                    ).bind_runtime(
                        OfficialTarget(
                            bot,
                            scope="groups",
                            target_id=str(bot.group_openid),
                            allow_missing_media=True,
                        ),
                        route_id=route_id,
                    )
                    routes.append(
                        PlannedRoute(
                            route_id=route_id,
                            adapter=self._adapters["qq_official"],
                            target=target,
                        )
                    )

        return routes

    @staticmethod
    def _matches_filters(
        target: Any,
        platform: str,
        member_name: str | None,
        account_name: str,
    ) -> bool:
        if isinstance(target, Mapping):
            if not target.get(f"push_{platform}", True):
                return False
            member_filter = target.get("member_filter") or []
            social_filter = target.get("social_filter") or []
        else:
            if not getattr(target, f"push_{platform}", True):
                return False
            member_filter = getattr(target, "member_filter", None) or []
            social_filter = getattr(target, "social_filter", None) or []

        if member_name and member_filter and not match_member_filter(
            member_name, member_filter
        ):
            return False
        if social_filter and account_name not in social_filter and (
            not member_name or member_name not in social_filter
        ):
            return False
        return True


__all__ = ["PlannedRoute", "RoutePlanner"]
