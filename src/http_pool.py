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

_thread_lock = threading.Lock()
_async_locks: dict[int, asyncio.Lock] = {}


def _get_async_lock() -> asyncio.Lock:
    """按当前运行中的事件循环获取或初始化关联的 asyncio.Lock，避免跨 Loop / 线程冲突。"""
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = 0
    with _thread_lock:
        if loop_id not in _async_locks:
            _async_locks[loop_id] = asyncio.Lock()
        return _async_locks[loop_id]


def _is_client_alive(client: httpx.AsyncClient | None) -> bool:
    """检查 client 是否存在且未关闭。"""
    if client is None:
        return False
    try:
        return not client.is_closed
    except Exception:
        return False


async def get_general_client() -> httpx.AsyncClient:
    """获取或自愈重建通用 HTTP 异步客户端。"""
    global _general_client
    if _is_client_alive(_general_client):
        return _general_client  # type: ignore

    async with _get_async_lock():
        if _is_client_alive(_general_client):
            return _general_client  # type: ignore
        proxy_url = getattr(cfg, "PROXY", "") or None
        _general_client = httpx.AsyncClient(
            timeout=getattr(cfg, "TIMEOUT", 30),
            proxy=proxy_url,
            follow_redirects=True,
        )
        return _general_client


async def get_qq_client() -> httpx.AsyncClient:
    """获取或自愈重建 QQ 专属 HTTP 异步客户端（强制直连）。"""
    global _qq_client
    if _is_client_alive(_qq_client):
        return _qq_client  # type: ignore

    async with _get_async_lock():
        if _is_client_alive(_qq_client):
            return _qq_client  # type: ignore
        _qq_client = httpx.AsyncClient(
            timeout=getattr(cfg, "TIMEOUT", 30),
            proxy=None,
            follow_redirects=True,
        )
        return _qq_client


async def get_blog_client() -> httpx.AsyncClient:
    """获取或自愈重建博客抓取 HTTP 异步客户端。"""
    global _blog_client
    if _is_client_alive(_blog_client):
        return _blog_client  # type: ignore

    async with _get_async_lock():
        if _is_client_alive(_blog_client):
            return _blog_client  # type: ignore
        proxy_url = getattr(cfg, "PROXY", "") or None
        _blog_client = httpx.AsyncClient(
            timeout=getattr(cfg, "TIMEOUT", 30),
            proxy=proxy_url,
            follow_redirects=True,
        )
        return _blog_client


async def reset_general_client() -> httpx.AsyncClient:
    """强制重置并返回全新的通用 HTTP 客户端（用于 Loop 变动自愈）。"""
    global _general_client
    async with _get_async_lock():
        if _general_client:
            try:
                if not _general_client.is_closed:
                    await _general_client.aclose()
            except Exception:  # nosec B110
                pass
        proxy_url = getattr(cfg, "PROXY", "") or None
        _general_client = httpx.AsyncClient(
            timeout=getattr(cfg, "TIMEOUT", 30),
            proxy=proxy_url,
            follow_redirects=True,
        )
        return _general_client


async def close_all() -> None:
    """优雅关闭所有活跃的 HTTP 客户端连接池。"""
    global _general_client, _qq_client, _blog_client
    async with _get_async_lock():
        clients = [_general_client, _qq_client, _blog_client]
        for c in clients:
            if c is not None:
                try:
                    if not c.is_closed:
                        await c.aclose()
                except Exception:  # nosec B110
                    pass
        _general_client = None
        _qq_client = None
        _blog_client = None
