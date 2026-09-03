"""统一社交动态投递服务。

``DeliveryService`` 是社交链路的应用层投递边界：它接收已经准备好的正文，
由 ``RoutePlanner`` 计算目标，再把每个目标交给通道适配器执行。投递状态和
归档也在这里集中处理，因而定时监控、WebUI 和 QQ Bot 不会再各自维护一套
“发送后如何标记”的实现。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from src.social.adapters import (
    ChannelAdapter,
    OfficialTarget,
    QQOfficialAdapter,
)
from src.social.contracts import DeliveryResult, DeliveryTarget
from src.social.errors import SocialDeliveryError
from src.social.models import MediaItem, Post
from src.social.route_planner import PlannedRoute, RoutePlanner
from src.social.settings import RuntimeConfig


@runtime_checkable
class DeliveryStateRepository(Protocol):
    """投递状态仓储的最小契约。

    ``SocialStore`` 已经实现了这两个方法；协议单独存在后，测试和未来的
    Redis/远程仓储可以替换 SQLite，而不会让 DeliveryService 依赖具体存储。
    """

    def delivered_routes(self, platform: str, item_id: str) -> set[str]:
        ...

    def mark_route_result(
        self,
        platform: str,
        item_id: str,
        route_id: str,
        ok: bool,
        error: str = "",
    ) -> None:
        ...


class SocialStoreDeliveryState:
    """把既有 ``SocialStore`` 适配为显式的状态仓储。"""

    def __init__(self, store: DeliveryStateRepository):
        self._store = store

    def delivered_routes(self, platform: str, item_id: str) -> set[str]:
        return set(self._store.delivered_routes(platform, item_id))

    def mark_route_result(
        self,
        platform: str,
        item_id: str,
        route_id: str,
        ok: bool,
        error: str = "",
    ) -> None:
        self._store.mark_route_result(platform, item_id, route_id, ok, error)


class ArchiveService:
    """社交内容归档边界，保证调用方只需要关心成功/失败。"""

    _ARCHIVE_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error)

    def __init__(
        self,
        archive_fn: Callable[[Post], bool] | None = None,
        logger: Callable[..., None] | None = None,
    ):
        self._archive_fn = archive_fn
        self._log = logger or (lambda *_args, **_kwargs: None)

    def archive(self, post: Post) -> bool:
        """写入一次归档；归档失败不回滚已完成的通道投递。"""
        try:
            if self._archive_fn is not None:
                return bool(self._archive_fn(post))
            # 延迟导入，避免测试/启动阶段初始化整个归档模块。
            from src.social import archive

            return bool(archive.get_archive().add_post(post))
        except self._ARCHIVE_ERRORS as exc:
            self._log(
                f"⚠️ [社媒归档] 写入失败 | request_id={post.request_id or '-'} "
                f"| post_id={post.post_id} | error={type(exc).__name__}",
                is_debug=True,
            )
            return False


@dataclass(frozen=True)
class _RouteAttempt:
    route_id: str
    ok: bool
    error: str = ""


class DeliveryService:
    """统一执行路由匹配、并发投递、状态持久化和归档。"""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        config_view: RuntimeConfig | None = None,
        store: DeliveryStateRepository | None = None,
        state: DeliveryStateRepository | None = None,
        archive_service: ArchiveService | None = None,
        planner: RoutePlanner | None = None,
        adapters: Mapping[str, ChannelAdapter] | None = None,
        telegram_provider: Callable[[], Iterable[Any]] | None = None,
        official_provider: Callable[[], Iterable[Any]] | None = None,
        napcat_routes_provider: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
        logger: Callable[..., None] | None = None,
    ):
        self._runtime = config_view or RuntimeConfig(config)
        self._log = logger or (lambda *_args, **_kwargs: None)
        raw_state = state or store
        self._state = (
            raw_state
            if raw_state is None or isinstance(raw_state, SocialStoreDeliveryState)
            else SocialStoreDeliveryState(raw_state)
        )
        self._archive = archive_service or ArchiveService(logger=self._log)
        self._planner = planner or RoutePlanner(
            config,
            config_view=self._runtime,
            telegram_provider=telegram_provider,
            official_provider=official_provider,
            napcat_routes_provider=napcat_routes_provider,
            adapters=adapters,
            logger=self._log,
        )

    @property
    def runtime_config(self) -> RuntimeConfig:
        return self._runtime

    @property
    def planner(self) -> RoutePlanner:
        return self._planner

    @property
    def adapters(self) -> Mapping[str, ChannelAdapter]:
        """Expose the planner's adapter registry for non-Post acknowledgements."""
        return self._planner.adapters

    @property
    def state(self) -> DeliveryStateRepository | None:
        return self._state

    @property
    def archive_service(self) -> ArchiveService:
        return self._archive

    @staticmethod
    def _dispatch_async(coro):
        """兼容同步监控线程和已有 asyncio loop 的调用方。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=60)
        return asyncio.run(coro)

    @staticmethod
    async def _maybe_await(value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _send_route(
        self,
        route: PlannedRoute,
        full_text: str,
        media: list[MediaItem],
        *,
        post: Post | None = None,
    ) -> _RouteAttempt:
        """调用适配器，不让一个路由的异常中断其它路由。"""
        request_id = getattr(post, "request_id", "") if post else ""
        post_id = getattr(post, "post_id", "") if post else ""
        context = (
            f"request_id={request_id or '-'} | post_id={post_id or '-'} | "
            f"route_id={route.route_id}"
        )
        self._log(f"📤 [社媒推送] 开始投递 | {context}", is_debug=True)
        try:
            send_post = getattr(route.adapter, "send_post", None)
            if callable(send_post):
                sent = await self._maybe_await(
                    send_post(route.target, full_text, media)
                )
            else:
                sent = await self._maybe_await(
                    route.adapter.send_text(route.target, full_text)
                )
                if sent:
                    for item in media:
                        sent = await self._maybe_await(
                            route.adapter.send_media(route.target, item)
                        )
                        if not sent:
                            break
            attempt = _RouteAttempt(route.route_id, bool(sent))
            self._log(
                f"{'✅' if attempt.ok else '⚠️'} [社媒推送] 路由{'成功' if attempt.ok else '失败'} | {context}",
                is_debug=attempt.ok,
                is_error=not attempt.ok,
            )
            return attempt
        except Exception as exc:  # 网络/第三方 SDK 异常必须隔离到单一路由
            error = type(exc).__name__
            self._log(
                f"⚠️ [社媒推送] 路由异常 | {context} | error={error}",
                is_error=True,
            )
            return _RouteAttempt(route.route_id, False, error)

    async def _deliver_routes(
        self,
        post: Post,
        full_text: str,
        routes: list[PlannedRoute],
        delivered_routes: set[str],
        *,
        persist: bool = True,
    ) -> DeliveryResult:
        pending = [route for route in routes if route.route_id not in delivered_routes]
        skipped_routes = len(routes) - len(pending)
        attempts = await asyncio.gather(
            *(
                self._send_route(route, full_text, post.media, post=post)
                for route in pending
            )
        )

        errors: list[str] = []
        for attempt in attempts:
            if attempt.error:
                errors.append(f"{attempt.route_id}: {attempt.error}")
            if persist and self._state is not None:
                try:
                    self._state.mark_route_result(
                        post.platform.lower(),
                        post.post_id,
                        attempt.route_id,
                        attempt.ok,
                        attempt.error,
                    )
                    self._log(
                        f"💾 [社媒推送] 路由状态已持久化 | request_id={post.request_id or '-'} "
                        f"| post_id={post.post_id} | route_id={attempt.route_id} "
                        f"| success={str(attempt.ok).lower()}",
                        is_debug=True,
                    )
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    state_error = f"state_{type(exc).__name__}"
                    errors.append(f"{attempt.route_id}: {state_error}")
                    self._log(
                        f"⚠️ [社媒推送] 路由状态持久化失败 | request_id={post.request_id or '-'} "
                        f"| post_id={post.post_id} | route_id={attempt.route_id} "
                        f"| error={state_error}",
                        is_error=True,
                    )

        matched_routes = len(routes)
        attempted_routes = len(attempts)
        success_routes = sum(attempt.ok for attempt in attempts)
        failed_routes = attempted_routes - success_routes
        if matched_routes == 0:
            outcome = "no_route"
        elif attempted_routes == 0 and skipped_routes == matched_routes:
            outcome = "already_delivered"
        elif failed_routes == 0:
            outcome = "success"
        elif success_routes:
            outcome = "partial"
        else:
            outcome = "failed"

        media_total = len(post.media)
        media_sent = media_total if (success_routes > 0 or not pending) else 0
        return DeliveryResult(
            outcome=outcome,
            matched_routes=matched_routes,
            attempted_routes=attempted_routes,
            success_routes=success_routes,
            failed_routes=failed_routes,
            skipped_routes=skipped_routes,
            errors=tuple(errors),
            route_results=tuple(attempt.ok for attempt in attempts),
            media_sent=media_sent,
            media_total=media_total,
        )

    def _log_summary(self, post: Post, result: DeliveryResult) -> None:
        post_ref = f"{post.author} - {post.post_id[:20]}"
        context = (
            f"request_id={post.request_id or '-'} | post_id={post.post_id}"
        )
        if result.errors:
            for error in result.errors:
                route_id, _, detail = error.partition(": ")
                self._log(
                    f"⚠️ [社媒推送] {error} | {context} | route_id={route_id or '-'}",
                    is_error=True,
                )

        if result.outcome == "no_route":
            self._log(
                f"⏭️ [社媒推送] {post.platform} 动态跳过 | 无匹配路由 | "
                f"{post_ref} | {context}",
                is_debug=True,
            )
        elif result.outcome == "already_delivered":
            self._log(
                f"✅ [社媒推送] {post.platform} 动态已完成 | 路由已投递 "
                f"{result.skipped_routes}/{result.matched_routes} | {post_ref} | {context}",
                is_debug=True,
            )
        elif result.outcome == "success":
            self._log(
                f"✅ [社媒推送] {post.platform} 动态已分发 | 路由成功 "
                f"{result.success_routes}/{result.matched_routes} | {post_ref} | {context}",
                is_debug=True,
            )
        elif result.outcome == "partial":
            self._log(
                f"⚠️ [社媒推送] {post.platform} 动态部分成功 | 路由成功 "
                f"{result.success_routes}/{result.matched_routes} | 失败 "
                f"{result.failed_routes} | 下轮仅重试失败路由 | {post_ref} | {context}",
                is_error=True,
            )
        else:
            self._log(
                f"⚠️ [社媒推送] {post.platform} 动态全部目标失败 | 路由 0/"
                f"{result.matched_routes} | 下轮重试 | {post_ref} | {context}",
                is_error=True,
            )

    def deliver_post(
        self,
        post: Post,
        full_text: str,
        targets: list[DeliveryTarget] | None = None,
        *,
        archive: bool = True,
    ) -> DeliveryResult:
        """同步入口：规划并发投递，成功路由写入状态，归档只执行一次。"""
        try:
            if targets is not None and any(
                not isinstance(target, DeliveryTarget) for target in targets
            ):
                raise SocialDeliveryError(
                    "投递服务只接受 DeliveryTarget，不接受旧字符串通道",
                    request_id=post.request_id,
                    post_id=post.post_id,
                )
            routes = self._planner.plan(post, targets)
            delivered_routes = (
                self._state.delivered_routes(post.platform.lower(), post.post_id)
                if self._state is not None
                else set()
            )
            result = self._dispatch_async(
                self._deliver_routes(
                    post,
                    full_text,
                    routes,
                    delivered_routes,
                )
            )
        except SocialDeliveryError:
            raise
        except Exception as exc:  # 规划/事件循环级故障转为领域异常
            self._log(
                f"⚠️ [社媒推送] 编排异常 | request_id={post.request_id or '-'} "
                f"| post_id={post.post_id} | error={type(exc).__name__}",
                is_error=True,
            )
            raise SocialDeliveryError(
                "社媒投递编排失败",
                request_id=post.request_id,
                post_id=post.post_id,
            ) from exc

        self._log_summary(post, result)
        if archive:
            self._archive.archive(post)
        return result

    async def deliver_text(self, target: DeliveryTarget, text: str) -> bool:
        """发送非动态确认文本，仍经由同一通道适配器。

        QQ Bot 的指令错误回复以前在 ``qq_commands`` 中直接调用 Bot 方法，
        使入口绕过通道适配器。该小型入口不创建 Post/归档状态，但发送动作
        仍由 DeliveryService 统一承接。
        """
        if not isinstance(target, DeliveryTarget):
            raise SocialDeliveryError("文本投递只接受 DeliveryTarget")
        adapter = self._planner.adapters.get(target.channel)
        if adapter is None or target.runtime is None:
            raise SocialDeliveryError(
                "文本投递目标未绑定可用通道",
                post_id=target.target_id,
            )
        try:
            sent = await self._maybe_await(adapter.send_text(target, text))
            return bool(sent)
        except Exception as exc:
            raise SocialDeliveryError(
                "文本投递失败",
                post_id=target.target_id,
            ) from exc

    async def deliver_to_target(
        self,
        post: Post,
        full_text: str,
        bot: Any,
        scope: str,
        target_id: str,
        *,
        archive: bool = True,
        archive_callback: Callable[[Post], bool] | None = None,
    ) -> DeliveryResult:
        """异步入口：向 QQ Bot 指定 OpenID 投递，不污染广播路由状态。"""
        normalized_scope = "groups" if scope == "groups" else "users"
        route_id = f"official:direct:{normalized_scope}:{target_id}"
        target = DeliveryTarget(
            channel="qq_official",
            target_id=str(target_id),
            scope=normalized_scope,
        ).bind_runtime(OfficialTarget(
                bot,
                scope=normalized_scope,
                target_id=str(target_id),
            ), route_id=route_id)
        adapter = QQOfficialAdapter(logger=self._log)
        text_ok = False
        media_sent = 0
        errors: list[str] = []

        try:
            text_ok = bool(await adapter.send_text(target, full_text))
            if not text_ok:
                errors.append("正文发送未成功")
        except Exception as exc:
            errors.append(f"正文: {type(exc).__name__}")

        for item in post.media:
            try:
                sent = bool(await adapter.send_media(target, item))
            except Exception as exc:
                sent = False
                errors.append(f"媒体: {type(exc).__name__}")
            if sent:
                media_sent += 1
            elif not item.local_path or not os.path.exists(item.local_path):
                errors.append(f"媒体文件缺失 ({item.type})")
            else:
                errors.append(
                    f"媒体发送未成功 ({os.path.basename(item.local_path)})"
                )

        media_total = len(post.media)
        complete = text_ok and media_sent == media_total and not errors
        partial = (text_ok or media_sent > 0) and not complete
        outcome = "success" if complete else "partial" if partial else "failed"
        result = DeliveryResult(
            outcome=outcome,
            matched_routes=1,
            attempted_routes=1,
            success_routes=1 if complete else 0,
            failed_routes=0 if complete else 1,
            errors=tuple(errors),
            media_sent=media_sent,
            media_total=media_total,
            route_results=(complete,),
        )
        for error in result.errors:
            self._log(f"⚠️ [社媒推送] {error}", is_error=True)

        if archive:
            if archive_callback is not None:
                try:
                    archive_callback(post)
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    self._log(
                        f"⚠️ [社媒归档] 写入失败: {type(exc).__name__}",
                        is_debug=True,
                    )
            else:
                self._archive.archive(post)
        return result


__all__ = [
    "ArchiveService",
    "DeliveryService",
    "DeliveryStateRepository",
    "SocialStoreDeliveryState",
]
