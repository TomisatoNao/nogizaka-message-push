"""
src/http_pool.py — 全局 HTTP Client 连接池与生命周期管理器

统一管理核心 AsyncClient 实例（通用请求、QQ Bot API、博客抓取），
提供 Loop 绑定自愈、状态探活、代理热重载以及优雅关闭。
"""

import asyncio
import logging
import threading
import httpx
import config.config as cfg

log = logging.getLogger("collink")

_general_client: httpx.AsyncClient | None = None
_qq_client: httpx.AsyncClient | None = None
_blog_client: httpx.AsyncClient | None = None
_general_loop: asyncio.AbstractEventLoop | None = None
_qq_loop: asyncio.AbstractEventLoop | None = None
_blog_loop: asyncio.AbstractEventLoop | None = None

_thread_lock = threading.Lock()


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


async def _close_quietly(client: httpx.AsyncClient | None) -> None:
    """尽力关闭被替换的 Client；跨 loop 的旧 transport 失败不影响新请求。"""
    if client is None:
        return
    try:
        if not client.is_closed:
            await client.aclose()
    except Exception as exc:  # nosec B110
        log.debug("closing stale HTTP client failed: %s", exc)


async def get_general_client() -> httpx.AsyncClient:
    """获取或自愈重建通用 HTTP 异步客户端。"""
    global _general_client, _general_loop
    loop = asyncio.get_running_loop()
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
    await _close_quietly(stale)
    return current


async def get_qq_client() -> httpx.AsyncClient:
    """获取或自愈重建 QQ 专属 HTTP 异步客户端（强制直连）。"""
    global _qq_client, _qq_loop
    loop = asyncio.get_running_loop()
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
    await _close_quietly(stale)
    return current


async def close_all() -> None:
    """优雅关闭所有活跃的 HTTP 客户端连接池。"""
    global _general_client, _qq_client, _blog_client, _general_loop, _qq_loop, _blog_loop
    with _thread_lock:
        clients = (_general_client, _qq_client, _blog_client)
        _general_client = None
        _qq_client = None
        _blog_client = None
        _general_loop = None
        _qq_loop = None
        _blog_loop = None
    for client in clients:
        await _close_quietly(client)
