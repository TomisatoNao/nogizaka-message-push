# ============================================================
# health.py — 运行时健康状态追踪（纯内存，不持久化）
# ============================================================
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto

from src.logger import log_all


# ── 错误分级 ──────────────────────────────────────────
class ErrorTier(Enum):
    TRANSIENT  = auto()   # 网络超时、429/5xx、临时连接失败
    PERSISTENT = auto()   # 401 刷新后仍失败、凭证缺失、配置错误


# ── 统计数据结构 ──────────────────────────────────────
@dataclass
class ChannelStats:
    success: int = 0
    total: int = 0
    last_error: str | None = None

    @property
    def rate(self) -> float:
        return self.success / self.total if self.total > 0 else 1.0

    @property
    def is_healthy(self) -> bool:
        return self.rate == 1.0


@dataclass
class TokenInfo:
    account_id: str
    remaining: float   # Token 剩余秒数，0 = 失效
    is_healthy: bool   # remaining > 0


@dataclass
class MemberStats:
    name: str
    fetch_ok: bool = True
    push_ok: bool = True
    last_error: str | None = None


# ── HealthTracker ─────────────────────────────────────
class HealthTracker:
    """全局健康状态追踪器。

    所有 record_* 方法均为非阻塞的纯内存操作，可在任何协程中安全调用
    （dict.setdefault / deque.append 在 CPython 中是 GIL 安全的原子操作）。

    cycle_complete() 每 N 轮被 app.py 的主循环调用一次，返回格式化的状态摘要字符串。
    """

    def __init__(self):
        self._channels: dict[str, ChannelStats] = {}
        self._tokens: dict[str, TokenInfo] = {}
        self._members: dict[str, MemberStats] = {}
        self._errors: deque[tuple[str, ErrorTier, int]] = deque()  # (msg, tier, cycle)
        self._alert_cooldowns: dict[str, float] = {}

        self._cycle_count: int = 0
        self._start_time: float = 0.0
        self._summary_interval: int = 10
        self._error_buffer: int = 50
        self._token_warn_seconds: int = 600

    def initialize(self, summary_interval: int = 10, error_buffer: int = 50,
                   token_warn_seconds: int = 600) -> None:
        self._summary_interval = summary_interval
        self._error_buffer = error_buffer
        self._token_warn_seconds = token_warn_seconds
        self._start_time = time.monotonic()

    # ── 记录方法 ──────────────────────────────────────

    def record_channel(self, channel: str, ok: bool, err: str | None = None) -> None:
        stats = self._channels.setdefault(channel, ChannelStats())
        stats.total += 1
        if ok:
            stats.success += 1
        elif err:
            stats.last_error = err

    def record_member_fetch(self, name: str, ok: bool,
                             tier: ErrorTier | None = None,
                             err: str | None = None) -> None:
        stats = self._members.setdefault(name, MemberStats(name=name))
        stats.fetch_ok = ok
        if err:
            stats.last_error = err
        if not ok and tier is not None:
            self.record_error(f"{name} 拉取失败: {err or '未知错误'}", tier)

    def record_member_push(self, name: str, ok: bool) -> None:
        stats = self._members.setdefault(name, MemberStats(name=name))
        stats.push_ok = ok
        if not ok:
            self.record_error(f"{name} 推送失败", ErrorTier.TRANSIENT)

    def record_token(self, acc_id: str, remaining: float) -> None:
        self._tokens[acc_id] = TokenInfo(
            account_id=acc_id, remaining=remaining, is_healthy=remaining > 0
        )
        if remaining <= 0:
            self.record_error(f"{acc_id} Token 刷新失败", ErrorTier.PERSISTENT)

    def record_alert_cooldown(self, acc_id: str, remaining: float) -> None:
        self._alert_cooldowns[acc_id] = remaining

    def record_error(self, msg: str, tier: ErrorTier) -> None:
        while len(self._errors) >= self._error_buffer:
            self._errors.popleft()
        self._errors.append((msg, tier, self._cycle_count))

    # ── 摘要生成 ──────────────────────────────────────

    def cycle_complete(self) -> str | None:
        self._cycle_count += 1
        if self._cycle_count % self._summary_interval != 0:
            return None
        return self._build_summary()

    def _build_summary(self) -> str:
        elapsed = time.monotonic() - self._start_time
        elapsed_str = self._format_duration(elapsed)
        lines = [f"📊 [状态摘要 #{self._cycle_count} · 运行 {elapsed_str}]"]

        # 1. 通道状态
        if self._channels:
            parts = []
            for name, stats in self._channels.items():
                icon = "✅" if stats.is_healthy else "⚠️"
                parts.append(f"{name} {icon} {stats.success}/{stats.total}")
            lines.append(f"  通道: {' | '.join(parts)}")

        # 2. Token 状态
        if self._tokens:
            parts = []
            for acc_id, info in self._tokens.items():
                if info.remaining <= 0:
                    parts.append(f"{acc_id} 失效 🔴")
                elif info.remaining < self._token_warn_seconds:
                    parts.append(f"{acc_id} {self._format_remaining(info.remaining)} ⚠️")
                else:
                    parts.append(f"{acc_id} {self._format_remaining(info.remaining)}")
            lines.append(f"  Token: {' · '.join(parts)}")

        # 3. 成员状态
        if self._members:
            fetch_total = len(self._members)
            fetch_ok = sum(1 for m in self._members.values() if m.fetch_ok)
            push_ok = sum(1 for m in self._members.values() if m.push_ok)
            fetch_icon = "✅" if fetch_ok == fetch_total else "⚠️"
            push_icon = "✅" if push_ok == fetch_total else "⚠️"
            lines.append(f"  成员: {fetch_ok}/{fetch_total} 拉取正常 {fetch_icon} · "
                         f"{push_ok}/{fetch_total} 推送正常 {push_icon}")

        # 4. 近期错误（仅 PERSISTENT 存在时展开，或最近 5 条皆有）
        recent_window = self._summary_interval
        persistent_errors = [
            (msg, tier, cyc) for msg, tier, cyc in self._errors
            if tier == ErrorTier.PERSISTENT
            and self._cycle_count - cyc <= recent_window
        ]
        transient_errors = [
            (msg, tier, cyc) for msg, tier, cyc in self._errors
            if tier == ErrorTier.TRANSIENT
            and self._cycle_count - cyc <= recent_window
        ]

        if persistent_errors or transient_errors:
            lines.append("  ⚠️ 近期错误:")
            for msg, tier, cyc in persistent_errors[-5:] + transient_errors[-5:]:
                tag = "PERSIST" if tier == ErrorTier.PERSISTENT else "TRANSIENT"
                ago = self._cycle_count - cyc
                ago_str = f"{ago}轮前" if ago > 0 else "本轮"
                lines.append(f"    └─ [{tag}] {msg}（{ago_str}）")

        return "\n".join(lines)

    # ── 格式化工具 ────────────────────────────────────

    @staticmethod
    def _format_remaining(seconds: float) -> str:
        if seconds <= 0:
            return "0min"
        if seconds >= 3600:
            return f"{seconds / 3600:.1f}h"
        minutes = seconds / 60
        if minutes >= 1:
            return f"{int(minutes)}min"
        return f"{int(seconds)}s"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


# ── 模块级单例 ──────────────────────────────────────
_tracker: HealthTracker | None = None


def initialize(summary_interval: int = 10, error_buffer: int = 50,
               token_warn_seconds: int = 600) -> None:
    global _tracker
    _tracker = HealthTracker()
    _tracker.initialize(summary_interval, error_buffer, token_warn_seconds)


def get_tracker() -> HealthTracker:
    """获取全局 HealthTracker 实例。未 initialize 时自动创建默认实例。"""
    global _tracker
    if _tracker is None:
        _tracker = HealthTracker()
    return _tracker
