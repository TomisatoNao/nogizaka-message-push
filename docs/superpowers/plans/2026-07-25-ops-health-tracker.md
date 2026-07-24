# 运维体验优化：HealthTracker 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 HealthTracker 模块，每 N 轮自动输出系统状态摘要到终端，修复 send_alert_message 告警覆盖（NapCat/TG/多Bot），修复热重载遗漏 TG 环境变量，修正健康检查日志。

**Architecture:** 新增 `src/health.py` 作为纯内存状态追踪模块（不持久化），通过依赖注入模式与现有 fetcher/notifier/credentials 模块集成。使用 `collections.deque` 实现环形错误 buffer，`dataclass` 定义统计数据。

**Tech Stack:** Python 3.10+ stdlib（`dataclasses`, `collections.deque`, `enum`, `time`），无外部依赖。

## Global Constraints

- 不引入任何新第三方依赖
- 不改变现有推送语义（NapCat 失败仍阻断时间戳，官方 Bot/TG 仍旁路）
- 状态仅保留在内存中，重启后重新计数
- 遵循现有代码风格：中文注释、`log_all()` 日志、`initialize()` 注入模式
- 所有配置项需在 `config.json` + `config.schema.json` + `config.py:_KEY_TO_VAR` 三处同步

---

## File Structure

| 文件 | 职责 |
|---|---|
| `src/health.py` | **新建** — HealthTracker 核心类 + 数据类 |
| `src/app.py` | **改造** — 初始化 health、修正健康检查日志、循环中输出摘要 |
| `src/notifier.py` | **改造** — 记录每条通道推送结果、修复 send_alert_message |
| `src/fetcher.py` | **改造** — 记录成员拉取/推送结果 |
| `config/credentials.py` | **改造** — 记录 Token 刷新后状态和告警冷却 |
| `config/config.py` | **改造** — 新增 3 个配置项 + 修复 reload() 遗漏 TG 环境变量 |
| `config/config.schema.json` | **改造** — 新增 3 个 schema 属性 |
| `config/config.json` | **改造** — 添加 3 个新键的默认值 |

---

### Task 1: 新建 `src/health.py` — HealthTracker 核心模块

**Files:**
- Create: `src/health.py`

**Interfaces:**
- Produces:
  - `ErrorTier` enum: `TRANSIENT`, `PERSISTENT`
  - `ChannelStats` dataclass: `success: int`, `total: int`, `last_error: str | None`
  - `TokenInfo` dataclass: `account_id: str`, `remaining: float`, `is_healthy: bool`
  - `MemberStats` dataclass: `name: str`, `fetch_ok: bool=True`, `push_ok: bool=True`, `last_error: str | None=None`
  - `HealthTracker` class with:
    - `initialize(summary_interval, error_buffer, token_warn_seconds) -> None`
    - `record_channel(channel, ok, err=None) -> None`
    - `record_member_fetch(name, ok, tier=None, err=None) -> None`
    - `record_member_push(name, ok) -> None`
    - `record_token(acc_id, remaining) -> None`
    - `record_alert_cooldown(acc_id, remaining) -> None`
    - `record_error(msg, tier) -> None`
    - `cycle_complete() -> str | None`

- [ ] **Step 1: 创建 `src/health.py` 完整实现**

```python
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
```

- [ ] **Step 2: 验证模块语法**

```
python -c "from src.health import HealthTracker, ErrorTier, ChannelStats, TokenInfo, MemberStats, get_tracker, initialize; t = get_tracker(); t.record_channel('test', True); print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/health.py
git commit -m "feat: add HealthTracker module for runtime health monitoring"
```

---

### Task 2: 新增配置项（config.json + schema + config.py）

**Files:**
- Modify: `config/config.json` — 末尾添加 3 个键
- Modify: `config/config.schema.json` — 添加 3 个属性定义
- Modify: `config/config.py` — `_KEY_TO_VAR` 添加映射 + `reload()` 补上 TG 覆盖

**Interfaces:**
- Produces:
  - `config.config.HEALTH_SUMMARY_INTERVAL: int` (default 10)
  - `config.config.HEALTH_ERROR_BUFFER: int` (default 50)
  - `config.config.HEALTH_TOKEN_WARN_SECONDS: int` (default 600)
- Consumes: nothing new

- [ ] **Step 1: 编辑 `config/config.json` — 在文件末尾 `"bilibili_min_interval"` 的 `}` 前添加**

在 `"bilibili_min_interval": 3.0` 之后，`}` 之前，添加逗号和新键：

```json5
  "bilibili_min_interval": 3.0,

  // ============================================================
  // 健康状态追踪
  // health_summary_interval:    每隔多少轮输出一次状态摘要
  // health_error_buffer:        环形 buffer 保留最近错误数
  // health_token_warn_seconds:  Token 剩余时间低于此值时显示警告
  // ============================================================
  "health_summary_interval": 10,
  "health_error_buffer": 50,
  "health_token_warn_seconds": 600
}
```

- [ ] **Step 2: 编辑 `config/config.schema.json` — 添加 3 个属性**

在 `"bilibili_min_interval"` 之后，`}` 之前添加：

```json
    "health_summary_interval": {
      "type": "integer",
      "minimum": 1,
      "description": "每隔多少轮输出一次状态摘要"
    },
    "health_error_buffer": {
      "type": "integer",
      "minimum": 1,
      "description": "环形 buffer 保留最近错误数"
    },
    "health_token_warn_seconds": {
      "type": "integer",
      "minimum": 0,
      "description": "Token 剩余时间低于此值时显示警告（秒）"
    }
```

位置：在 `"bilibili_min_interval"` block 的 `}` 之后，整个 `"properties"` 对象末尾的 `}` 之前。

- [ ] **Step 3: 编辑 `config/config.py` — 添加 `_KEY_TO_VAR` 映射**

在 `_KEY_TO_VAR` 字典中添加（在 `"bilibili_min_interval"` 之后）：

```python
    "health_summary_interval":      "HEALTH_SUMMARY_INTERVAL",
    "health_error_buffer":          "HEALTH_ERROR_BUFFER",
    "health_token_warn_seconds":    "HEALTH_TOKEN_WARN_SECONDS",
```

- [ ] **Step 4: 编辑 `config/config.py` — 在 `reload()` 末尾补上 TG 环境变量覆盖**

修改 `reload()` 函数最后部分：

```python
        # 重新应用环境变量覆盖
        global ENABLE_NAPCAT_QQ, ENABLE_QQ_OFFICIAL_BOT, DEBUG_LOG_QQ_PAYLOAD
        ENABLE_NAPCAT_QQ       = _env_bool("ENABLE_NAPCAT_QQ",       ENABLE_NAPCAT_QQ)
        ENABLE_QQ_OFFICIAL_BOT = _env_bool("ENABLE_QQ_OFFICIAL_BOT", ENABLE_QQ_OFFICIAL_BOT)
        DEBUG_LOG_QQ_PAYLOAD   = _env_bool("DEBUG_LOG_QQ_PAYLOAD",   DEBUG_LOG_QQ_PAYLOAD)

        # 补回 TG Bot 热重载环境变量覆盖
        global ENABLE_TG_BOT, TG_BOT_TOKEN
        ENABLE_TG_BOT  = _env_bool("ENABLE_TG_BOT", ENABLE_TG_BOT)
        TG_BOT_TOKEN   = _env("TG_BOT_TOKEN", TG_BOT_TOKEN)
```

需要把 `global` 声明扩展为包含 `ENABLE_TG_BOT` 和 `TG_BOT_TOKEN`。只需修改现有的 `global` 行（第 275 行），把两个新变量加入即可：

```python
        global ENABLE_NAPCAT_QQ, ENABLE_QQ_OFFICIAL_BOT, DEBUG_LOG_QQ_PAYLOAD, \
               ENABLE_TG_BOT, TG_BOT_TOKEN
```

然后在 `DEBUG_LOG_QQ_PAYLOAD` 赋值之后添加 TG Bot 的两行覆盖。

- [ ] **Step 5: 验证配置加载**

```
python -c "import config.config as cfg; print(cfg.HEALTH_SUMMARY_INTERVAL, cfg.HEALTH_ERROR_BUFFER, cfg.HEALTH_TOKEN_WARN_SECONDS)"
```
Expected: `10 50 600`

- [ ] **Step 6: Commit**

```bash
git add config/config.json config/config.schema.json config/config.py
git commit -m "feat: add health tracker config items; fix reload() missing TG Bot env override"
```

---

### Task 3: 改造 `src/app.py` — 初始化 health + 修正健康检查日志 + 摘要输出

**Files:**
- Modify: `src/app.py`

**Interfaces:**
- Consumes: `src.health.get_tracker()`, `src.health.initialize()`, `HealthTracker.cycle_complete()`, `HealthTracker.record_error()`, `HealthTracker.record_channel()`
- Produces: 启动时将健康检查结果记录到 HealthTracker

- [ ] **Step 1: 在 `src/app.py` 顶部添加 import**

在第 17 行 `from src.platforms import tgbot` 之后添加：

```python
from src import health
```

- [ ] **Step 2: 在 `_health_check()` 中修正误导日志并记录到 HealthTracker**

修改 `_health_check()` 函数，将第 43-44 行的 QQ-only 检查替换为三通道检查：

原代码（第 43-44 行）：
```python
    if not cfg.ENABLE_NAPCAT_QQ and not cfg.ENABLE_QQ_OFFICIAL_BOT:
        log_all("🟡 QQ 推送通道均未启用：成员消息会被抓取并记录，但不会推送到 QQ", is_error=True)
```

改为：
```python
    if not cfg.ENABLE_NAPCAT_QQ and not cfg.ENABLE_QQ_OFFICIAL_BOT and not cfg.ENABLE_TG_BOT:
        log_all("🟡 所有推送通道均未启用：成员消息会被抓取并记录，但不会推送", is_error=True)
```

同时在各通道检查成功后，记录到 HealthTracker：

在 NapCat 检查成功后（第 52 行 `log_all("🟢 NapCat QQ 连通正常")` 之后）添加：
```python
                health.get_tracker().record_channel("napcat", True)
```
在 NapCat 失败处（第 54-58 行）添加：
```python
                health.get_tracker().record_channel("napcat", False, f"HTTP {resp.status_code}")
```
和：
```python
                health.get_tracker().record_channel("napcat", False, "连接失败")
```

在 TG Bot 检查成功后（第 72 行 `log_all("🟢 TG Bot 连通正常")` 之后）添加：
```python
                health.get_tracker().record_channel("tg", True)
```
TG Bot 失败已有第 72 行的逻辑，在 `all_ok = False` 之前添加：
```python
                health.get_tracker().record_channel("tg", False, "无法连接")
```

在账号凭证检查全部通过后（第 94 行 `log_all(f"🟢 账号凭证完整（{len(needed)} 个账号）"）` 之后），为有 token 过期信息的账号记录状态。添加：

```python
        for acc_id in sorted(needed):
            remaining = get_token_remaining_seconds(acc_id)
            if remaining is not None:
                health.get_tracker().record_token(acc_id, max(0, remaining))
```

- [ ] **Step 3: 在 `main()` 中调用 `health.initialize()`**

在 `main()` 的 `tgbot.initialize()` 之后（第 232 行之后）添加：

```python
    health.initialize(
        summary_interval=cfg.HEALTH_SUMMARY_INTERVAL,
        error_buffer=cfg.HEALTH_ERROR_BUFFER,
        token_warn_seconds=cfg.HEALTH_TOKEN_WARN_SECONDS,
    )
```

- [ ] **Step 4: 在 `_run_loop()` 每轮结束时输出摘要**

在 `_run_loop()` 中 `error_members` 检查结束之后、`wait_time` 计算之前（`log_all(f"⚠️ 巡查完毕...")` 之后），添加：

```python
        summary = health.get_tracker().cycle_complete()
        if summary:
            log_all(summary)
```

- [ ] **Step 5: 验证模块导入和初始化**

```
python -c "from src import health; from src.app import main; print('Import OK')"
```
Expected: `Import OK`

- [ ] **Step 6: Commit**

```bash
git add src/app.py
git commit -m "feat: integrate HealthTracker into main loop and health check"
```

---

### Task 4: 改造 `src/notifier.py` — 通道记录 + send_alert_message bug 修复

**Files:**
- Modify: `src/notifier.py`

**Interfaces:**
- Consumes: `src.health.get_tracker()`, `HealthTracker.record_channel()`, `HealthTracker.record_error()`
- Produces: `send_alert_message(target_group, text)` 现在支持 NapCat/TG/多Bot

- [ ] **Step 1: 添加 import**

在 `src/notifier.py` 顶部添加（第 8 行 `from src.platforms import tgbot` 之后）：

```python
from src import health
from src.health import ErrorTier
```

同时把第 5 行 `from src.logger import log_all` 改为：

```python
from src.logger import error_logger, log_all
```

NapCat 的 `send_qq_message` 已有导入（第 6 行），tgbot 通过模块导入访问 `tgbot.send_alert`（第 8 行）。

- [ ] **Step 2: 在 `send_member_message()` 中添加通道记录**

修改 `send_member_message()`：

在 NapCat 发送成功后（第 38 行 `ok = await send_qq_message(gid, message_chain)` 附近），添加：

```python
            if cfg.ENABLE_NAPCAT_QQ:
                for gid in member["target_groups"]:
                    ok = await send_qq_message(gid, message_chain)
                    health.get_tracker().record_channel(
                        f"napcat:{gid}", ok,
                        err=None if ok else f"群 {gid} 发送失败"
                    )
                    if not ok:
                        napcat_ok = False
                        log_all(f"⚠️ NapCat QQ 推送失败 (群 {gid})", is_error=True)
```

在官方 Bot 发送后添加：

```python
    if cfg.ENABLE_QQ_OFFICIAL_BOT:
        bots = get_configured_bots()
        for bot in bots:
            ok = await bot.send_message_chain(member, message_chain)
            health.get_tracker().record_channel(
                f"official:{bot.name}", ok,
                err=None if ok else f"{bot.name} 发送失败"
            )
            if not ok:
                log_all(f"⚠️ 官方 QQ Bot [{bot.name}] 推送失败", is_error=True)
```

在 TG Bot 发送后添加：

```python
    if cfg.ENABLE_TG_BOT:
        tg_ok = await tgbot.send_member_message(member, message_chain)
        health.get_tracker().record_channel(
            "tg", tg_ok,
            err=None if tg_ok else f"{member.get('m_name', '?')} TG 发送失败"
        )
        if not tg_ok:
            log_all(f"⚠️ TG Bot 推送失败 [{member.get('m_name', '?')}]", is_error=True)
```

- [ ] **Step 3: 重写 `send_alert_message()` 以支持 NapCat/TG/多Bot**

完整替换 `send_alert_message()` 函数（第 60-71 行）：

```python
async def send_alert_message(target_group: int, text: str) -> bool:
    """向所有已启用的推送通道发送系统警报。"""
    channels = enabled_channels()
    if not channels:
        log_all(f"⏸️ 没有可用的推送通道，警报未发送: {text}", is_error=True)
        return False

    any_ok = False

    # NapCat 群告警
    if cfg.ENABLE_NAPCAT_QQ:
        from src.platforms.napcat import send_qq_message
        alert_chain = [{"type": "text", "data": {"text": f"📢 系统警报\n{text}"}}]
        ok = await send_qq_message(target_group, alert_chain)
        if ok:
            any_ok = True
        else:
            health.get_tracker().record_error(
                f"NapCat 告警发送失败 (群 {target_group})", ErrorTier.TRANSIENT
            )
            if error_logger:
                error_logger.error(f"NapCat 告警发送失败: {text}")

    # QQ 官方 Bot 告警 — 发给所有已配置的 Bot
    if cfg.ENABLE_QQ_OFFICIAL_BOT:
        from src.platforms.qq_official import get_configured_bots
        bots = get_configured_bots()
        if not bots:
            log_all(f"⏸️ 没有已配置的 QQ 官方 Bot，警报未发送: {text}", is_error=True)
        else:
            for bot in bots:
                ok = await bot.send_text(f"📢 系统警报\n{text}")
                if ok:
                    any_ok = True
                else:
                    health.get_tracker().record_error(
                        f"官方Bot [{bot.name}] 告警发送失败", ErrorTier.TRANSIENT
                    )

    # TG Bot 告警
    if cfg.ENABLE_TG_BOT:
        # 使用成员的 tg_chat_id 来发送告警？TG 没有类似 target_group 的概念。
        # 这里需要从 account 关联的第一个成员获取 tg_chat_id。
        # 查找 target_group 对应的成员的 tg_chat_id
        tg_chat_id = ""
        for m in cfg.MONITOR_LIST:
            if target_group in m["target_groups"]:
                cid = (m.get("tg_chat_id") or "").strip()
                if cid:
                    tg_chat_id = cid
                    break
        if tg_chat_id:
            ok = await tgbot.send_alert(tg_chat_id, text)
            if ok:
                any_ok = True
            else:
                health.get_tracker().record_error(
                    f"TG Bot 告警发送失败", ErrorTier.TRANSIENT
                )
        else:
            log_all(f"⏸️ 群 {target_group} 无关联 TG 频道，TG 警报跳过", is_debug=True)

    return any_ok
```

（所需 import 已在 Step 1 中完成：`from src.health import ErrorTier` 和 `from src.logger import error_logger, log_all`）

- [ ] **Step 4: 验证 import**

```
python -c "from src.notifier import send_alert_message, send_member_message; print('Import OK')"
```
Expected: `Import OK`

- [ ] **Step 5: Commit**

```bash
git add src/notifier.py
git commit -m "fix: add channel recording to send_member_message; fix send_alert_message for NapCat/TG/multi-bot"
```

---

### Task 5: 改造 `src/fetcher.py` — 成员拉取/推送结果记录

**Files:**
- Modify: `src/fetcher.py`

**Interfaces:**
- Consumes: `src.health.get_tracker()`, `ErrorTier`, `HealthTracker.record_member_fetch()`, `HealthTracker.record_member_push()`
- Produces: nothing new externally

- [ ] **Step 1: 添加 import**

在 `src/fetcher.py` 顶部 `from src.notifier import send_member_message` 之后添加：

```python
from src.health import ErrorTier, get_tracker as _health_tracker
```

- [ ] **Step 2: 在 `_fetch_member_messages()` 中添加拉取结果记录**

在 `_fetch_member_messages()` 中：

HTTP 200 成功路径（第 188 行 `if resp.status_code == 200:` 块内，`return` 语句之前），添加：

```python
                _health_tracker().record_member_fetch(m_name, True)
```

在 `_fetch_member_messages()` 中各失败路径添加记录。需要修改 4 个失败出口（未包括 `return None` 的类型是成功的失败出口）：

在函数开头 `cred` 检查失败处（第 130 行 `return None` 之前）：
```python
        _health_tracker().record_member_fetch(
            m_name, False, ErrorTier.PERSISTENT, f"账号 {account_id} 无可用凭据"
        )
```

在 API 返回非 JSON 处（第 192 行 `return None` 之前）：
```python
                    _health_tracker().record_member_fetch(
                        m_name, False, ErrorTier.TRANSIENT, "API 响应非 JSON"
                    )
```

在 401 刷新后仍失败处（第 215-216 行 `return None` 的 `if attempt >= MAX_FETCH_ATTEMPTS` 分支）：
```python
                    _health_tracker().record_member_fetch(
                        m_name, False, ErrorTier.PERSISTENT,
                        f"401 认证失败 (已重试{MAX_FETCH_ATTEMPTS}次)"
                    )
```

在 401 mobile 刷新失败处（第 224 行 `return None`）：
```python
                        _health_tracker().record_member_fetch(
                            m_name, False, ErrorTier.PERSISTENT, "移动端 Token 刷新失败"
                        )
```

在 401 web 刷新失败处（第 228 行 `return None`）：
```python
                        _health_tracker().record_member_fetch(
                            m_name, False, ErrorTier.PERSISTENT, "Web Token 刷新失败"
                        )
```

在其他异常状态码处（第 236 行 `return None`）：
```python
                _health_tracker().record_member_fetch(
                    m_name, False, ErrorTier.TRANSIENT, f"HTTP {resp.status_code}"
                )
```

在网络错误重试耗尽处（第 248 行 `return None`）：
```python
                _health_tracker().record_member_fetch(
                    m_name, False, ErrorTier.TRANSIENT, f"网络错误: {format_httpx_error(e)}"
                )
```

在未预料错误处（第 252 行 `return None` 的 Exception 块内）：
```python
            _health_tracker().record_member_fetch(
                m_name, False, ErrorTier.TRANSIENT, f"未预料错误: {type(e).__name__}"
            )
```

- [ ] **Step 3: 在 `_push_member_messages()` 末尾添加推送结果记录**

在 `_push_member_messages()` 的函数末尾，`return True` 之前（第 282 行）：

```python
    _health_tracker().record_member_push(m_name, True)
    return True
```

在失败返回处（第 273 行 `return False` 之前）：
```python
            _health_tracker().record_member_push(m_name, False)
            return False
```

- [ ] **Step 4: 验证 import**

```
python -c "from src.fetcher import fetch_member_messages, push_member_messages; print('Import OK')"
```
Expected: `Import OK`

注意：fetcher 模块依赖 `initialize()` 注入 `_http_client` 和 `_semaphore`，直接调用 `fetch_member_messages` 会因 `_http_client is None` 而失败。仅测试 import 即可。

- [ ] **Step 5: Commit**

```bash
git add src/fetcher.py
git commit -m "feat: add member fetch/push result recording to HealthTracker"
```

---

### Task 6: 改造 `config/credentials.py` — Token 状态和告警冷却记录

**Files:**
- Modify: `config/credentials.py`

**Interfaces:**
- Consumes: `src.health.get_tracker()`, `HealthTracker.record_token()`, `HealthTracker.record_alert_cooldown()`
- Produces: nothing new externally

- [ ] **Step 1: 添加 import**

在 `config/credentials.py` 顶部（`from src.logger import ...` 之后）添加：

```python
from src.health import get_tracker as _health_tracker
```

- [ ] **Step 2: 在 `refresh_mobile_token()` 中记录 Token 状态和告警冷却**

在 `refresh_mobile_token()` 函数中：

成功路径（第 190 行 `return True` 之前 `log_all(f"✅ 账号 {account_id} 移动端续期成功")` 之后）：

```python
                    # 记录 Token 状态
                    remaining = get_token_remaining_seconds(account_id)
                    if remaining is not None:
                        _health_tracker().record_token(account_id, max(0, remaining))
```

告警冷却路径（第 220-222 行，`log_all(f"⏳ 账号 {account_id} 报警冷却中...")` 之前或之后）：

```python
        _health_tracker().record_alert_cooldown(account_id, float(remaining))
```

- [ ] **Step 3: 在 `refresh_token()` 中记录 Token 状态和告警冷却**

同样在 `refresh_token()` (web 端) 函数中做相同处理：

成功路径（第 426-427 行 `return True` 之前）：

```python
                    remaining = get_token_remaining_seconds(account_id)
                    if remaining is not None:
                        _health_tracker().record_token(account_id, max(0, remaining))
```

告警冷却路径（第 452-454 行）：

```python
        _health_tracker().record_alert_cooldown(account_id, float(remaining))
```

失败路径（两个函数最后的 `return False` 之前，已经在 record_token 调用处含 remaining=0 的逻辑）。实际上在失败时，上方的 `record_token(acc_id, 0)` 调用已经会在 record_token 内部生成 PERSISTENT 错误。所以失败路径不需要额外处理。

- [ ] **Step 4: 验证 import**

```
python -c "from config.credentials import load_all_accounts, proactive_refresh_if_expiring; print('Import OK')"
```
Expected: `Import OK`

- [ ] **Step 5: Commit**

```bash
git add config/credentials.py
git commit -m "feat: record token status and alert cooldown to HealthTracker"
```

---

### Task 7: 端到端验证

**Files:**
- Modify: none (validation only)

- [ ] **Step 1: 运行配置热重载测试**

```
python -c "
import config.config as cfg
print('Health config:', cfg.HEALTH_SUMMARY_INTERVAL, cfg.HEALTH_ERROR_BUFFER, cfg.HEALTH_TOKEN_WARN_SECONDS)
print('TG Bot:', cfg.ENABLE_TG_BOT, cfg.TG_BOT_TOKEN[:10] if cfg.TG_BOT_TOKEN else 'N/A')
# 测试热重载
result = cfg.reload()
print('Reload result:', result)
print('Health config after reload:', cfg.HEALTH_SUMMARY_INTERVAL, cfg.HEALTH_ERROR_BUFFER, cfg.HEALTH_TOKEN_WARN_SECONDS)
"
```
Expected: 三行输出，reload 后值不变（因为文件没改）。

- [ ] **Step 2: 运行 HealthTracker 单元测试**

```
python -c "
from src import health
health.initialize(summary_interval=1, error_buffer=10, token_warn_seconds=600)
t = health.get_tracker()
t.record_channel('napcat:123', True)
t.record_channel('tg', False, '连接超时')
t.record_member_fetch('test_member', True)
t.record_member_push('test_member', True)
t.record_token('test_acc', 3600.0)
s = t.cycle_complete()
print(s)
print('---')
print('PASS: cycle_complete returned summary')
"
```
Expected: 输出格式化的摘要字符串。

- [ ] **Step 3: 验证 send_alert_message 的 import 链**

```
python -c "
from src.notifier import send_alert_message
from src.platforms.napcat import send_qq_message
from src.platforms.qq_official import get_configured_bots
from src.platforms.tgbot import send_alert
print('All alert imports OK')
"
```
Expected: `All alert imports OK`

- [ ] **Step 4: 检查所有模块无循环 import**

```
python -c "
import src.health
import src.app
import src.fetcher
import src.notifier
import config.credentials
import config.config
print('All modules import without circular dependency')
"
```
Expected: `All modules import without circular dependency`

- [ ] **Step 5: Commit（如有 fixup）**

如果没有修改则不 commit。如有小的调整，执行：
```bash
git add -A && git commit -m "chore: end-to-end validation fixes"
```
