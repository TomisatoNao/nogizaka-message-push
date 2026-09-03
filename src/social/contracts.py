"""社交链路的稳定领域契约。

这些对象位于平台适配器之上，WebUI、QQ Bot 和定时监控都只能通过它们
交换投递目标与结果。平台客户端、文件路径和配置字典不应泄漏到这些契约中。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import weakref


# Runtime platform clients are deliberately kept out of the public domain
# object.  A small identity registry lets the route-planning compatibility
# layer bind a client without adding non-contract dataclass fields.
_TARGET_RUNTIME: dict[int, tuple[weakref.ReferenceType["DeliveryTarget"], Any, str]] = {}
_RESULT_OBSERVATIONS: dict[
    int, tuple[weakref.ReferenceType["DeliveryResult"], int, int, tuple[bool, ...]]
] = {}


@dataclass(frozen=True)
class DeliveryTarget:
    """一个可投递目标。

    公开构造参数严格保持计划中的四个字段。平台客户端不属于领域对象，
    由路由规划层通过 ``bind_runtime`` 放入迁移期的进程内注册表。
    """

    channel: str
    target_id: str
    scope: str = ""
    bot_name: str = ""
    def __post_init__(self) -> None:
        # 迁移期只读兼容旧测试/插件的三参数形状
        # DeliveryTarget(route_id, channel, runtime). 这段只在构造边界执行；
        # 核心服务从不解析旧字符串，也不会依赖该形状。
        if isinstance(self.scope, dict) and not self.bot_name:
            legacy_route_id = str(self.channel)
            legacy_runtime = self.scope
            object.__setattr__(self, "channel", str(self.target_id))
            object.__setattr__(self, "target_id", legacy_route_id)
            object.__setattr__(self, "scope", "")
            _TARGET_RUNTIME[id(self)] = (
                weakref.ref(self),
                legacy_runtime,
                legacy_route_id,
            )
            return
        object.__setattr__(self, "channel", str(self.channel))
        object.__setattr__(self, "target_id", str(self.target_id))
        object.__setattr__(self, "scope", str(self.scope or ""))
        object.__setattr__(self, "bot_name", str(self.bot_name or ""))

    @property
    def route_id(self) -> str:
        """迁移期只读路由标识，正式代码应使用 PlannedRoute.route_id。"""
        entry = _TARGET_RUNTIME.get(id(self))
        if entry and entry[0]() is self and entry[2]:
            return entry[2]
        parts = [self.channel]
        if self.bot_name:
            parts.append(self.bot_name)
        if self.scope:
            parts.append(self.scope)
        parts.append(self.target_id)
        return ":".join(parts)

    def bind_runtime(self, value: Any, *, route_id: str = "") -> "DeliveryTarget":
        """返回绑定平台运行时对象的副本，仅供路由规划层使用。"""
        target = DeliveryTarget(self.channel, self.target_id, self.scope, self.bot_name)
        _TARGET_RUNTIME[id(target)] = (
            weakref.ref(target),
            value,
            str(route_id or ""),
        )
        return target

    @property
    def runtime(self) -> Any:
        """平台适配器读取的内部运行时对象。"""
        entry = _TARGET_RUNTIME.get(id(self))
        return entry[1] if entry and entry[0]() is self else None


@dataclass(frozen=True, init=False)
class DeliveryResult:
    """一次投递的统一结果。

    这七个字段是稳定业务契约。媒体计数和逐路由布尔值属于观测信息，
    通过属性提供但不污染领域契约字段；它们仅为旧 UI/QQ 适配保留。
    """

    outcome: str
    matched_routes: int
    attempted_routes: int
    success_routes: int
    failed_routes: int
    skipped_routes: int = 0
    errors: tuple[str, ...]

    def __init__(
        self,
        outcome: str,
        matched_routes: int,
        attempted_routes: int,
        success_routes: int,
        failed_routes: int,
        skipped_routes: int = 0,
        errors: tuple[str, ...] = (),
        *,
        media_sent: int = 0,
        media_total: int = 0,
        route_results: tuple[bool, ...] = (),
    ) -> None:
        object.__setattr__(self, "outcome", str(outcome))
        object.__setattr__(self, "matched_routes", int(matched_routes))
        object.__setattr__(self, "attempted_routes", int(attempted_routes))
        object.__setattr__(self, "success_routes", int(success_routes))
        object.__setattr__(self, "failed_routes", int(failed_routes))
        object.__setattr__(self, "skipped_routes", int(skipped_routes))
        object.__setattr__(self, "errors", tuple(str(item) for item in errors))
        _RESULT_OBSERVATIONS[id(self)] = (
            weakref.ref(self),
            int(media_sent),
            int(media_total),
            tuple(bool(item) for item in route_results),
        )

    def _observation(self) -> tuple[int, int, tuple[bool, ...]]:
        entry = _RESULT_OBSERVATIONS.get(id(self))
        if entry and entry[0]() is self:
            return entry[1], entry[2], entry[3]
        return 0, 0, ()

    @property
    def media_sent(self) -> int:
        return self._observation()[0]

    @property
    def media_total(self) -> int:
        return self._observation()[1]

    @property
    def route_results(self) -> tuple[bool, ...]:
        return self._observation()[2]

    @property
    def any_success(self) -> bool:
        return self.success_routes > 0

    @property
    def complete(self) -> bool:
        return self.outcome in {"success", "no_route", "already_delivered"}


# 旧导入路径的明确兼容别名。核心模块统一导入 DeliveryResult，避免继续
# 以旧名称扩散新的依赖。
SocialDeliveryResult = DeliveryResult


__all__ = ["DeliveryResult", "DeliveryTarget", "SocialDeliveryResult"]
