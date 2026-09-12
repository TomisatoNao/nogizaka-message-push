# ============================================================
# napcat.py — NapCat/OneBot HTTP QQ 消息发送 & 消息链构造
# ============================================================
import asyncio
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from src.utils import utc_to_jst

import httpx

import config.config as cfg
from src.constants import ROLE_KEY, ROLE_TRANSLATION
from src.logger import format_httpx_error, log_all

# ---- 模块级状态（由 initialize() 在 main() 中注入） ----
_client: httpx.AsyncClient = None   # type: ignore


@dataclass(frozen=True)
class NapCatSendOutcome:
    """一次 OneBot 发送请求的安全结果。

    ``error_code`` 只保留稳定的分类，不把 NapCat 返回的原始内容带到上层，
    这样既方便熔断/观测，也避免日志意外泄露 token 或本地路径。
    """

    ok: bool
    error_code: str = ""


def _configured_send_interval() -> float:
    """读取 NapCat 账号级发送间隔。

    兼容既有 ``qq_send_interval`` 配置；新配置尚未提供时沿用原来的 1.5s。
    该间隔只约束 NapCat HTTP 请求开始时间，不改变消息抓取周期。
    """

    raw = getattr(cfg, "NAPCAT_SEND_INTERVAL_SECONDS", None)
    if raw is None:
        raw = getattr(cfg, "QQ_SEND_INTERVAL", 1.5)
    try:
        return min(60.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return 1.5


class NapCatSendGate:
    """单个 NapCat 账号的串行发送门闩与轻量熔断器。

    监控、博客、社媒和 WebUI 测试推送都可能同时调用 ``send_qq_message``。
    同一账号共享一个门闩，确保不会并发向 QQNT 发起富媒体请求。显式检测到
    QQ 会话离线/鉴权失败时短暂熔断，避免离线期间反复重试造成请求风暴；
    ``rich media transfer failed`` 则只标记为媒体失败，不误判为掉线。
    """

    _TRANSPORT_ERRORS = frozenset({
        "network_error",
        "timeout",
        "upstream_error",
        "qq_send_network_error",
    })
    _SESSION_ERRORS = frozenset({"qq_offline", "auth_failed", "rate_limited"})

    def __init__(
        self,
        *,
        cooldown_seconds: float = 60.0,
        failure_threshold: int = 3,
        logger=None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._last_request_at: float | None = None
        self._blocked_until: float | None = None
        self._blocked_reason: str = ""
        self._failure_streak = 0
        self._session_state = "unknown"
        self._last_error: str | None = None
        self._last_target: str | None = None
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        self._last_elapsed_ms: float | None = None
        self._cooldown_seconds = max(5.0, float(cooldown_seconds))
        self._failure_threshold = max(1, int(failure_threshold))
        self._last_skip_log_at: float | None = None
        self._log = logger or (lambda *_args, **_kwargs: None)

    async def __aenter__(self):
        await self._lock.acquire()
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self._lock.release()

    async def wait_turn(self) -> None:
        """等待账号级最小间隔，并记录本次请求的开始时间。"""

        now = time.monotonic()
        if self._last_request_at is not None:
            delay = self._last_request_at + _configured_send_interval() - now
            if delay > 0:
                await asyncio.sleep(delay)
        self._last_request_at = time.monotonic()

    def is_blocked(self) -> bool:
        if self._blocked_until is None:
            return False
        if time.monotonic() >= self._blocked_until:
            self._blocked_until = None
            self._blocked_reason = ""
            return False
        return True

    def retry_after(self) -> float:
        if not self.is_blocked() or self._blocked_until is None:
            return 0.0
        return max(0.0, self._blocked_until - time.monotonic())

    def should_log_skip(self) -> bool:
        """熔断期间最多每 30 秒提示一次，避免每条消息刷屏。"""

        now = time.monotonic()
        if self._last_skip_log_at is not None and now - self._last_skip_log_at < 30:
            return False
        self._last_skip_log_at = now
        return True

    def block(self, reason: str, *, cooldown_seconds: float | None = None) -> None:
        duration = self._cooldown_seconds if cooldown_seconds is None else max(5.0, float(cooldown_seconds))
        self._blocked_until = time.monotonic() + duration
        self._blocked_reason = str(reason or "unknown")

    def record(
        self,
        outcome: NapCatSendOutcome,
        *,
        target: object | None = None,
        elapsed_ms: float | None = None,
        attempted: bool = True,
    ) -> None:
        """根据一次完整发送结果更新熔断状态。"""

        if attempted:
            self._last_attempt_at = time.monotonic()
            self._last_target = str(target) if target is not None else None
            self._last_elapsed_ms = (
                round(max(0.0, float(elapsed_ms)), 1)
                if elapsed_ms is not None
                else None
            )

        if outcome.ok:
            self._last_error = None
            self._last_success_at = time.monotonic()
            self._failure_streak = 0
            # 探针明确报告掉线时，不能被一个并发中的“恰好成功”请求清除；
            # 必须等会话监控确认 online 后再解除熔断。
            if self._session_state not in {"offline", "auth_failed"}:
                self._blocked_until = None
                self._blocked_reason = ""
            return

        code = outcome.error_code or "delivery_failed"
        if code == "circuit_open":
            # 熔断期间的快速跳过不应再次递增失败次数或延长冷却窗口。
            if not self._last_error:
                self._last_error = code
            return
        self._last_error = code
        if code in self._SESSION_ERRORS:
            # 会话/鉴权问题不应继续重试；限流给一个更短的冷却窗口。
            self.block(code, cooldown_seconds=30.0 if code == "rate_limited" else None)
            self._failure_streak = 0
            if code in {"qq_offline", "auth_failed"}:
                self._session_state = code
        elif code in self._TRANSPORT_ERRORS:
            self._failure_streak += 1
            if self._failure_streak >= self._failure_threshold:
                self.block(code)
        else:
            # 媒体格式/尺寸等业务失败不影响后续文本或其它媒体发送。
            self._failure_streak = 0

    def set_session_state(self, state: str) -> None:
        """让会话监控结果驱动发送熔断。"""

        normalized = str(state or "unknown").lower()
        self._session_state = normalized
        if normalized == "online":
            # 会话探针在线并不等于 QQNT 发送链路恢复。只有探针之前明确
            # 判定为离线/鉴权失败时，online 才能解除对应的会话熔断；
            # 发送网络错误的熔断必须等待冷却或实际发送成功来解除。
            if self._blocked_reason in {"qq_offline", "auth_failed"}:
                self._failure_streak = 0
                self._blocked_until = None
                self._blocked_reason = ""
        elif normalized in {"offline", "auth_failed"}:
            self.block(normalized)

    def snapshot(self) -> dict[str, object]:
        return {
            "blocked": self.is_blocked(),
            "blocked_reason": self._blocked_reason or None,
            "retry_after_seconds": round(self.retry_after(), 1),
            "failure_streak": self._failure_streak,
            "session_state": self._session_state,
            "failure_threshold": self._failure_threshold,
            "last_error": self._last_error,
            "last_target": self._last_target,
            "last_attempt_at": self._last_attempt_at,
            "last_success_at": self._last_success_at,
            "last_elapsed_ms": self._last_elapsed_ms,
            "last_request_at": self._last_request_at,
            "send_interval_seconds": _configured_send_interval(),
        }


_send_gate = NapCatSendGate()


def _safe_excerpt(value: object, limit: int = 240) -> str:
    """将 NapCat 返回内容压缩为可安全写入日志的摘要。

    OneBot 的错误文本有时会包含本地文件路径或请求头片段；发送失败时只需
    分类即可，不能把完整业务响应直接写入日志。
    """

    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    # 失败响应偶尔会回显鉴权查询参数或宿主机路径；日志只保留诊断所需的
    # 文本，不让 token/绝对路径扩散到系统日志。
    text = re.sub(
        r"(?i)(access[_-]?token|authorization|token)\s*[:=]\s*([^\s&,'\"}]+)",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)(?:[A-Z]:\\|/)(?:app|home|tmp|var|opt|mnt|workspace|data)"
        r"(?:[\\/][^\s,'\"}]+)+",
        "<local-path>",
        text,
    )
    return text[:limit] + ("…" if len(text) > limit else "")


def _body_excerpt(body: object, fallback: str = "") -> str:
    if isinstance(body, dict):
        containers = [body]
        nested = body.get("data")
        if isinstance(nested, dict):
            containers.append(nested)
        values: list[str] = []
        for container in containers:
            for key in ("message", "wording", "msg", "error", "errMsg"):
                value = container.get(key)
                if value:
                    values.append(str(value))
        if values:
            return _safe_excerpt(" | ".join(values))
    return _safe_excerpt(fallback)


def _classify_error(status_code: int, body: object = None, text: str = "") -> str:
    """把 HTTP/OneBot 失败映射为稳定的内部错误分类。"""

    excerpt = _body_excerpt(body, text).lower()
    result_codes: set[str] = set()
    if isinstance(body, dict):
        for container in (body, body.get("data")):
            if isinstance(container, dict) and container.get("result") is not None:
                result_codes.add(str(container.get("result")).strip())
    if status_code in {401, 403} or "token verify failed" in excerpt:
        return "auth_failed"
    if status_code == 429:
        return "rate_limited"
    # QQ 群通常会把“每分钟只能发 N 条消息”作为 HTTP 200 的业务错误
    # 返回，而不是使用标准 HTTP 429。把这类文本归到限流分类，避免
    # 合并转发失败时被误记成普通 http_error，也让发送门闩进入短冷却。
    if any(
        marker in excerpt
        for marker in (
            "本群每分钟只能发",
            "每分钟只能发",
            "每分钟最多发送",
            "rate limit",
            "too many messages",
        )
    ):
        return "rate_limited"
    if any(
        marker in excerpt
        for marker in (
            "kickedoffline",
            "kicked_offline",
            "kickoffline",
            "账号当前登录已失效",
            "登录已失效",
            "账号已离线",
            "account is offline",
        )
    ):
        return "qq_offline"
    if (
        "1006514" in result_codes
        or "1006514" in excerpt
        or "网络连接异常" in excerpt
        or "network connection error" in excerpt
        or "network connection abnormal" in excerpt
    ):
        return "qq_send_network_error"
    if "rich media transfer failed" in excerpt or "media transfer failed" in excerpt:
        return "media_transfer_failed"
    if 500 <= status_code <= 599:
        return "upstream_error"
    if status_code:
        return "http_error"
    return "delivery_failed"


def initialize(client: httpx.AsyncClient) -> None:
    """注入共享的 AsyncClient 实例。"""
    global _client, _send_gate
    _client = client
    # initialize() 在主事件循环内调用；重建 HTTP 客户端时同步清理旧的
    # 熔断/锁状态，避免已关闭的事件循环或上一轮故障污染新实例。
    _send_gate = NapCatSendGate(logger=log_all)


def get_send_gate_snapshot() -> dict[str, object]:
    """返回账号级发送门闩状态，供健康页/测试使用。"""

    return _send_gate.snapshot()


def _record_send_health(
    outcome: NapCatSendOutcome,
    group_id: object,
    elapsed_ms: float | None,
    *,
    attempted: bool = True,
) -> None:
    """将实际发送结果写入独立的 NapCat 发送状态。

    发送模块不能依赖 WebUI 或具体调用方来记录状态，否则 WebUI 测试、
    定时监控、社交媒体和告警发送会产生不一致的健康结果。这里采用延迟
    导入，避免模块初始化阶段形成循环依赖；状态写入失败也不能影响投递。
    """

    try:
        from src import health

        health.get_tracker().record_napcat_send(
            outcome.ok,
            outcome.error_code or None,
            target=group_id,
            elapsed_ms=elapsed_ms,
            attempted=attempted,
        )
    except Exception:
        # 健康记录属于旁路观测，不能改变发送结果。
        pass


def record_send_timeout(group_id: object | None = None, elapsed_ms: float | None = None) -> None:
    """记录调用方施加的单路超时。

    ``notifier`` 对 NapCat 路由设置的是整个投递预算；预算触发时，正在
    等待的协程会被取消，未必能回到 ``send_qq_message`` 的正常结果分支。
    由调用方补记一次稳定的 ``timeout``，让熔断、健康页和后续告警仍能看到
    这次真实的失败，而不会把“超时取消”误当成尚未发生。
    """

    outcome = NapCatSendOutcome(False, "timeout")
    elapsed = max(0.0, float(elapsed_ms)) if elapsed_ms is not None else None
    _send_gate.record(outcome, target=group_id, elapsed_ms=elapsed)
    _record_send_health(outcome, group_id, elapsed)


def set_session_state(state: str) -> None:
    """由 NapCat 会话监控同步在线/离线状态。"""

    _send_gate.set_session_state(state)


# ──────────────────────────────────────────────
# 消息链构造
# ──────────────────────────────────────────────
def build_message_chain(
    m_name: str,
    updated: str,
    msg: dict,
    translated_text: str = "",
    model_name: str = "",
) -> list[dict]:
    """
    将一条 API 消息体转换为 QQ 消息链。
    updated 格式：'%Y-%m-%dT%H:%M:%SZ'（UTC）
    NapCat 支持文本与图片/视频/语音在同一个消息链里发送，
    因此这里始终把成员名和时间戳作为第一段，媒体段紧随其后。
    """
    jst_time = utc_to_jst(updated)
    original = msg.get("text", "")
    # 有正文时加换行分隔；纯媒体消息也保留成员名和时间戳。
    header = f"{m_name} {jst_time}\n{original}" if original else f"{m_name} {jst_time}"
    chain: list[dict] = [
        {"type": "text", "data": {"text": header}}
    ]

    file_url = msg.get("file")
    if file_url:
        media_type = cfg.MEDIA_TYPE_MAP.get(msg.get("type", ""))
        if media_type:
            media_data = {"file": file_url}
            # 部分上游会额外提供文件名/MIME；保留这些提示供 QQ 官方 Bot
            # 上传层生成正确的 file_name，旧版消息没有这些字段时行为不变。
            for key in ("filename", "file_name", "name", "mime_type", "content_type"):
                value = msg.get(key)
                if value:
                    media_data[key] = value
            chain.append({"type": media_type, "data": media_data})
        else:
            log_all(f"⚠️ 未知媒体类型 '{msg.get('type')}'，跳过媒体段")

    if translated_text:
        sep = f"\n\n─── 🌐 译文 ({model_name}) ───\n\n" if model_name else "\n\n─── 🌐 译文 ───\n\n"
        # 打上角色标记，供下游通道识别翻译段（发送前会被剥离）
        chain.append({
            "type": "text",
            "data": {"text": f"{sep}{translated_text}"},
            ROLE_KEY: ROLE_TRANSLATION,
            "_model_name": model_name,
        })

    return chain


def strip_internal_keys(message_chain: list[dict]) -> list[dict]:
    """剥离消息链各段中以 _ 开头的内部字段（如 _role），
    保证发给 OneBot 的 payload 只含协议字段。"""
    return [
        {k: v for k, v in item.items() if not k.startswith("_")}
        for item in message_chain
    ]


def _split_video_record_chain(message_chain: list[dict]) -> list[list[dict]]:
    """
    NapCat/OneBot 的 video、record 段在部分版本里和 text 混发时会吞掉文本段。
    为保证成员名和时间戳一定可见，视频/语音消息拆成文本批次和媒体批次；
    所有文本段会合并在一起，避免正文和翻译分成两条消息。
    图片保持原有的图文合并消息链。
    """
    if not any(item.get("type") in {"video", "record"} for item in message_chain):
        return [message_chain]

    text_batch: list[dict] = []
    media_batches: list[list[dict]] = []
    other_batches: list[list[dict]] = []
    for item in message_chain:
        msg_type = item.get("type")
        if msg_type == "text":
            text_batch.append(item)
            continue
        if msg_type in {"video", "record"}:
            media_batches.append([item])
            continue
        # 理论上 video/record 消息不会混入 image；这里保守地单独发送未知非文本段。
        other_batches.append([item])

    batches: list[list[dict]] = []
    if text_batch:
        batches.append(text_batch)
    batches.extend(media_batches)
    batches.extend(other_batches)
    return batches


# ──────────────────────────────────────────────
# 发送
# ──────────────────────────────────────────────
def _resolve_api_url_and_headers(raw_url: str) -> tuple[str, dict[str, str]]:
    raw_value = str(raw_url or "").strip()
    configured_url = str(getattr(cfg, "QQ_BOT_API", "") or "").strip()
    url = (raw_value or configured_url or str(getattr(cfg, "NAPCAT_API_BASE", "") or "")).strip()
    use_config_token = not raw_value or raw_value == configured_url
    if not url:
        url = "http://127.0.0.1:3000/send_group_msg"

    headers = {"Content-Type": "application/json", "User-Agent": cfg.QQ_USER_AGENT}
    try:
        from urllib.parse import urlparse, parse_qs, urlunparse
        parsed = urlparse(url)
        # 若未填具体的发送 endpoint，自动补齐 /send_group_msg
        if not parsed.path or parsed.path == "/":
            parsed = parsed._replace(path="/send_group_msg")
            url = urlunparse(parsed)
        # 若 URL 中携带 access_token 或 token，自动增加 Authorization Bearer 头增强兼容
        qs = parse_qs(parsed.query)
        token = qs.get("access_token", [None])[0] or qs.get("token", [None])[0]
        # 新版配置不再把密钥拼进 URL。仅当调用方使用当前配置的地址
        # （或未显式传入地址）时才读取独立 Token，避免测试/调用方传入
        # 一个临时地址却意外套用另一台 NapCat 的密钥。
        if not token and use_config_token:
            token = str(getattr(cfg, "NAPCAT_API_TOKEN", "") or "").strip() or None
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except Exception:
        pass
    return url, headers


def _resolve_api_action_url(raw_url: str, action: str) -> tuple[str, dict[str, str]]:
    """将已配置的 OneBot endpoint 切换到指定 action。

    新版管理端保存的是不含动作路径的服务基地址，发送侧按 action 自动
    追加 ``/send_group_msg`` 或 ``/send_group_forward_msg``；旧版完整地址
    也会复用同一主机、查询参数和鉴权头。对自定义前缀保留兼容：未知路径
    会在末尾追加 action。
    """

    url, headers = _resolve_api_url_and_headers(raw_url)
    action = str(action or "send_group_msg").strip().lstrip("/")
    if not action:
        action = "send_group_msg"
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        path = parsed.path or "/"
        normalized = path.rstrip("/")
        endpoint = normalized.rsplit("/", 1)[-1] if normalized else ""
        if endpoint in {"send_group_msg", "send_group_forward_msg"}:
            prefix = normalized.rsplit("/", 1)[0]
            path = f"{prefix}/{action}" if prefix else f"/{action}"
        elif not normalized:
            path = f"/{action}"
        else:
            path = f"{normalized}/{action}"
        return urlunparse(parsed._replace(path=path)), headers
    except Exception:
        # 保留原有 endpoint 的容错行为；仅在 URL 可解析时切换 action。
        return url, headers


def _strip_payload_internal(value: object) -> object:
    """递归移除 OneBot payload 中的内部字段。

    合并转发节点会嵌套消息链，不能只清理最外层，否则内部的 ``_role``
    等准备阶段字段可能被 NapCat 当成协议字段处理。
    """

    if isinstance(value, Mapping):
        return {
            key: _strip_payload_internal(candidate)
            for key, candidate in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [_strip_payload_internal(item) for item in value]
    return value


async def _post_payload_detailed(
    group_id: int,
    payload: Mapping[str, object],
    action: str,
    max_retries: int,
) -> NapCatSendOutcome:
    """向 OneBot action 发送一个 JSON payload 并返回稳定结果。"""

    if _client is None:
        log_all("⚠️ NapCat 客户端尚未初始化", is_error=True)
        return NapCatSendOutcome(False, "not_initialized")

    body: dict[str, object] = {"group_id": group_id}
    body.update(_strip_payload_internal(payload))  # type: ignore[arg-type]
    payload_str = json.dumps(body, ensure_ascii=False)
    action_label = "合并转发" if action == "send_group_forward_msg" else "普通消息"
    if cfg.DEBUG_LOG_QQ_PAYLOAD:
        log_all(
            f"📤 发送体: {len(payload_str)} 字节 | 方式={action_label} | "
            f"预览: {payload_str[:200]}",
            is_debug=True,
        )

    api_url, req_headers = _resolve_api_action_url(cfg.QQ_BOT_API, action)
    retries = max(1, int(max_retries))
    last_error = "delivery_failed"
    for attempt in range(retries):
        # 限速作用于每次真实 HTTP 请求，包括重试；调用方已经持有账号级锁。
        await _send_gate.wait_turn()
        try:
            resp = await _client.post(
                api_url,
                content=payload_str,
                headers=req_headers,
            )
            if resp.status_code == 200:
                try:
                    response_body = resp.json()
                except Exception:
                    log_all(
                        f"⚠️ Bot 返回非 JSON 响应（HTTP 200，按成功处理）: "
                        f"{_safe_excerpt(getattr(resp, 'text', ''))}",
                        is_error=True,
                    )
                    return NapCatSendOutcome(True)
                if isinstance(response_body, dict) and (
                    response_body.get("status") == "ok"
                    or response_body.get("retcode") == 0
                ):
                    return NapCatSendOutcome(True)
                last_error = _classify_error(
                    resp.status_code,
                    response_body,
                    getattr(resp, "text", ""),
                )
                log_all(
                    f"⚠️ Bot 返回业务失败 | 方式={action_label} | "
                    f"error_code={last_error} | "
                    f"{_body_excerpt(response_body, getattr(resp, 'text', ''))}",
                    is_error=True,
                )
                return NapCatSendOutcome(False, last_error)

            last_error = _classify_error(
                resp.status_code,
                None,
                getattr(resp, "text", ""),
            )
            log_all(
                f"📡 异常响应 ({attempt + 1}/{retries}): HTTP {resp.status_code} | "
                f"方式={action_label} | error_code={last_error} | "
                f"{_safe_excerpt(getattr(resp, 'text', ''))}",
                is_error=True,
            )
            if resp.status_code == 502:
                log_all("🔥 502，代理/协议问题", is_error=True)
            # 4xx（除 429）不可重试，直接放弃
            if resp.status_code < 500 and resp.status_code != 429:
                return NapCatSendOutcome(False, last_error)

        except httpx.TimeoutException as e:
            last_error = "timeout"
            log_all(
                f"🔥 发送异常 ({attempt + 1}/{retries}) | 方式={action_label} | "
                f"error_code=timeout | {_safe_excerpt(format_httpx_error(e))}",
                is_error=True,
            )
        except (httpx.RequestError, OSError) as e:
            last_error = "network_error"
            log_all(
                f"🔥 发送异常 ({attempt + 1}/{retries}) | 方式={action_label} | "
                f"error_code=network_error | {_safe_excerpt(format_httpx_error(e))}",
                is_error=True,
            )
        except Exception as e:
            last_error = "unexpected_error"
            log_all(
                f"🔥 发送异常 ({attempt + 1}/{retries}) | 方式={action_label} | "
                f"error_code=unexpected_error | {_safe_excerpt(format_httpx_error(e))}",
                is_error=True,
            )

        if attempt < retries - 1:
            await asyncio.sleep(2 ** attempt)

    return NapCatSendOutcome(False, last_error)


async def _post_message_detailed(
    group_id: int,
    message_chain: list[dict],
    max_retries: int,
) -> NapCatSendOutcome:
    """发送一批消息并返回稳定的内部结果。

    调用方负责持有 ``_send_gate``；这样同一账号的文本、图片、视频批次会
    保持顺序，且监控/手动推送不会并发打到同一个 OneBot 端点。
    """

    return await _post_payload_detailed(
        group_id,
        {"message": strip_internal_keys(message_chain)},
        "send_group_msg",
        max_retries,
    )


async def _send_payload(
    group_id: int,
    payload: Mapping[str, object],
    action: str,
    max_retries: int = 3,
) -> bool:
    """在账号级发送门闩内执行任意 OneBot action。"""

    started = time.monotonic()
    async with _send_gate:
        if _send_gate.is_blocked():
            if _send_gate.should_log_skip():
                snapshot = _send_gate.snapshot()
                log_all(
                    "⏸️ NapCat 发送已熔断，跳过请求 | "
                    f"reason={snapshot.get('blocked_reason')} | "
                    f"retry_after={_send_gate.retry_after():.1f}s",
                    is_warning=True,
                )
            outcome = NapCatSendOutcome(False, "circuit_open")
            _send_gate.record(
                outcome,
                target=group_id,
                elapsed_ms=(time.monotonic() - started) * 1000,
                attempted=False,
            )
            _record_send_health(
                outcome,
                group_id,
                (time.monotonic() - started) * 1000,
                attempted=False,
            )
            return False
        outcome = await _post_payload_detailed(
            group_id,
            payload,
            action,
            max_retries,
        )
        elapsed_ms = (time.monotonic() - started) * 1000
        _send_gate.record(outcome, target=group_id, elapsed_ms=elapsed_ms)
        _record_send_health(outcome, group_id, elapsed_ms)
        if not outcome.ok:
            log_all(
                f"❌ QQ {('合并转发' if action == 'send_group_forward_msg' else '消息')}"
                f"发送彻底失败 | error_code={outcome.error_code}",
                is_error=True,
            )
        return outcome.ok


async def _post_message(group_id: int, message_chain: list[dict], max_retries: int) -> bool:
    """兼容旧调用方的布尔发送接口。"""

    return await _send_payload(
        group_id,
        {"message": strip_internal_keys(message_chain)},
        "send_group_msg",
        max_retries,
    )


async def send_qq_message(
    group_id: int,
    message_chain: list[dict],
    max_retries: int = 3,
) -> bool:
    batches = _split_video_record_chain(message_chain)
    started = time.monotonic()
    async with _send_gate:
        if _send_gate.is_blocked():
            if _send_gate.should_log_skip():
                snapshot = _send_gate.snapshot()
                log_all(
                    "⏸️ NapCat 发送已熔断，跳过请求 | "
                    f"reason={snapshot.get('blocked_reason')} | "
                    f"retry_after={snapshot.get('retry_after_seconds')}s",
                    is_warning=True,
                )
            outcome = NapCatSendOutcome(False, "circuit_open")
            elapsed_ms = (time.monotonic() - started) * 1000
            _send_gate.record(
                outcome,
                target=group_id,
                elapsed_ms=elapsed_ms,
                attempted=False,
            )
            _record_send_health(outcome, group_id, elapsed_ms, attempted=False)
            return False

        for batch in batches:
            outcome = await _post_message_detailed(group_id, batch, max_retries)
            _send_gate.record(
                outcome,
                target=group_id,
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
            if not outcome.ok:
                elapsed_ms = (time.monotonic() - started) * 1000
                _record_send_health(outcome, group_id, elapsed_ms)
                log_all(
                    f"❌ QQ 消息发送彻底失败 | error_code={outcome.error_code}",
                    is_error=True,
                )
                return False

    elapsed_ms = (time.monotonic() - started) * 1000
    _record_send_health(NapCatSendOutcome(True), group_id, elapsed_ms)
    return True


async def send_group_forward_message(
    group_id: int,
    messages: list[dict],
    max_retries: int = 3,
) -> bool:
    """以 OneBot 合并转发卡片发送多个消息节点。

    ``send_group_forward_msg`` 会把多个节点折叠成一条群消息，适合
    Instagram/TikTok 轮播等多媒体动态，避免普通消息链被 QQ 的每分钟
    消息条数限制拆成多条。节点内容由 ``NapCatAdapter`` 负责构造。
    """

    if not isinstance(messages, list) or not messages:
        return False
    log_all(
        f"📦 NapCat 合并转发 | 群{group_id} | 节点{len(messages)} | "
        "按 1 条群消息投递",
        is_debug=True,
    )
    return await _send_payload(
        group_id,
        {"messages": messages},
        "send_group_forward_msg",
        max_retries,
    )


__all__ = [
    "NapCatSendGate",
    "NapCatSendOutcome",
    "build_message_chain",
    "get_send_gate_snapshot",
    "initialize",
    "record_send_timeout",
    "send_group_forward_message",
    "send_qq_message",
    "set_session_state",
    "strip_internal_keys",
]
