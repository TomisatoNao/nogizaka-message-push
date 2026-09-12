"""NapCat/OneBot 会话状态巡检。

该模块只负责读取 ``get_status``，不执行登录、重启或其它有副作用的操作。
发送侧的熔断由 :mod:`src.platforms.napcat` 负责；两者通过稳定的会话状态
字符串衔接，避免把一次富媒体传输失败误判成 QQ 掉线。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import re
import time
from typing import Callable
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx

import config.config as cfg
from src.logger import log_all


@dataclass(frozen=True)
class NapCatSessionSnapshot:
    """一次会话探针结果。"""

    state: str
    online: bool | None
    reason: str = ""
    checked_at: float = 0.0
    consecutive_failures: int = 0
    # 追加在末尾，保持旧版位置参数构造的兼容性。
    api_reachable: bool | None = None


class NapCatSessionAlertTracker:
    """将会话探针压缩为一次离线告警和一次恢复告警。

    ``NapCatSessionMonitor`` 每个周期都会回调宿主，即使状态没有变化；
    这里单独维护告警生命周期，避免同一段掉线期间重复刷屏。网络暂态
    ``unknown``/``unreachable`` 不会清除已确认的离线状态，直到探针明确
    报告 ``online`` 后才发送恢复通知。
    """

    def __init__(self) -> None:
        self._offline_alerted = False

    @property
    def offline_alerted(self) -> bool:
        """当前是否处于已经发送过离线告警的生命周期。"""

        return self._offline_alerted

    def update(self, state: str) -> str | None:
        """处理一次状态，返回 ``offline``、``recovered`` 或 ``None``。"""

        normalized = str(state or "unknown").lower()
        if normalized == "offline":
            if self._offline_alerted:
                return None
            self._offline_alerted = True
            return "offline"
        if normalized == "online" and self._offline_alerted:
            self._offline_alerted = False
            return "recovered"
        return None


def _safe_reason(value: object, limit: int = 160) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    # httpx 的异常有时会把请求 URL 放进消息；状态探针使用的 URL 可能
    # 携带 access_token，诊断信息必须可读但不能把鉴权凭证写入日志。
    text = re.sub(
        r"(?i)(access[_-]?token|authorization|token)\s*=\s*([^&\s]+)",
        r"\1=<redacted>",
        text,
    )
    return text[:limit] + ("…" if len(text) > limit else "")


def resolve_status_endpoint(raw_url: str) -> tuple[str, dict[str, str]]:
    """将发送端点转换为 ``get_status``，并提取 URL 中的访问令牌。

    新版管理端分别读取 ``NAPCAT_API_BASE`` 与 ``NAPCAT_API_TOKEN``；旧版
    ``QQ_BOT_API`` 中的 ``.../send_group_msg?access_token=...`` 也继续兼容。
    状态探针必须沿用同一个令牌，否则会把正常服务误报为 HTTP 403。
    """

    raw_value = str(raw_url or "").strip()
    configured_url = str(getattr(cfg, "QQ_BOT_API", "") or "").strip()
    raw = (raw_value or configured_url or str(getattr(cfg, "NAPCAT_API_BASE", "") or "")).strip()
    if not raw:
        raw = "http://127.0.0.1:3000"

    headers = {
        "Content-Type": "application/json",
        "User-Agent": getattr(cfg, "QQ_USER_AGENT", "") or "Mozilla/5.0",
    }
    try:
        parsed = urlparse(raw)
        path = parsed.path.rstrip("/")
        if path.endswith("/send_group_msg"):
            status_path = path[: -len("/send_group_msg")] + "/get_status"
        elif path.endswith("/get_status"):
            status_path = path
        elif not path:
            status_path = "/get_status"
        else:
            # 新版填写的是 NapCat 服务基地址（例如 /api），因此状态
            # 接口应追加到基地址，而不是误退回到上一级路径。
            status_path = path + "/get_status"
        if not status_path.startswith("/"):
            status_path = "/" + status_path
        parsed = parsed._replace(path=status_path)
        endpoint = urlunparse(parsed)
        query = parse_qs(parsed.query)
        token = (query.get("access_token") or [""])[0] or (query.get("token") or [""])[0]
        if not token and (not raw_value or raw == configured_url):
            token = str(getattr(cfg, "NAPCAT_API_TOKEN", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return endpoint, headers
    except Exception:
        # 保持与发送侧相同的安全兜底；异常 URL 会在请求阶段被标记为 unknown，
        # 但不应因为状态页拼接失败而让主进程退出。
        return "http://127.0.0.1:3000/get_status", headers


def classify_status_response(
    status_code: int,
    body: object,
    text: str = "",
) -> tuple[str, bool | None, str]:
    """解析 OneBot ``get_status`` 响应，供启动自检和后台巡检共用。"""

    excerpt = _safe_reason(text).lower()
    if isinstance(body, dict):
        for key in ("message", "wording", "msg", "error", "errMsg"):
            value = body.get(key)
            if value:
                excerpt = f"{excerpt} {_safe_reason(value)}".lower()

    if status_code in {401, 403} or "token verify failed" in excerpt:
        return "auth_failed", None, "鉴权失败"
    if any(marker in excerpt for marker in ("kickedoffline", "kicked_offline", "账号当前登录已失效", "登录已失效")):
        return "offline", False, "QQ 账号登录已失效"

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) and isinstance(body, dict):
        data = body
    if isinstance(data, dict) and data.get("online") is True:
        return "online", True, "在线"
    if isinstance(data, dict) and data.get("online") is False:
        return "offline", False, "QQ 账号离线"
    if status_code >= 500:
        return "unreachable", None, f"HTTP {status_code}"
    if status_code:
        return "unknown", None, f"HTTP {status_code}"
    return "unknown", None, "响应缺少 online 状态"


class NapCatSessionMonitor:
    """按固定间隔读取 NapCat 会话状态的后台任务。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_url: str,
        *,
        interval_seconds: float = 60.0,
        logger: Callable[..., object] | None = None,
        on_snapshot: Callable[[NapCatSessionSnapshot], object] | None = None,
    ) -> None:
        self._client = client
        self._api_url = api_url
        try:
            self._interval_seconds = min(3600.0, max(5.0, float(interval_seconds)))
        except (TypeError, ValueError):
            self._interval_seconds = 60.0
        self._log = logger or log_all
        self._on_snapshot = on_snapshot
        self._last_state: str | None = None
        self._consecutive_failures = 0
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    def _emit_log(self, message: str, **kwargs) -> None:
        """兼容项目 ``log_all`` 和只接受一个字符串的测试/宿主 logger。"""

        try:
            self._log(message, **kwargs)
        except TypeError:
            self._log(message)

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    async def check_once(self) -> NapCatSessionSnapshot:
        endpoint, headers = resolve_status_endpoint(self._api_url)
        api_reachable: bool | None = None
        try:
            response = await self._client.get(endpoint, headers=headers)
            api_reachable = bool(getattr(response, "status_code", 0))
            try:
                body = response.json()
            except Exception:
                body = None
            state, online, reason = classify_status_response(
                int(getattr(response, "status_code", 0) or 0),
                body,
                getattr(response, "text", ""),
            )
            self._consecutive_failures = self._consecutive_failures + 1 if state == "unreachable" else 0
        except httpx.TimeoutException:
            state, online, reason = "unreachable", None, "请求超时"
            api_reachable = False
            self._consecutive_failures += 1
        except (httpx.RequestError, OSError):
            state, online, reason = "unreachable", None, "网络不可达"
            api_reachable = False
            self._consecutive_failures += 1
        except Exception as exc:
            detail = _safe_reason(exc)
            reason = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            state, online = "unknown", None
            api_reachable = None
            self._consecutive_failures = 0
            self._emit_log(
                f"⚠️ NapCat 会话探针异常 | error={_safe_reason(reason)}",
                is_warning=True,
            )

        snapshot = NapCatSessionSnapshot(
            state=state,
            online=online,
            api_reachable=api_reachable,
            reason=reason,
            checked_at=time.time(),
            consecutive_failures=self._consecutive_failures,
        )
        if state != self._last_state:
            level = "🟢" if state == "online" else "⚠️"
            failures = (
                f" | consecutive_failures={self._consecutive_failures}"
                if state == "unreachable"
                else ""
            )
            self._emit_log(
                f"{level} NapCat 会话状态变更 | state={state} | "
                f"reason={_safe_reason(reason)}{failures}",
                is_warning=state != "online",
            )
            self._last_state = state
        await self._notify(snapshot)
        return snapshot

    async def _notify(self, snapshot: NapCatSessionSnapshot) -> None:
        if self._on_snapshot is None:
            return
        try:
            result = self._on_snapshot(snapshot)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self._emit_log(
                f"⚠️ NapCat 会话状态回调异常 | error={_safe_reason(type(exc).__name__)}",
                is_warning=True,
            )

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            await self.check_once()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                continue

    def start(self) -> asyncio.Task:
        """启动后台巡检；重复调用只返回已有任务。"""

        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="napcat-session-monitor")
        return self._task

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None


__all__ = [
    "NapCatSessionMonitor",
    "NapCatSessionSnapshot",
    "NapCatSessionAlertTracker",
    "classify_status_response",
    "resolve_status_endpoint",
]
