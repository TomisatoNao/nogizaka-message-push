# ============================================================
# health.py — 运行时健康状态追踪（纯内存，不持久化）
# ============================================================
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
import threading

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
        self._lock = threading.RLock()
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
        self._next_cycle: dict | None = None   # {"at_epoch": float, "tag": str}
        # 启动状态与运行时健康是两个维度：首次运行的“等待配置”不是故障，
        # 但需要在管理端明确展示，避免只能从日志猜测系统当前阶段。
        self._startup_state: str = "STARTING"
        self._startup_reasons: list[str] = []
        self._startup_updated_at: float = 0.0

        # 巡查生命周期与存活状态。时间戳使用 Unix 时间，年龄使用
        # monotonic，避免系统校时导致健康判定倒退或突然过期。
        self._cycle_id: str = ""
        self._cycle_in_progress: bool = False
        self._cycle_phase: str = "idle"
        self._cycle_started_at: float | None = None
        self._cycle_started_monotonic: float | None = None
        self._last_cycle_started_at: float | None = None
        self._last_cycle_completed_at: float | None = None
        self._last_cycle_elapsed_ms: float | None = None
        self._last_cycle_outcome: str = "never"
        self._cycle_phase_updated_at: float | None = None
        self._cycle_timeout_seconds: float = 900.0
        self._monitor_stale_seconds: float = 1800.0
        self._startup_grace_seconds: float = 300.0

    def initialize(self, summary_interval: int = 10, error_buffer: int = 50,
                   token_warn_seconds: int = 600,
                   cycle_timeout_seconds: float = 900.0,
                   monitor_stale_seconds: float | None = None,
                   startup_grace_seconds: float = 300.0) -> None:
        with self._lock:
            self._summary_interval = max(1, int(summary_interval))
            self._error_buffer = max(1, int(error_buffer))
            self._token_warn_seconds = max(0, int(token_warn_seconds))
            self._cycle_timeout_seconds = max(1.0, float(cycle_timeout_seconds))
            self._monitor_stale_seconds = max(
                self._cycle_timeout_seconds + 5.0,
                float(monitor_stale_seconds)
                if monitor_stale_seconds is not None
                else self._cycle_timeout_seconds + 300.0,
            )
            self._startup_grace_seconds = max(0.0, float(startup_grace_seconds))
            self._start_time = time.monotonic()

    def set_startup_state(self, state: str, reasons: list[str] | None = None) -> None:
        """更新启动/配置状态（``READY``、``SETUP_REQUIRED`` 或 ``DEGRADED``）。"""
        with self._lock:
            allowed = {"STARTING", "READY", "SETUP_REQUIRED", "DEGRADED"}
            normalized = str(state or "STARTING").upper()
            if normalized not in allowed:
                normalized = "DEGRADED"
            self._startup_state = normalized
            self._startup_reasons = [str(item) for item in (reasons or []) if str(item).strip()]
            self._startup_updated_at = time.time()

    def startup_snapshot(self) -> dict:
        """返回启动状态快照，供状态页和健康摘要复用。"""
        with self._lock:
            return {
                "state": self._startup_state,
                "reasons": list(self._startup_reasons),
                "updated_at": self._startup_updated_at or None,
            }

    # ── 巡查生命周期 ─────────────────────────────────

    def record_cycle_started(self, cycle_id: str = "", phase: str = "starting") -> None:
        """记录一轮巡查开始。

        该方法只更新内存状态，不执行任何网络或文件操作，因此可以在主循环
        创建任务前调用；即使后续任务异常，健康探针仍能判断它是否超时。
        """
        now = time.time()
        monotonic_now = time.monotonic()
        with self._lock:
            self._cycle_id = str(cycle_id or f"cycle-{self._cycle_count + 1}")
            self._cycle_in_progress = True
            self._cycle_phase = str(phase or "starting")
            self._cycle_started_at = now
            self._cycle_started_monotonic = monotonic_now
            self._last_cycle_started_at = now
            self._cycle_phase_updated_at = now
            self._last_cycle_outcome = "running"
            self._next_cycle = None

    def record_cycle_phase(self, phase: str) -> None:
        """更新当前巡查阶段，供卡住时的健康探针定位。"""
        with self._lock:
            if self._cycle_in_progress:
                self._cycle_phase = str(phase or "unknown")
                self._cycle_phase_updated_at = time.time()

    def record_cycle_finished(self, outcome: str = "success", *,
                              elapsed_ms: float | None = None,
                              phase: str | None = None) -> None:
        """记录巡查完成、异常或超时，并结束当前生命周期。"""
        now = time.time()
        with self._lock:
            self._last_cycle_completed_at = now
            if elapsed_ms is None and self._cycle_started_monotonic is not None:
                elapsed_ms = max(0.0, (time.monotonic() - self._cycle_started_monotonic) * 1000)
            self._last_cycle_elapsed_ms = round(float(elapsed_ms), 1) if elapsed_ms is not None else None
            self._last_cycle_outcome = str(outcome or "unknown")
            self._cycle_in_progress = False
            self._cycle_phase = str(phase or ("idle" if outcome == "success" else self._cycle_phase))
            self._cycle_started_at = None
            self._cycle_started_monotonic = None
            self._cycle_phase_updated_at = now

    def _monitor_snapshot_locked(self, now_epoch: float | None = None) -> dict:
        now = time.time() if now_epoch is None else float(now_epoch)
        uptime = (time.monotonic() - self._start_time) if self._start_time else 0.0
        cycle_age = (
            max(0.0, time.monotonic() - self._cycle_started_monotonic)
            if self._cycle_in_progress and self._cycle_started_monotonic is not None
            else None
        )
        last_completed_age = (
            max(0.0, now - self._last_cycle_completed_at)
            if self._last_cycle_completed_at is not None
            else None
        )
        startup_age = (
            max(0.0, now - self._startup_updated_at)
            if self._startup_updated_at
            else uptime
        )
        next_at = (self._next_cycle or {}).get("at_epoch") if self._next_cycle else None

        state = self._startup_state
        if state == "STARTING":
            healthy, reason = False, "startup_not_ready"
        elif state == "DEGRADED":
            healthy, reason = False, "startup_degraded"
        elif state == "SETUP_REQUIRED":
            # 首次运行尚未启用监控是预期状态，不把配置等待误报为卡死。
            healthy, reason = True, "setup_required"
        elif self._cycle_in_progress:
            healthy = cycle_age is not None and cycle_age <= self._monitor_stale_seconds
            reason = "cycle_in_progress" if healthy else "cycle_heartbeat_stale"
        elif next_at is not None and next_at > now:
            # 休眠时段或正常退避期间没有活跃轮次，但下一轮已经明确排程，
            # 不应因上一轮/首轮尚未完成而误报监控停止。
            healthy, reason = True, "next_cycle_scheduled"
        elif self._last_cycle_completed_at is None:
            # 宽限期从启动自检完成/状态切换时开始，而不是从进程创建时开始；
            # 避免首次握手耗时较长时，首轮尚未启动就被误判为无心跳。
            healthy = startup_age <= self._startup_grace_seconds
            reason = "startup_grace" if healthy else "no_cycle_heartbeat"
        else:
            healthy = (last_completed_age is not None and
                       last_completed_age <= self._monitor_stale_seconds)
            reason = "idle_after_cycle" if healthy else "cycle_heartbeat_stale"

        return {
            "cycle_id": self._cycle_id or None,
            "cycle_in_progress": self._cycle_in_progress,
            "phase": self._cycle_phase,
            "phase_updated_at": self._cycle_phase_updated_at,
            "last_cycle_started_at": self._last_cycle_started_at,
            "last_cycle_completed_at": self._last_cycle_completed_at,
            "cycle_age_seconds": round(cycle_age, 1) if cycle_age is not None else None,
            "last_cycle_age_seconds": round(last_completed_age, 1) if last_completed_age is not None else None,
            "last_cycle_elapsed_ms": self._last_cycle_elapsed_ms,
            "last_cycle_outcome": self._last_cycle_outcome,
            "monitor_healthy": healthy,
            "monitor_reason": reason,
            "stale_after_seconds": self._monitor_stale_seconds,
            "cycle_timeout_seconds": self._cycle_timeout_seconds,
            "startup_grace_seconds": self._startup_grace_seconds,
        }

    def monitor_snapshot(self) -> dict:
        """返回后台巡查存活快照，供 ``/api/health/live`` 使用。"""
        with self._lock:
            return self._monitor_snapshot_locked()

    # ── 记录方法 ──────────────────────────────────────

    def record_channel(self, channel: str, ok: bool, err: str | None = None) -> None:
        with self._lock:
            stats = self._channels.setdefault(channel, ChannelStats())
            stats.total += 1
            if ok:
                stats.success += 1
            elif err:
                stats.last_error = err

    def record_member_fetch(self, name: str, ok: bool,
                             tier: ErrorTier | None = None,
                             err: str | None = None) -> None:
        with self._lock:
            stats = self._members.setdefault(name, MemberStats(name=name))
            stats.fetch_ok = ok
            if err:
                stats.last_error = err
            if not ok and tier is not None:
                self.record_error(f"{name} 拉取失败: {err or '未知错误'}", tier)

    def record_member_push(self, name: str, ok: bool) -> None:
        with self._lock:
            stats = self._members.setdefault(name, MemberStats(name=name))
            stats.push_ok = ok
            if not ok:
                self.record_error(f"{name} 推送失败", ErrorTier.TRANSIENT)

    def record_token(self, acc_id: str, remaining: float) -> None:
        with self._lock:
            was_healthy = self._tokens[acc_id].is_healthy if acc_id in self._tokens else True
            self._tokens[acc_id] = TokenInfo(
                account_id=acc_id, remaining=remaining, is_healthy=remaining > 0
            )
            if remaining <= 0 and was_healthy:
                self.record_error(f"{acc_id} Token 刷新失败", ErrorTier.PERSISTENT)

    def record_alert_cooldown(self, acc_id: str, remaining: float) -> None:
        with self._lock:
            self._alert_cooldowns[acc_id] = remaining

    def record_error(self, msg: str, tier: ErrorTier) -> None:
        with self._lock:
            while len(self._errors) >= self._error_buffer:
                self._errors.popleft()
            self._errors.append((msg, tier, self._cycle_count))

    def record_next_cycle(self, at_epoch: float, tag: str) -> None:
        """记录下一轮巡查的预计时间（供网页状态页显示倒计时）。"""
        with self._lock:
            self._next_cycle = {"at_epoch": at_epoch, "tag": tag}

    # ── 结构化快照（网页状态页用）───────────────────────

    def snapshot(self) -> dict:
        """当前健康状态的 JSON 可序列化快照。"""
        with self._lock:
            monitor = self._monitor_snapshot_locked()
            return {
                "cycle_count": self._cycle_count,
                "uptime_seconds": (time.monotonic() - self._start_time) if self._start_time else 0,
                "next_cycle": dict(self._next_cycle) if self._next_cycle else None,
                "startup": self.startup_snapshot(),
                "monitor": monitor,
                # 顶层别名便于旧版状态页/脚本逐步迁移，不破坏原有字段。
                "monitor_healthy": monitor["monitor_healthy"],
                "monitor_reason": monitor["monitor_reason"],
                "cycle_in_progress": monitor["cycle_in_progress"],
                "last_cycle_started_at": monitor["last_cycle_started_at"],
                "last_cycle_completed_at": monitor["last_cycle_completed_at"],
                "cycle_age_seconds": monitor["cycle_age_seconds"],
                "token_warn_seconds": self._token_warn_seconds,
                "channels": {
                    name: {"success": s.success, "total": s.total,
                           "healthy": s.is_healthy, "last_error": s.last_error}
                    for name, s in self._channels.items()
                },
                "tokens": {
                    acc: {"remaining": info.remaining, "healthy": info.is_healthy}
                    for acc, info in self._tokens.items()
                },
                "members": [
                    {"name": m.name, "fetch_ok": m.fetch_ok, "push_ok": m.push_ok,
                     "last_error": m.last_error}
                    for m in self._members.values()
                ],
                "errors": [
                    {"msg": msg, "tier": tier.name, "cycles_ago": self._cycle_count - cyc}
                    for msg, tier, cyc in list(self._errors)[-20:]
                ],
            }

    # ── 摘要生成 ──────────────────────────────────────

    def cycle_complete(self) -> str | None:
        with self._lock:
            self._cycle_count += 1
            if self._cycle_count % self._summary_interval != 0:
                return None
            summary = self._build_summary()
            # 通道计数按摘要周期滚动清零：不清零的话跑几天后，
            # 几天前的一次失败仍在稀释成功率，摘要数字失去判断价值
            for stats in self._channels.values():
                stats.success = 0
                stats.total = 0
            return summary

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
               token_warn_seconds: int = 600,
               cycle_timeout_seconds: float = 900.0,
               monitor_stale_seconds: float | None = None,
               startup_grace_seconds: float = 300.0) -> None:
    global _tracker
    _tracker = HealthTracker()
    _tracker.initialize(
        summary_interval,
        error_buffer,
        token_warn_seconds,
        cycle_timeout_seconds,
        monitor_stale_seconds,
        startup_grace_seconds,
    )


def get_tracker() -> HealthTracker:
    """获取全局 HealthTracker 实例。未 initialize 时自动创建默认实例。"""
    global _tracker
    if _tracker is None:
        _tracker = HealthTracker()
    return _tracker
