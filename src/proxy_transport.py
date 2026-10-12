"""HTTP CONNECT 握手失败时回收连接，防止 httpcore 1.0.9 的永久池耗尽。

上游追踪：https://github.com/encode/httpcore/pull/1049
只扩展本项目创建的 transport，不修改 httpcore 的全局类。依赖内部接口的
部分集中在本文件，升级 httpcore 时必须运行真实代理握手回归测试。
"""

import logging

import httpcore
import httpx
from httpcore._async.http_proxy import AsyncTunnelHTTPConnection
from httpcore._synchronization import AsyncShieldCancellation

log = logging.getLogger("collink")


class _ClosingTunnelConnection(AsyncTunnelHTTPConnection):
    async def handle_async_request(self, request):
        try:
            return await super().handle_async_request(request)
        except BaseException:
            # CONNECT 200 后 TLS 失败/取消时，底层 HTTP/1.1 仍停留在 ACTIVE。
            # 显式关闭使池能够移除它；已建好的隧道仍由上游管理响应生命周期。
            if not self._connected:
                with AsyncShieldCancellation():
                    try:
                        await self.aclose()
                    except Exception as cleanup_error:
                        # 不能让清理异常覆盖真正的网络错误或取消信号。
                        log.warning("proxy tunnel cleanup failed: %s", type(cleanup_error).__name__)
            raise


class _ClosingHTTPProxy(httpcore.AsyncHTTPProxy):
    def create_connection(self, origin):
        if origin.scheme == b"http":
            return super().create_connection(origin)
        return _ClosingTunnelConnection(
            proxy_origin=self._proxy_url.origin,
            remote_origin=origin,
            proxy_headers=self._proxy_headers,
            ssl_context=self._ssl_context,
            proxy_ssl_context=self._proxy_ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            network_backend=self._network_backend,
            socket_options=self._socket_options,
        )


class ClosingHTTPProxyTransport(httpx.AsyncHTTPTransport):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        pool = self._pool
        if not isinstance(pool, httpcore.AsyncHTTPProxy):
            return
        # 复用 HTTPX 生成的 TLS 上下文、认证头和限额，避免重新解释代理配置。
        # 构造阶段旧池尚无连接，无需异步关闭，也不会影响其他客户端。
        self._pool = _ClosingHTTPProxy(
            proxy_url=pool._proxy_url,
            proxy_headers=pool._proxy_headers,
            ssl_context=pool._ssl_context,
            proxy_ssl_context=pool._proxy_ssl_context,
            max_connections=pool._max_connections,
            max_keepalive_connections=pool._max_keepalive_connections,
            keepalive_expiry=pool._keepalive_expiry,
            http1=pool._http1,
            http2=pool._http2,
            network_backend=pool._network_backend,
            socket_options=pool._socket_options,
        )


def new_async_client(*, proxy=None, **kwargs):
    """保留 HTTPX 的代理/直连语义，为显式 HTTP(S) 代理安装回收修复。"""
    if proxy and httpx.URL(proxy).scheme in ("http", "https"):
        transport = ClosingHTTPProxyTransport(
            proxy=proxy,
            limits=kwargs.get("limits", httpx.Limits()),
            trust_env=kwargs.get("trust_env", True),
        )
        # 显式 mounts 覆盖 HTTPX 为 proxy 自动创建的 all:// transport。
        # 不重试、不重放请求；认证 POST 的重试仍由 credentials 决定。
        kwargs["mounts"] = {"all://": transport}
    return httpx.AsyncClient(proxy=proxy, **kwargs)
