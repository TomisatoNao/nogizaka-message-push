"""
src/http_pool.py — 全局 HTTP Client 连接池与生命周期管理器

统一管理核心 AsyncClient 实例（通用请求、Token 认证、QQ Bot API、博客抓取），
提供 Loop 绑定自愈、状态探活、代理热重载以及优雅关闭。
"""

import asyncio
import logging
import threading
from collections.abc import Callable
import httpx
import src.config.config as cfg

log = logging.getLogger("collink")

_general_client: httpx.AsyncClient | None = None
_auth_client: httpx.AsyncClient | None = None
_qq_client: httpx.AsyncClient | None = None
_blog_client: httpx.AsyncClient | None = None
_general_loop: asyncio.AbstractEventLoop | None = None
_auth_loop: asyncio.AbstractEventLoop | None = None
_qq_loop: asyncio.AbstractEventLoop | None = None
_blog_loop: asyncio.AbstractEventLoop | None = None

_thread_lock = threading.Lock()
_lifecycle_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
_general_rebind_callbacks: list[Callable[[httpx.AsyncClient], None]] = []
_auth_rebind_callbacks: list[Callable[[httpx.AsyncClient], None]] = []

# 连接池关闭属于恢复路径，不能因为坏掉的 transport 无限阻塞主循环。
CLIENT_CLOSE_TIMEOUT_SECONDS = 5.0


def _get_lifecycle_lock() -> asyncio.Lock:
    """获取当前事件循环专属的生命周期锁。"""
    loop = asyncio.get_running_loop()
    key = id(loop)
    with _thread_lock:
        current = _lifecycle_locks.get(key)
        if current is None or current[0] is not loop:
            current = (loop, asyncio.Lock())
            _lifecycle_locks[key] = current
        return current[1]


def clear_loop_state(loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """移除指定事件循环的生命周期锁。

    HTTP Client 关闭后锁不再有工作要保护；清理模块级 registry 可避免
    反复创建/销毁测试或重载 loop 时保留旧锁。调用方应在所有池操作完成
    后调用，正在运行的 ``get_*`` 不会被中途移除。
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
    key = id(loop)
    with _thread_lock:
        current = _lifecycle_locks.get(key)
        if current is not None and current[0] is loop:
            _lifecycle_locks.pop(key, None)
            return True
    return False


def register_general_client_rebind(callback: Callable[[httpx.AsyncClient], None]) -> None:
    """注册通用 Client 替换后的同步注入回调（幂等）。"""
    with _thread_lock:
        if callback not in _general_rebind_callbacks:
            _general_rebind_callbacks.append(callback)


def unregister_general_client_rebind(callback: Callable[[httpx.AsyncClient], None]) -> None:
    """移除通用 Client 替换回调；测试和进程重载可安全调用。"""
    with _thread_lock:
        try:
            _general_rebind_callbacks.remove(callback)
        except ValueError:
            pass


def register_auth_client_rebind(callback: Callable[[httpx.AsyncClient], None]) -> None:
    """注册认证 Client 替换后的同步注入回调（幂等）。"""
    with _thread_lock:
        if callback not in _auth_rebind_callbacks:
            _auth_rebind_callbacks.append(callback)


def unregister_auth_client_rebind(callback: Callable[[httpx.AsyncClient], None]) -> None:
    """移除认证 Client 替换回调；测试和进程重载可安全调用。"""
    with _thread_lock:
        try:
            _auth_rebind_callbacks.remove(callback)
        except ValueError:
            pass


def bind_runtime_clients(
    general_client: httpx.AsyncClient,
    *,
    auth_client: httpx.AsyncClient | None = None,
    qq_client: httpx.AsyncClient | None = None,
    blog_client: httpx.AsyncClient | None = None,
) -> None:
    """绑定主程序创建的 Client，使恢复路径与实际运行时状态一致。"""
    global _general_client, _auth_client, _qq_client, _blog_client
    global _general_loop, _auth_loop, _qq_loop, _blog_loop
    loop = asyncio.get_running_loop()
    with _thread_lock:
        _general_client = general_client
        _general_loop = loop
        if auth_client is not None:
            _auth_client = auth_client
            _auth_loop = loop
        if qq_client is not None:
            _qq_client = qq_client
            _qq_loop = loop
        if blog_client is not None:
            _blog_client = blog_client
            _blog_loop = loop


def _notify_general_rebind(client: httpx.AsyncClient) -> None:
    with _thread_lock:
        callbacks = tuple(_general_rebind_callbacks)
    for callback in callbacks:
        try:
            callback(client)
        except Exception as exc:  # nosec B110 - 单个模块重绑定不能阻断恢复
            log.warning("general HTTP client rebind failed: %s", type(exc).__name__)


def _notify_auth_rebind(client: httpx.AsyncClient) -> None:
    with _thread_lock:
        callbacks = tuple(_auth_rebind_callbacks)
    for callback in callbacks:
        try:
            callback(client)
        except Exception as exc:  # nosec B110 - 单个模块重绑定不能阻断恢复
            log.warning("auth HTTP client rebind failed: %s", type(exc).__name__)


def _new_auth_client() -> httpx.AsyncClient:
    """按当前热配置创建 Token 续期专用 Client。"""
    try:
        concurrency = max(1, int(getattr(cfg, "TOKEN_REFRESH_CONCURRENCY", 2)))
    except (TypeError, ValueError):
        concurrency = 2
    return httpx.AsyncClient(
        timeout=15,
        proxy=getattr(cfg, "PROXY", "") or None,
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=max(2, concurrency * 2),
            max_keepalive_connections=concurrency,
        ),
    )


def _is_client_alive(client: httpx.AsyncClient | None) -> bool:
    """检查 client 是否存在且未关闭。"""
    if client is None:
        return False
    try:
        return not client.is_closed
    except Exception:
        return False


def _is_current_loop_client(client: httpx.AsyncClient | None, owner: asyncio.AbstractEventLoop | None) -> bool:
    """只复用属于当前运行事件循环的 Client，避免跨 loop 使用 transport。"""
    return _is_client_alive(client) and owner is asyncio.get_running_loop()


async def _close_quietly(
    client: httpx.AsyncClient | None,
    *,
    timeout: float | None = None,
) -> bool:
    """在有限预算内关闭被替换的 Client；跨 loop/坏 transport 不阻塞恢复。"""
    if client is None:
        return True
    try:
        if client.is_closed:
            return True
    except Exception as exc:  # nosec B110
        log.debug("checking stale HTTP client failed: %s", type(exc).__name__)
        return False

    close_task = asyncio.create_task(client.aclose(), name="http-client-close")
    budget = max(
        0.01,
        float(CLIENT_CLOSE_TIMEOUT_SECONDS if timeout is None else timeout),
    )
    try:
        await asyncio.wait_for(asyncio.shield(close_task), timeout=budget)
        return True
    except asyncio.TimeoutError:
        close_task.cancel()
        await asyncio.gather(close_task, return_exceptions=True)
        log.warning("closing stale HTTP client timed out after %.1fs", budget)
        return False
    except asyncio.CancelledError:
        # 上层取消不能吞掉；但先给 close 一个有限的清理窗口。
        try:
            await asyncio.wait_for(asyncio.shield(close_task), timeout=budget)
        except asyncio.TimeoutError:
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
        raise
    except Exception as exc:  # nosec B110
        log.debug("closing stale HTTP client failed: %s", type(exc).__name__)
        return False


async def get_general_client() -> httpx.AsyncClient:
    """获取或自愈重建通用 HTTP 异步客户端。"""
    global _general_client, _general_loop
    loop = asyncio.get_running_loop()
    async with _get_lifecycle_lock():
        with _thread_lock:
            if _is_current_loop_client(_general_client, _general_loop):
                return _general_client  # type: ignore[return-value]
            stale = _general_client
            proxy_url = getattr(cfg, "PROXY", "") or None
            _general_client = httpx.AsyncClient(
                timeout=getattr(cfg, "TIMEOUT", 30),
                proxy=proxy_url,
                follow_redirects=True,
            )
            _general_loop = loop
            current = _general_client
        _notify_general_rebind(current)
        await _close_quietly(stale)
        return current


async def get_auth_client() -> httpx.AsyncClient:
    """获取或自愈重建 Token 续期专用 HTTP 客户端。"""
    global _auth_client, _auth_loop
    loop = asyncio.get_running_loop()
    async with _get_lifecycle_lock():
        with _thread_lock:
            if _is_current_loop_client(_auth_client, _auth_loop):
                return _auth_client  # type: ignore[return-value]
            stale = _auth_client
            _auth_client = _new_auth_client()
            _auth_loop = loop
            current = _auth_client
        _notify_auth_rebind(current)
        await _close_quietly(stale)
        return current


async def get_qq_client() -> httpx.AsyncClient:
    """获取或自愈重建 QQ 专属 HTTP 异步客户端（强制直连）。"""
    global _qq_client, _qq_loop
    loop = asyncio.get_running_loop()
    async with _get_lifecycle_lock():
        with _thread_lock:
            if _is_current_loop_client(_qq_client, _qq_loop):
                return _qq_client  # type: ignore[return-value]
            stale = _qq_client
            _qq_client = httpx.AsyncClient(
                timeout=getattr(cfg, "TIMEOUT", 30),
                proxy=None,
                follow_redirects=True,
            )
            _qq_loop = loop
            current = _qq_client
        await _close_quietly(stale)
        return current


async def get_blog_client() -> httpx.AsyncClient:
    """获取或自愈重建博客抓取 HTTP 异步客户端。"""
    global _blog_client, _blog_loop
    loop = asyncio.get_running_loop()
    async with _get_lifecycle_lock():
        with _thread_lock:
            if _is_current_loop_client(_blog_client, _blog_loop):
                return _blog_client  # type: ignore[return-value]
            stale = _blog_client
            proxy_url = getattr(cfg, "PROXY", "") or None
            _blog_client = httpx.AsyncClient(
                timeout=getattr(cfg, "TIMEOUT", 30),
                proxy=proxy_url,
                follow_redirects=True,
            )
            _blog_loop = loop
            current = _blog_client
        await _close_quietly(stale)
        return current


async def reset_general_client() -> httpx.AsyncClient:
    """强制重置并返回全新的通用 HTTP 客户端（用于 Loop 变动自愈）。"""
    global _general_client, _general_loop
    loop = asyncio.get_running_loop()
    async with _get_lifecycle_lock():
        with _thread_lock:
            stale = _general_client
            proxy_url = getattr(cfg, "PROXY", "") or None
            _general_client = httpx.AsyncClient(
                timeout=getattr(cfg, "TIMEOUT", 30),
                proxy=proxy_url,
                follow_redirects=True,
            )
            _general_loop = loop
            current = _general_client
        _notify_general_rebind(current)
        closed = await _close_quietly(stale)
        log.info(
            "general HTTP client reset complete (old_client_closed=%s)",
            closed,
        )
        return current


async def reset_auth_client(
    *,
    expected_client: httpx.AsyncClient | None = None,
) -> httpx.AsyncClient:
    """重建 Token 认证池；并发故障只允许旧池的首个请求执行替换。

    ``expected_client`` 是发现故障的 Client。若另一个协程已先完成替换，
    当前调用直接复用新池，避免多个账号同时超时时反复关闭彼此的新连接。
    """
    global _auth_client, _auth_loop
    loop = asyncio.get_running_loop()
    async with _get_lifecycle_lock():
        with _thread_lock:
            if expected_client is not None and _auth_client is not expected_client:
                # 调用方持有的不是当前由本模块托管的认证池：要么其他协程已经
                # 完成替换，要么是独立工具自行注入的 Client。前者直接复用新池，
                # 后者不擅自接管其生命周期。
                if _is_current_loop_client(_auth_client, _auth_loop):
                    return _auth_client  # type: ignore[return-value]
                return expected_client
            stale = _auth_client
            _auth_client = _new_auth_client()
            _auth_loop = loop
            current = _auth_client
        _notify_auth_rebind(current)
        closed = await _close_quietly(stale)
        log.info(
            "auth HTTP client reset complete (old_client_closed=%s)",
            closed,
        )
        return current


async def close_all() -> None:
    """优雅关闭所有活跃的 HTTP 客户端连接池。"""
    global _general_client, _auth_client, _qq_client, _blog_client
    global _general_loop, _auth_loop, _qq_loop, _blog_loop
    async with _get_lifecycle_lock():
        with _thread_lock:
            clients = (_general_client, _auth_client, _qq_client, _blog_client)
            _general_client = None
            _auth_client = None
            _qq_client = None
            _blog_client = None
            _general_loop = None
            _auth_loop = None
            _qq_loop = None
            _blog_loop = None
        for client in clients:
            await _close_quietly(client)


__all__ = [
    "CLIENT_CLOSE_TIMEOUT_SECONDS",
    "bind_runtime_clients",
    "clear_loop_state",
    "close_all",
    "get_auth_client",
    "get_blog_client",
    "get_general_client",
    "get_qq_client",
    "register_auth_client_rebind",
    "register_general_client_rebind",
    "reset_auth_client",
    "reset_general_client",
    "unregister_auth_client_rebind",
    "unregister_general_client_rebind",
]
