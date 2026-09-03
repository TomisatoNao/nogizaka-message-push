"""QQ 官方 Bot 单目标投递兼容门面。"""

from __future__ import annotations

from collections.abc import Callable

from src.social.delivery_service import DeliveryService
from src.social.contracts import DeliveryResult
from src.social.models import Post


class QQDirectDelivery:
    """保留旧 ``send`` API，实际发送委托给统一 DeliveryService。"""

    def __init__(
        self,
        logger: Callable[..., None] | None = None,
        *,
        delivery_service: DeliveryService | None = None,
    ):
        self._log = logger or (lambda *_args, **_kwargs: None)
        self._service = delivery_service or DeliveryService(logger=self._log)

    @property
    def delivery_service(self) -> DeliveryService:
        return self._service

    async def send(
        self,
        post: Post,
        bot,
        scope: str,
        target_id: str,
        *,
        prepared=None,
        prepare: Callable[[Post], object] | None = None,
        archive: bool = True,
        archive_callback: Callable[[Post], bool] | None = None,
    ) -> DeliveryResult:
        """发送一条已经准备或可由回调准备的动态。"""
        try:
            if prepared is None:
                if prepare is None:
                    raise ValueError("QQ 直投缺少正文准备器")
                prepared = prepare(post)
            full_text = getattr(prepared, "full_text", None)
            if full_text is None:
                raise ValueError("QQ 直投正文准备结果无 full_text")
        except Exception as exc:
            # 保留旧 API 的“返回失败结果而非向上抛出”语义；这样 QQ 指令层
            # 可以统一渲染失败原因，也不会因为准备器异常中断事件循环。
            error = f"QQ 官方 Bot [{getattr(bot, 'name', 'official')}] {type(exc).__name__}"
            self._log(f"⚠️ [社媒推送] {error}", is_error=True)
            if archive:
                try:
                    if archive_callback is not None:
                        archive_callback(post)
                    else:
                        self._service.archive_service.archive(post)
                except Exception as archive_exc:
                    self._log(
                        f"⚠️ [社媒归档] 写入失败: {type(archive_exc).__name__}",
                        is_debug=True,
                    )
            return DeliveryResult(
                outcome="failed",
                matched_routes=1,
                attempted_routes=1,
                success_routes=0,
                failed_routes=1,
                errors=(error,),
                route_results=(False,),
            )
        return await self._service.deliver_to_target(
            post,
            str(full_text),
            bot,
            scope,
            target_id,
            archive=archive,
            archive_callback=archive_callback,
        )


__all__ = ["QQDirectDelivery"]
