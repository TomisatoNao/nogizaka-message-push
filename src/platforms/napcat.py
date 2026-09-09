# ============================================================
# napcat.py — NapCat/OneBot HTTP QQ 消息发送 & 消息链构造
# ============================================================
import asyncio
import json
import re
import time
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

    _TRANSPORT_ERRORS = frozenset({"network_error", "timeout", "upstream_error"})
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

    def record(self, outcome: NapCatSendOutcome) -> None:
        """根据一次完整发送结果更新熔断状态。"""

        if outcome.ok:
            self._failure_streak = 0
            # 探针明确报告掉线时，不能被一个并发中的“恰好成功”请求清除；
            # 必须等会话监控确认 online 后再解除熔断。
            if self._session_state not in {"offline", "auth_failed"}:
                self._blocked_until = None
                self._blocked_reason = ""
            return

        code = outcome.error_code or "delivery_failed"
        if code in self._SESSION_ERRORS:
            # 会话/鉴权问题不应继续重试；限流给一个更短的冷却窗口。
            self.block(code, cooldown_seconds=30.0 if code == "rate_limited" else None)
            self._failure_streak = 0
            if code in {"qq_offline", "auth_failed"}:
                self._session_state = code
        elif code in self._TRANSPORT_ERRORS:
            self._failure_streak += 1
            if self._failure_streak >= self._failure_threshold:
                self.block("transport_error")
        else:
            # 媒体格式/尺寸等业务失败不影响后续文本或其它媒体发送。
            self._failure_streak = 0

    def set_session_state(self, state: str) -> None:
        """让会话监控结果驱动发送熔断。"""

        normalized = str(state or "unknown").lower()
        self._session_state = normalized
        if normalized == "online":
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
        for key in ("message", "wording", "msg", "error", "errMsg"):
            value = body.get(key)
            if value:
                return _safe_excerpt(value)
    return _safe_excerpt(fallback)


def _classify_error(status_code: int, body: object = None, text: str = "") -> str:
    """把 HTTP/OneBot 失败映射为稳定的内部错误分类。"""

    excerpt = _body_excerpt(body, text).lower()
    if status_code in {401, 403} or "token verify failed" in excerpt:
        return "auth_failed"
    if status_code == 429:
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
    url = (raw_url or getattr(cfg, "QQ_BOT_API", "") or "").strip()
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
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except Exception:
        pass
    return url, headers


async def _post_message_detailed(
    group_id: int,
    message_chain: list[dict],
    max_retries: int,
) -> NapCatSendOutcome:
    """发送一批消息并返回稳定的内部结果。

    调用方负责持有 ``_send_gate``；这样同一账号的文本、图片、视频批次会
    保持顺序，且监控/手动推送不会并发打到同一个 OneBot 端点。
    """

    if _client is None:
        log_all("⚠️ NapCat 客户端尚未初始化", is_error=True)
        return NapCatSendOutcome(False, "not_initialized")

    payload_str = json.dumps(
        {"group_id": group_id, "message": strip_internal_keys(message_chain)},
        ensure_ascii=False,
    )
    if cfg.DEBUG_LOG_QQ_PAYLOAD:
        log_all(
            f"📤 发送体: {len(payload_str)} 字节 | 预览: {payload_str[:200]}",
            is_debug=True,
        )

    api_url, req_headers = _resolve_api_url_and_headers(cfg.QQ_BOT_API)
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
                    body = resp.json()
                except Exception:
                    log_all(
                        f"⚠️ Bot 返回非 JSON 响应（HTTP 200，按成功处理）: "
                        f"{_safe_excerpt(getattr(resp, 'text', ''))}",
                        is_error=True,
                    )
                    return NapCatSendOutcome(True)
                if isinstance(body, dict) and (
                    body.get("status") == "ok" or body.get("retcode") == 0
                ):
                    return NapCatSendOutcome(True)
                last_error = _classify_error(
                    resp.status_code,
                    body,
                    getattr(resp, "text", ""),
                )
                log_all(
                    f"⚠️ Bot 返回业务失败 | error_code={last_error} | "
                    f"{_body_excerpt(body, getattr(resp, 'text', ''))}",
                    is_error=True,
                )
                return NapCatSendOutcome(False, last_error)
            else:
                last_error = _classify_error(
                    resp.status_code,
                    None,
                    getattr(resp, "text", ""),
                )
                log_all(
                    f"📡 异常响应 ({attempt + 1}/{retries}): "
                    f"HTTP {resp.status_code} | error_code={last_error} | "
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
                f"🔥 发送异常 ({attempt + 1}/{retries}) | error_code=timeout | "
                f"{_safe_excerpt(format_httpx_error(e))}",
                is_error=True,
            )
        except (httpx.RequestError, OSError) as e:
            last_error = "network_error"
            log_all(
                f"🔥 发送异常 ({attempt + 1}/{retries}) | error_code=network_error | "
                f"{_safe_excerpt(format_httpx_error(e))}",
                is_error=True,
            )
        except Exception as e:
            last_error = "unexpected_error"
            log_all(
                f"🔥 发送异常 ({attempt + 1}/{retries}) | error_code=unexpected_error | "
                f"{_safe_excerpt(format_httpx_error(e))}",
                is_error=True,
            )

        if attempt < retries - 1:
            await asyncio.sleep(2 ** attempt)

    return NapCatSendOutcome(False, last_error)


async def _post_message(group_id: int, message_chain: list[dict], max_retries: int) -> bool:
    """兼容旧调用方的布尔发送接口。"""

    async with _send_gate:
        if _send_gate.is_blocked():
            if _send_gate.should_log_skip():
                log_all(
                    "⏸️ NapCat 发送已熔断，跳过请求 | "
                    f"reason={_send_gate.snapshot().get('blocked_reason')} | "
                    f"retry_after={_send_gate.retry_after():.1f}s",
                    is_warning=True,
                )
            return False
        outcome = await _post_message_detailed(group_id, message_chain, max_retries)
        _send_gate.record(outcome)
        if not outcome.ok:
            log_all(
                f"❌ QQ 消息发送彻底失败 | error_code={outcome.error_code}",
                is_error=True,
            )
        return outcome.ok


async def send_qq_message(
    group_id: int,
    message_chain: list[dict],
    max_retries: int = 3,
) -> bool:
    batches = _split_video_record_chain(message_chain)
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
            return False

        for batch in batches:
            outcome = await _post_message_detailed(group_id, batch, max_retries)
            _send_gate.record(outcome)
            if not outcome.ok:
                log_all(
                    f"❌ QQ 消息发送彻底失败 | error_code={outcome.error_code}",
                    is_error=True,
                )
                return False

    return True
