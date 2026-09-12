"""NapCat/OneBot 入站群消息监听。

NapCat 原有的 ``send_group_msg`` 发送链路保持不变。本模块只负责接收
OneBot 群消息事件，在严格的网络/Token/群路由白名单边界内提取社媒链接，
再把耗时的解析、下载和回复工作放入有界后台队列。

默认使用独立 HTTP 端口接收 HTTP POST 事件。NapCat 位于同一 Docker
Compose 时可以通过 Docker 内网访问该端口；位于 Windows 时应通过 NAS
局域网或 VPN 访问。实现同时保留反向 WebSocket 入口，便于后续切换而不
改变事件处理逻辑。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
import hmac
import http.server
import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

import config.config as cfg

from src.logger import log_all
from src.social.url_utils import extract_social_urls


DEFAULT_SETTINGS: dict[str, object] = {
    "enabled": False,
    "transport": "http_post",
    "listen_host": "0.0.0.0",
    "listen_port": 46047,
    "event_path": "/api/napcat/events",
    "translate": False,
    "archive": False,
    "max_links_per_message": 1,
    "queue_size": 32,
    "workers": 2,
    "cooldown_seconds": 10,
    "dedupe_ttl_seconds": 900,
    "request_timeout_seconds": 180,
    "max_message_bytes": 65536,
}

_MAX_PORT = 65535
_MIN_PORT = 1
_MAX_WORKERS = 8
_MAX_QUEUE_SIZE = 256
_MAX_MESSAGE_BYTES = 1024 * 1024
_MAX_DEDUPE_ENTRIES = 4096
_MAX_URLS_PER_MESSAGE = 4


def _safe_log_fragment(value: object, *, limit: int = 64) -> str:
    """将事件中的可控 ID 限制为单行日志片段。"""

    text = str(value or "")
    safe = "".join(
        char if (char.isalnum() or char in "-_.:") else "_"
        for char in text
    )
    return safe[:limit] or "unknown"


def _safe_log_text(value: object, *, limit: int = 96) -> str:
    """保留可读字符，同时移除换行和控制字符，避免日志注入/刷屏。"""

    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = "".join(
        char if ord(char) >= 0x20 and char != "\x7f" else " "
        for char in text
    )
    if len(text) > limit:
        return text[: max(1, limit - 3)] + "..."
    return text or "-"


def _safe_url_preview(value: object, *, limit: int = 96) -> str:
    """只显示社媒链接的协议、域名和路径，不把查询参数写入日志。"""

    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        preview = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except ValueError:
        preview = raw
    return _safe_log_text(preview, limit=limit)


def _as_bool(value: object, default: bool = False) -> bool:
    """解析配置中的布尔值，避免字符串 ``"false"`` 被当成 True。"""

    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def _safe_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _safe_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def normalize_group_id(value: object) -> str | None:
    """把 OneBot/config 中的群号统一为正整数文本。"""

    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        return None
    return str(int(text))


def configured_group_ids(routes: object) -> frozenset[str]:
    """只从 NapCat 路由读取入站白名单，不维护第二份群号配置。"""

    if not isinstance(routes, (list, tuple)):
        return frozenset()
    groups: set[str] = set()
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        group_id = normalize_group_id(route.get("group_id"))
        if group_id:
            groups.add(group_id)
    return frozenset(groups)


def event_message_text(message: object, raw_message: object = "") -> str:
    """从 OneBot 字符串或消息段数组提取可扫描的文本。"""

    parts: list[str] = []
    if isinstance(message, str):
        parts.append(message)
    elif isinstance(message, list):
        for segment in message:
            if not isinstance(segment, Mapping):
                continue
            if str(segment.get("type") or "").lower() != "text":
                continue
            data = segment.get("data")
            if isinstance(data, Mapping) and data.get("text") is not None:
                parts.append(str(data.get("text")))
    if raw_message and str(raw_message) not in parts:
        parts.append(str(raw_message))
    return "\n".join(part for part in parts if part)


def _auth_value(headers: Mapping[str, object]) -> str:
    """读取常见 OneBot 鉴权头，不读取 URL 查询参数，避免 Token 泄露到日志。"""

    def _get(name: str) -> object:
        value = headers.get(name)
        if value is not None:
            return value
        # BaseHTTPRequestHandler 的 HTTPMessage 本身大小写不敏感，
        # 这里同时兼容单元测试和 WebSocket 库返回的普通 dict。
        lowered = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == lowered:
                return candidate
        return None

    authorization = str(_get("Authorization") or "").strip()
    if authorization:
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return authorization
    for key in ("X-OneBot-Token", "X-Auth-Token"):
        value = str(_get(key) or "").strip()
        if value:
            return value
    return ""


def _header_token_ok(headers: Mapping[str, object], expected: str) -> bool:
    token = _auth_value(headers)
    return bool(expected) and bool(token) and hmac.compare_digest(token, expected)


@dataclass(frozen=True)
class NapCatInboundJob:
    """已经通过入口校验、等待后台解析的一条链接任务。"""

    group_id: str
    user_id: str
    message_id: str
    url: str
    received_at: float
    # 事件中的机器人 QQ 号；用于合并转发自定义节点的 user_id。
    # 放在末尾并提供默认值，兼容旧版插件/测试的五参数构造。
    self_id: str = ""


class _InboundHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class NapCatInboundListener:
    """NapCat 入站事件监听与有界社媒解析队列。"""

    def __init__(
        self,
        config_provider: Callable[[], Mapping[str, object]] | None = None,
        *,
        service_provider: Callable[[], object | None] | None = None,
        logger: Callable[..., object] | None = None,
        event_token: str | None = None,
    ) -> None:
        self._config_provider = config_provider or self._default_config
        self._uses_default_config = config_provider is None
        self._service_provider = service_provider
        self._log = logger or log_all
        self._event_token = (
            str(event_token).strip()
            if event_token is not None
            else os.getenv("NAPCAT_EVENT_TOKEN", "").strip()
        )
        self._event_token_from_env = event_token is None
        self._queue: queue.Queue[NapCatInboundJob | None] = queue.Queue(maxsize=32)
        self._workers: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None
        self._server: _InboundHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._ws_server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._settings_snapshot: dict[str, object] = dict(DEFAULT_SETTINGS)
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._last_group_at: dict[str, float] = {}
        self._last_sender_at: dict[tuple[str, str], float] = {}
        self._state_lock = threading.RLock()
        self._stats: dict[str, int] = {
            "received": 0,
            "queued": 0,
            "ignored": 0,
            "rejected": 0,
            "duplicate": 0,
            "rate_limited": 0,
            "queue_full": 0,
            "succeeded": 0,
            "failed": 0,
        }
        # 允许单元测试/嵌入式调用在 start() 前直接提交事件，同时让自定义
        # config_provider 的设置立即生效；正式启动时仍会再次刷新快照。
        try:
            self._settings_snapshot = self._settings()
        except Exception:
            self._settings_snapshot = dict(DEFAULT_SETTINGS)

    @staticmethod
    def _default_config() -> Mapping[str, object]:
        raw = getattr(cfg, "_config", {})
        return raw if isinstance(raw, Mapping) else {}

    @property
    def running(self) -> bool:
        return self._server is not None or self._ws_server is not None

    @property
    def settings(self) -> dict[str, object]:
        return dict(self._settings_snapshot)

    def _emit(self, message: str, **kwargs) -> None:
        try:
            self._log(message, **kwargs)
        except TypeError:
            self._log(message)

    def _raw_config(self) -> Mapping[str, object]:
        try:
            value = self._config_provider()
        except Exception as exc:
            self._emit(
                f"⚠️ NapCat 入站读取配置失败 | error={type(exc).__name__}",
                is_warning=True,
            )
            return {}
        return value if isinstance(value, Mapping) else {}

    def _settings(self) -> dict[str, object]:
        raw = self._raw_config().get("napcat_inbound")
        values = dict(DEFAULT_SETTINGS)
        if isinstance(raw, Mapping):
            values.update(raw)
        values["enabled"] = _as_bool(values.get("enabled", False))
        transport = str(values.get("transport") or "http_post").strip().lower()
        values["transport"] = "reverse_ws" if transport in {"reverse_ws", "websocket", "ws"} else "http_post"
        values["listen_host"] = str(values.get("listen_host") or "0.0.0.0").strip() or "0.0.0.0"
        values["listen_port"] = _safe_int(values.get("listen_port"), 46047, _MIN_PORT, _MAX_PORT)
        path = str(values.get("event_path") or "/api/napcat/events").strip()
        if (
            not path.startswith("/")
            or len(path) > 128
            or any(char.isspace() or ord(char) < 0x20 for char in path)
        ):
            path = "/api/napcat/events"
        values["event_path"] = path
        values["translate"] = _as_bool(values.get("translate", False))
        values["archive"] = _as_bool(values.get("archive", False))
        values["max_links_per_message"] = _safe_int(
            values.get("max_links_per_message"), 1, 1, _MAX_URLS_PER_MESSAGE
        )
        values["queue_size"] = _safe_int(values.get("queue_size"), 32, 1, _MAX_QUEUE_SIZE)
        values["workers"] = _safe_int(values.get("workers"), 2, 1, _MAX_WORKERS)
        values["cooldown_seconds"] = _safe_float(
            values.get("cooldown_seconds"), 10.0, 0.0, 3600.0
        )
        values["dedupe_ttl_seconds"] = _safe_float(
            values.get("dedupe_ttl_seconds"), 900.0, 1.0, 86400.0
        )
        values["request_timeout_seconds"] = _safe_float(
            values.get("request_timeout_seconds"), 180.0, 5.0, 900.0
        )
        values["max_message_bytes"] = _safe_int(
            values.get("max_message_bytes"), 65536, 1024, _MAX_MESSAGE_BYTES
        )
        return values

    def _napcat_enabled(self) -> bool:
        """入站处理必须同时服从全局 NapCat 通道开关。"""

        raw = self._raw_config()
        # 生产默认 provider 需要保留 .env 对 JSON 的覆盖优先级；测试或
        # 嵌入调用传入 provider 时，则以该 provider 的最新配置为准。
        if self._uses_default_config:
            value = getattr(cfg, "ENABLE_NAPCAT_QQ", None)
            if value is not None:
                return _as_bool(value)
        if "enable_napcat_qq" in raw:
            return _as_bool(raw.get("enable_napcat_qq"))
        channels = raw.get("channels")
        if isinstance(channels, Mapping) and "napcat" in channels:
            return _as_bool(channels.get("napcat"))
        # 自定义 provider 不应意外继承进程全局开关；缺少明确的通道
        # 配置时按关闭处理，避免测试/嵌入场景绕过安全边界。
        if self._uses_default_config:
            value = getattr(cfg, "ENABLE_NAPCAT_QQ", None)
            return _as_bool(value, True) if value is not None else True
        return False

    def _allowed_groups(self) -> frozenset[str]:
        raw = self._raw_config()
        configured = raw.get("napcat_routes")
        if self._uses_default_config:
            configured = getattr(cfg, "NAPCAT_ROUTES", configured)
        if configured is None:
            configured = []
        return configured_group_ids(configured)

    def _emit_job_summary(
        self,
        job: NapCatInboundJob,
        request_id: str,
        operation: object | None,
        elapsed: float,
        *,
        error: str = "",
    ) -> None:
        """用一行稳定摘要收束一次入站任务，避免用户在多条 DEBUG 中拼接结果。"""

        post = getattr(operation, "post", None) if operation is not None else None
        delivery = getattr(operation, "delivery", None) if operation is not None else None
        platform = _safe_log_text(getattr(post, "platform", "unknown"), limit=12)
        post_id = _safe_log_text(getattr(post, "post_id", "-"), limit=18)
        try:
            media = getattr(post, "media", ()) or ()
            media_total = len(media)
        except (TypeError, AttributeError):
            media_total = 0

        def _count(name: str) -> int:
            try:
                return max(0, int(getattr(delivery, name, 0)))
            except (TypeError, ValueError):
                return 0

        success_routes = _count("success_routes")
        matched_routes = _count("matched_routes")
        try:
            media_sent = max(0, int(getattr(delivery, "media_sent", 0)))
        except (TypeError, ValueError):
            media_sent = 0
        completed = bool(getattr(operation, "completed", False)) and not error
        icon = "✅" if completed else "❌"
        status = "成功" if completed else "失败"
        # 关键摘要控制在终端单行限制附近；完整 request_id/post_id 仍可从前面的
        # 社媒 DEBUG 日志按同一 request_id 查询，避免再次触发 [TRUNCATED]。
        line = (
            f"{icon} NapCat {status} | 群{_safe_log_fragment(job.group_id, limit=16)} | "
            f"{platform}/{post_id} | 路由{success_routes}/{matched_routes} | "
            f"媒体{media_sent}/{media_total} | {max(0.0, elapsed):.1f}s | "
            f"req={_safe_log_text(request_id, limit=34)}"
        )
        if error:
            detail = _safe_log_text(error, limit=160)
            self._emit(f"{line}\n   error={detail}", is_error=True)
        elif completed:
            self._emit(line)
        else:
            outcome = _safe_log_text(getattr(delivery, "outcome", "failed"), limit=24)
            raw_errors = getattr(delivery, "errors", ()) if delivery is not None else ()
            if isinstance(raw_errors, (list, tuple)) and raw_errors:
                detail = _safe_log_text("; ".join(str(item) for item in raw_errors[:2]), limit=160)
                self._emit(f"{line}\n   outcome={outcome} | error={detail}", is_error=True)
            else:
                self._emit(f"{line}\n   outcome={outcome}", is_error=True)

    def _resize_queue(self, maxsize: int) -> None:
        if self._queue.maxsize == maxsize:
            return
        # Queue consumers may be polling this object from other asyncio tasks.
        # Never replace a live queue: doing so can pair get/task_done calls with
        # different Queue instances and lose jobs during a hot reload.
        if self._workers:
            return
        pending: list[NapCatInboundJob | None] = []
        while True:
            try:
                pending.append(self._queue.get_nowait())
            except queue.Empty:
                break
        self._queue = queue.Queue(maxsize=maxsize)
        for item in pending[:maxsize]:
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                break

    def _mark(self, key: str, amount: int = 1) -> None:
        with self._state_lock:
            self._stats[key] = self._stats.get(key, 0) + amount

    def stats(self) -> dict[str, object]:
        with self._state_lock:
            snapshot = dict(self._stats)
            snapshot.update(
                queue_depth=self._queue.qsize(),
                queue_capacity=self._queue.maxsize,
                running=self.running,
                allowed_groups=len(self._allowed_groups()),
            )
            return snapshot

    def _event_key(self, event: Mapping[str, object], message_id: str) -> str:
        if message_id:
            # OneBot message_id 在不同实现中可能只在单群/单连接内唯一，
            # 把群号和机器人号纳入去重键，避免跨群误丢弃合法链接。
            group_id = normalize_group_id(event.get("group_id")) or "-"
            self_id = str(event.get("self_id") or "-").strip()
            return f"message:{self_id}:{group_id}:{message_id}"
        encoded = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return "hash:" + sha256(encoded.encode("utf-8", "replace")).hexdigest()

    def _prune_state(self, now: float, dedupe_ttl: float) -> None:
        while self._seen:
            _, timestamp = next(iter(self._seen.items()))
            if now - timestamp <= dedupe_ttl and len(self._seen) <= _MAX_DEDUPE_ENTRIES:
                break
            self._seen.popitem(last=False)
        # 防止热群长期运行时频率表无限增长。
        for key, timestamp in list(self._last_sender_at.items()):
            if now - timestamp > max(dedupe_ttl, 3600.0):
                self._last_sender_at.pop(key, None)
        for key, timestamp in list(self._last_group_at.items()):
            if now - timestamp > max(dedupe_ttl, 3600.0):
                self._last_group_at.pop(key, None)

    def accept_event(self, event: object, *, source: str = "") -> str:
        """校验并入队一条 OneBot 事件，返回内部状态名。"""

        self._mark("received")
        if not isinstance(event, Mapping):
            self._mark("rejected")
            return "invalid_event"
        if str(event.get("post_type") or "").lower() != "message":
            self._mark("ignored")
            return "ignored_type"
        if str(event.get("message_type") or "").lower() != "group":
            self._mark("ignored")
            return "ignored_scope"

        group_id = normalize_group_id(event.get("group_id"))
        if not group_id or group_id not in self._allowed_groups():
            self._mark("rejected")
            return "group_not_allowed"

        self_id = str(event.get("self_id") or "").strip()
        user_id = str(event.get("user_id") or "").strip()
        # OneBot 兼容实现偶尔只在 sender 对象中提供发送者 ID；也要
        # 用它做自消息判断，避免服务自己的回帖再次触发入站解析。
        sender = event.get("sender")
        if not user_id and isinstance(sender, Mapping):
            user_id = str(sender.get("user_id") or "").strip()
        if self_id and user_id and self_id == user_id:
            self._mark("ignored")
            return "self_message"

        settings = self._settings_snapshot
        max_links = int(settings.get("max_links_per_message", 1))
        text = event_message_text(event.get("message"), event.get("raw_message"))
        urls = extract_social_urls(text, max_urls=max_links)
        if not urls:
            self._mark("ignored")
            return "no_social_url"

        try:
            message_id = str(event.get("message_id") or "").strip()
        except Exception:
            message_id = ""
        now = time.monotonic()
        dedupe_ttl = float(settings.get("dedupe_ttl_seconds", 900.0))
        cooldown = float(settings.get("cooldown_seconds", 10.0))
        event_key = self._event_key(event, message_id)
        with self._state_lock:
            self._prune_state(now, dedupe_ttl)
            if event_key in self._seen and now - self._seen[event_key] <= dedupe_ttl:
                self._mark("duplicate")
                return "duplicate"
            self._seen[event_key] = now
            group_last = self._last_group_at.get(group_id, 0.0)
            sender_key = (group_id, user_id)
            sender_last = self._last_sender_at.get(sender_key, 0.0)
            if cooldown and (now - group_last < cooldown or now - sender_last < cooldown):
                self._mark("rate_limited")
                return "rate_limited"
            self._last_group_at[group_id] = now
            if user_id:
                self._last_sender_at[sender_key] = now

        queued = 0
        for index, url in enumerate(urls):
            job = NapCatInboundJob(
                group_id=group_id,
                user_id=user_id,
                message_id=(message_id or event_key) + (f":{index}" if len(urls) > 1 else ""),
                url=url,
                received_at=time.time(),
                self_id=self_id,
            )
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                self._mark("queue_full")
                break
            queued += 1
            self._mark("queued")
        request_id = f"napcat-inbound-{_safe_log_fragment(message_id or event_key)}"[:96]
        sender_label = user_id or "unknown"
        if isinstance(sender, Mapping):
            display_name = str(sender.get("card") or sender.get("nickname") or "").strip()
            if display_name:
                sender_label = f"{display_name}/{sender_label}"
        source_label = _safe_log_text(source or "unknown", limit=12)
        if queued:
            queue_state = "已入队" if queued == len(urls) else f"部分入队{queued}/{len(urls)}"
            self._emit(
                f"📥 NapCat 收到链接 | 来自群{group_id}/{_safe_log_text(sender_label, limit=32)} | "
                f"入口={source_label} | {_safe_url_preview(urls[0])} | {queue_state} | "
                f"req={_safe_log_text(request_id, limit=34)}"
            )
        else:
            self._emit(
                f"⚠️ NapCat 链接入队失败 | 群{group_id} | 入口={source_label} | "
                f"原因=queue_full | req={_safe_log_text(request_id, limit=34)}",
                is_warning=True,
            )
        return "queued" if queued else "queue_full"

    async def _process_job(self, job: NapCatInboundJob) -> object:
        config = self._raw_config()
        service = self._service_provider() if self._service_provider else None
        if service is None:
            from src.social.service import SocialService

            service = SocialService(dict(config))

        request_id = f"napcat-inbound-{_safe_log_fragment(job.message_id)}"[:96]
        from src.social.contracts import DeliveryTarget

        target_runtime: dict[str, object] = {"group_id": int(job.group_id)}
        if job.self_id:
            target_runtime["self_id"] = job.self_id
        target = DeliveryTarget(
            channel="napcat",
            target_id=job.group_id,
            scope="groups",
        ).bind_runtime(
            target_runtime,
            route_id=f"napcat:inbound:{job.group_id}",
        )
        # 统一复用 SocialService 的解析、下载、格式化、投递和可选归档
        # 编排；整个同步流程放到线程中，HTTP/WS 事件循环只负责排队。
        operation = await asyncio.to_thread(
            service.process_url,
            job.url,
            targets=[target],
            translate=_as_bool(self._settings_snapshot.get("translate", False)),
            archive=_as_bool(self._settings_snapshot.get("archive", False)),
            request_id=request_id,
        )
        return operation

    async def _worker(self, worker_index: int) -> None:
        while self._stop_event is None or not self._stop_event.is_set():
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if job is None:
                self._queue.task_done()
                return
            request_id = f"napcat-inbound-{_safe_log_fragment(job.message_id)}"[:96]
            started_at = time.monotonic()
            operation: object | None = None
            try:
                timeout = float(self._settings_snapshot.get("request_timeout_seconds", 180.0))
                operation = await asyncio.wait_for(
                    self._process_job(job), timeout=timeout
                )
                elapsed = time.monotonic() - started_at
                if not getattr(operation, "completed", False):
                    self._mark("failed")
                    self._emit_job_summary(job, request_id, operation, elapsed)
                    continue
                self._mark("succeeded")
                self._emit_job_summary(job, request_id, operation, elapsed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark("failed")
                self._emit_job_summary(
                    job,
                    request_id,
                    operation,
                    time.monotonic() - started_at,
                    error=f"{type(exc).__name__}: {_safe_log_text(exc, limit=140)}",
                )
            finally:
                self._queue.task_done()

    def _response(self, handler: http.server.BaseHTTPRequestHandler, code: int, payload: Mapping[str, object]) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        try:
            handler.send_response(code)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _handle_http(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        path = handler.path.split("?", 1)[0]
        expected_path = str(self._settings_snapshot.get("event_path", "/api/napcat/events"))
        if path != expected_path:
            self._response(handler, 404, {"status": "not_found"})
            return

        if not _header_token_ok(handler.headers, self._event_token):
            self._mark("rejected")
            self._response(handler, 401, {"status": "unauthorized"})
            return

        raw_length = handler.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._mark("rejected")
            self._response(handler, 400, {"status": "invalid_content_length"})
            return
        max_bytes = int(self._settings_snapshot.get("max_message_bytes", 65536))
        if length <= 0 or length > max_bytes:
            self._mark("rejected")
            self._response(handler, 413 if length > max_bytes else 400, {"status": "invalid_body_size"})
            return
        try:
            body = handler.rfile.read(length)
            event = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            self._mark("rejected")
            self._response(handler, 400, {"status": "invalid_json"})
            return

        result = self.accept_event(event, source="http")
        # 对已鉴权但被过滤/限流的事件返回 200，避免 NapCat 因业务过滤
        # 自动重试；真正的失败只记录在本地统计和日志中。
        self._response(
            handler,
            200,
            {
                "status": "ok",
                "retcode": 0,
                "accepted": result == "queued",
            },
        )

    def _make_http_handler(self):
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                # 事件体很小；限制单连接读超时，避免错误配置或恶意慢速
                # 客户端长期占满 ThreadingHTTPServer 的工作线程。
                self.connection.settimeout(10.0)

            def do_POST(self) -> None:  # noqa: N802
                listener._handle_http(self)

            def do_GET(self) -> None:  # noqa: N802
                listener._response(self, 404, {"status": "not_found"})

            def log_message(self, _format: str, *_args) -> None:
                return

        return Handler

    async def _ws_handler(self, websocket, path: str) -> None:
        expected_path = str(self._settings_snapshot.get("event_path", "/api/napcat/events"))
        if str(path).split("?", 1)[0] != expected_path:
            await websocket.close(code=4404, reason="not found")
            return
        headers = getattr(websocket, "request_headers", {})
        if not _header_token_ok(headers, self._event_token):
            self._mark("rejected")
            await websocket.close(code=4401, reason="unauthorized")
            return
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > int(
                    self._settings_snapshot.get("max_message_bytes", 65536)
                ):
                    await websocket.close(code=1009, reason="message too large")
                    return
                try:
                    event = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    self._mark("rejected")
                    continue
                self.accept_event(event, source="websocket")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit(
                f"⚠️ NapCat 入站 WebSocket 断开 | error={type(exc).__name__}",
                is_warning=True,
            )

    async def _start_transport(self) -> None:
        transport = str(self._settings_snapshot.get("transport") or "http_post")
        host = str(self._settings_snapshot.get("listen_host") or "0.0.0.0")
        port = int(self._settings_snapshot.get("listen_port") or 46047)
        if transport == "reverse_ws":
            try:
                from websockets.legacy.server import serve
            except ImportError as exc:
                raise RuntimeError("缺少 websockets 依赖，无法启动 NapCat 反向 WebSocket") from exc
            self._ws_server = await serve(
                self._ws_handler,
                host,
                port,
                max_size=int(self._settings_snapshot.get("max_message_bytes", 65536)),
                ping_interval=20,
                ping_timeout=20,
            )
            return

        server = _InboundHTTPServer((host, port), self._make_http_handler())
        self._server = server
        self._server_thread = threading.Thread(
            target=server.serve_forever,
            name="napcat-inbound-http",
            daemon=True,
        )
        self._server_thread.start()

    async def start(self) -> bool:
        """按当前配置启动；未启用、未鉴权或 NapCat 全局关闭时保持停止。"""

        if self.running:
            return True
        if self._event_token_from_env:
            self._event_token = os.getenv("NAPCAT_EVENT_TOKEN", "").strip()
        self._settings_snapshot = self._settings()
        if not bool(self._settings_snapshot.get("enabled", False)):
            return False
        if not self._napcat_enabled():
            self._emit("ℹ️ NapCat 入站监听未启动：NapCat 通道未启用", is_debug=True)
            return False
        if not self._event_token:
            self._emit(
                "⚠️ NapCat 入站监听未启动：缺少 NAPCAT_EVENT_TOKEN",
                is_warning=True,
            )
            return False
        if len(self._event_token) < 16:
            self._emit(
                "⚠️ NapCat 入站 Token 长度较短，建议使用至少 16 位随机值",
                is_warning=True,
            )

        self._resize_queue(int(self._settings_snapshot.get("queue_size", 32)))
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"napcat-inbound-worker-{index}")
            for index in range(int(self._settings_snapshot.get("workers", 2)))
        ]
        try:
            await self._start_transport()
        except Exception:
            for task in self._workers:
                task.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
            self._stop_event = None
            raise

        transport = str(self._settings_snapshot.get("transport") or "http_post")
        self._emit(
            f"🔐 NapCat 入站监听已启动 | transport={transport} | "
            f"listen={self._settings_snapshot.get('listen_host')}:{self._settings_snapshot.get('listen_port')} | "
            f"groups={len(self._allowed_groups())} | "
            f"translate={str(_as_bool(self._settings_snapshot.get('translate', False))).lower()}",
        )
        return True

    async def stop(self) -> None:
        """停止传输端点并清理后台任务。"""

        server = self._server
        self._server = None
        if server is not None:
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(server.server_close)
        thread = self._server_thread
        self._server_thread = None
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 2.0)

        ws_server = self._ws_server
        self._ws_server = None
        if ws_server is not None:
            ws_server.close()
            wait_closed = getattr(ws_server, "wait_closed", None)
            if callable(wait_closed):
                await wait_closed()

        if self._stop_event is not None:
            self._stop_event.set()
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._stop_event = None
        self._loop = None

    async def reconcile(self) -> bool:
        """配置热重载后启停监听器；已运行时只刷新运行时安全参数。"""

        if self._event_token_from_env:
            current_token = os.getenv("NAPCAT_EVENT_TOKEN", "").strip()
            token_changed = current_token != self._event_token
            self._event_token = current_token
        else:
            token_changed = False
        desired = self._settings()
        enabled = bool(desired.get("enabled", False)) and self._napcat_enabled() and bool(self._event_token)
        if not enabled:
            if self.running:
                await self.stop()
            return False
        if not self.running:
            self._settings_snapshot = desired
            return await self.start()

        # 端口/传输方式变化需要重启端点；白名单、频率和队列参数可热更新。
        transport_changed = desired.get("transport") != self._settings_snapshot.get("transport")
        endpoint_changed = (
            desired.get("listen_host") != self._settings_snapshot.get("listen_host")
            or desired.get("listen_port") != self._settings_snapshot.get("listen_port")
            or desired.get("event_path") != self._settings_snapshot.get("event_path")
        )
        worker_config_changed = (
            desired.get("queue_size") != self._settings_snapshot.get("queue_size")
            or desired.get("workers") != self._settings_snapshot.get("workers")
        )
        self._settings_snapshot = desired
        if token_changed or transport_changed or endpoint_changed or worker_config_changed:
            await self.stop()
            return await self.start()
        return True


__all__ = [
    "DEFAULT_SETTINGS",
    "NapCatInboundJob",
    "NapCatInboundListener",
    "configured_group_ids",
    "event_message_text",
    "normalize_group_id",
]
