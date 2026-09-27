"""轮次内 HTTP 诊断：区分应用排队和请求耗时，不采集 URL 参数或凭证。"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import math
import time
from urllib.parse import urlsplit

from anyio import BrokenResourceError, ClosedResourceError
import httpx


def is_closed_transport_error(exc: BaseException) -> bool:
    if isinstance(exc, (ClosedResourceError, BrokenResourceError)):
        return True
    return isinstance(exc, RuntimeError) and any(
        text in str(exc).lower()
        for text in ("event loop is closed", "client has been closed", "handler is closed")
    )


def classify_http_error(exc: BaseException) -> str:
    if is_closed_transport_error(exc):
        return "closed_transport"
    for kind, name in (
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.TimeoutException, "timeout"),
        (httpx.ConnectError, "connect_error"),
        (httpx.RemoteProtocolError, "remote_protocol_error"),
        (httpx.RequestError, "request_error"),
        (asyncio.CancelledError, "cancelled"),
    ):
        if isinstance(exc, kind):
            return name
    return type(exc).__name__


@dataclass
class RequestScope:
    phase: str = "processing"
    queue_ms: float = 0
    request_ms: float = 0
    calls: int = 0
    last_error: str = "none"

    def describe(self) -> str:
        return (
            f"wait_phase={self.phase} | calls={self.calls} | last_error={self.last_error}\n"
            f"queue_ms={self.queue_ms:.0f} | request_ms={self.request_ms:.0f}"
        )


@dataclass
class RequestObservation:
    status_code: int | None = None


@dataclass
class RequestTotals:
    calls: int = 0
    active: int = 0
    peak: int = 0
    queue_ms: float = 0
    queue_max_ms: float = 0
    request_max_ms: float = 0
    # 有界采样，仅存本轮最近 512 次耗时；轮次结束后释放。
    request_samples: deque = field(default_factory=lambda: deque(maxlen=512))
    results: Counter = field(default_factory=Counter)


@dataclass
class CycleHTTPDiagnostics:
    cycle_id: str
    totals: dict[tuple[str, str, str], RequestTotals] = field(default_factory=dict)

    def report(self) -> list[str]:
        lines = []
        for (operation, host, account), totals in sorted(self.totals.items()):
            samples = sorted(totals.request_samples)
            p95 = samples[max(0, math.ceil(len(samples) * 0.95) - 1)] if samples else 0
            results = ",".join(f"{key}:{count}" for key, count in sorted(totals.results.items()))
            lines.append(
                f"HTTP 诊断 | cycle_id={self.cycle_id} | operation={operation}\n"
                f"host={host} | account_id={account or '-'}\n"
                f"calls={totals.calls} | active_peak={totals.peak} | "
                f"queue_ms_total={totals.queue_ms:.0f} | queue_ms_max={totals.queue_max_ms:.0f}\n"
                f"request_ms_p95={p95:.0f} | request_ms_max={totals.request_max_ms:.0f}\n"
                f"results={results}"
            )
        return lines


cycle_diagnostics: ContextVar[CycleHTTPDiagnostics | None] = ContextVar("http_cycle", default=None)
_request_scope: ContextVar[RequestScope | None] = ContextVar("http_scope", default=None)


@contextmanager
def request_scope():
    scope = RequestScope()
    token = _request_scope.set(scope)
    try:
        yield scope
    finally:
        _request_scope.reset(token)


def set_request_phase(phase: str) -> None:
    scope = _request_scope.get()
    if scope is not None:
        scope.phase = phase


@asynccontextmanager
async def observe_request(operation: str, url: str, *, account_id: str = "", semaphore=None):
    """测量应用信号量等待及 HTTP 调用；request_ms 包含连接池等待和网络耗时。"""
    cycle = cycle_diagnostics.get()
    scope = _request_scope.get()
    totals = None
    if cycle is not None:
        host = urlsplit(url).hostname or "unknown"
        totals = cycle.totals.setdefault((operation, host, account_id), RequestTotals())
    observation = RequestObservation()
    started = time.monotonic()
    request_started = None
    acquired = False
    result = "ok"
    phase = "semaphore_wait"
    if scope is not None:
        scope.phase = phase
    try:
        if semaphore is not None:
            await semaphore.acquire()
            acquired = True
        request_started = time.monotonic()
        phase = "http_request"
        if scope is not None:
            scope.phase = phase
            scope.calls += 1
        if totals is not None:
            totals.active += 1
            totals.peak = max(totals.peak, totals.active)
        yield observation
        if isinstance(observation.status_code, int):
            result = f"http_{observation.status_code}"
    except BaseException as exc:
        result = classify_http_error(exc)
        if result == "cancelled":
            result = f"cancelled_{phase}"
        if scope is not None:
            scope.last_error = result
        raise
    finally:
        finished = time.monotonic()
        queue_ms = ((request_started if request_started is not None else finished) - started) * 1000
        request_ms = (finished - request_started) * 1000 if request_started is not None else 0
        if acquired:
            semaphore.release()
        if scope is not None:
            scope.queue_ms += queue_ms
            scope.request_ms += request_ms
            # 取消时保留最后阶段，供外层成员/账号超时日志说明实际卡点。
            if not result.startswith("cancelled_"):
                scope.phase = "processing"
        if totals is not None:
            totals.calls += 1
            totals.queue_ms += queue_ms
            totals.queue_max_ms = max(totals.queue_max_ms, queue_ms)
            if request_started is not None:
                totals.active -= 1
                totals.request_samples.append(request_ms)
                totals.request_max_ms = max(totals.request_max_ms, request_ms)
            totals.results[result] += 1
