"""异步任务生命周期工具。

集中处理批量协程在异常/取消时的收尾，避免 ``asyncio.gather`` 提前返回后
留下仍在运行的子任务、占用信号量或持有领域锁。这里只负责任务生命周期，
不改变业务异常类型或重试策略。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable
from typing import Any


def _positive_timeout(value: float) -> float:
    try:
        return max(0.01, float(value))
    except (TypeError, ValueError):
        return 5.0


async def cancel_tasks_bounded(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    timeout: float = 5.0,
) -> bool:
    """取消并在有限预算内消费一组任务的结果。

    返回 ``True`` 表示所有任务在预算内收尾，``False`` 表示仍有任务在
    后台清理。即使调用方正在响应取消，也会尽力完成一次有限收尾，然后
    重新抛出 ``CancelledError``，从而不吞掉上层停止信号。
    """

    task_list = [task for task in tasks]
    pending = [task for task in task_list if not task.done()]
    for task in pending:
        task.cancel()
    if not pending:
        return True

    cleanup = asyncio.gather(*pending, return_exceptions=True)
    cleanup_timeout = _positive_timeout(timeout)
    try:
        await asyncio.wait_for(asyncio.shield(cleanup), timeout=cleanup_timeout)
        return True
    except asyncio.TimeoutError:
        # shield 防止 wait_for 超时取消 gather 本身；让它继续消费异常，
        # 避免“Task exception was never retrieved”，同时不再阻塞当前调用方。
        cleanup.add_done_callback(lambda _future: None)
        return False
    except asyncio.CancelledError:
        # 当前任务也在取消，仍给子任务一个短暂的收尾窗口；若窗口耗尽，
        # 取消剩余任务并保留原始取消语义。
        try:
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=cleanup_timeout)
        except asyncio.TimeoutError:
            for task in pending:
                if not task.done():
                    task.cancel()
            cleanup.add_done_callback(lambda _future: None)
        raise


async def gather_cancel_safe(
    awaitables: Iterable[Awaitable[Any]],
    *,
    return_exceptions: bool = False,
    cleanup_timeout: float = 5.0,
) -> list[Any]:
    """执行一组 awaitable，并在异常或取消时收尾所有子任务。

    与 ``asyncio.gather`` 一样返回按输入顺序排列的结果；不同之处是
    ``return_exceptions=False`` 时首个异常出现后会取消尚未完成的兄弟任务，
    不留下跨轮次运行的后台工作。
    """

    tasks = [asyncio.ensure_future(item) for item in awaitables]
    if not tasks:
        return []
    try:
        return list(await asyncio.gather(*tasks, return_exceptions=return_exceptions))
    except BaseException:
        await cancel_tasks_bounded(tasks, timeout=cleanup_timeout)
        raise


__all__ = ["cancel_tasks_bounded", "gather_cancel_safe"]
