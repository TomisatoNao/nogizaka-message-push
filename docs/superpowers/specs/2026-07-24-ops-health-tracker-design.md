# 运维体验优化：HealthTracker 健康状态追踪

## 背景

当前项目为本地 Windows 长期运行模式，运维人员偶尔查看终端。现状痛点：

- 无状态摘要：需要翻阅大量滚动日志才能判断系统是否正常
- 告警覆盖不完整：`send_alert_message` 仅支持 QQ 官方 Bot，NapCat/TG 告警未接入
- 多 Bot 只发第一个：`send_alert_message` 调用 `bots[0]` 而非全部
- 健康检查日志误导：仅检查 QQ 通道，TG 启用时日志不准确
- 热重载遗漏：`reload()` 未覆盖 `ENABLE_TG_BOT` / `TG_BOT_TOKEN`
- 错误无分级：网络抖动和认证失败混在一起，无法快速判断是否需要人工介入

## 目标

无需外部依赖，本地终端即可快速判断系统健康状态。关键错误有差异化标识，一眼可知是否需要人工介入。

## 设计

### 1. 新增模块 `src/health.py`

遵循项目现有模式：模块级状态 + `initialize()`。

#### 数据结构

```
ErrorTier（枚举）
├── TRANSIENT  — 网络超时、429/5xx、临时连接失败
└── PERSISTENT — 401 刷新后仍失败、凭证缺失、配置错误

ChannelStats（数据类）
├── success: int
├── total: int
└── last_error: str | None

TokenInfo
├── account_id: str
├── remaining: float     # Token 剩余秒数，0 = 失效
└── is_healthy: bool     # remaining > 0

MemberStats
├── name: str
├── fetch_ok: bool
├── push_ok: bool
└── last_error: str | None
```

#### 核心类 `HealthTracker`

| 方法 | 调用方 | 说明 |
|---|---|---|
| `initialize(interval, error_buf, token_warn)` | app.py | 传入配置参数 |
| `record_channel(channel, ok, err?)` | notifier.py | 每次推送的通道成败 |
| `record_member_fetch(name, ok, err_type?)` | fetcher.py | 成员拉取结果 |
| `record_member_push(name, ok)` | fetcher.py | 成员推送结果 |
| `record_token(acc_id, remaining)` | credentials.py | Token 刷新后状态 |
| `record_alert_cooldown(acc_id, remaining)` | credentials.py | 告警冷却状态 |
| `record_error(msg, tier)` | 各处 | 分级错误，环形 buffer |
| `cycle_complete() → str | None` | app.py | 每轮调用，每 N 轮返回摘要 |

状态保留在内存中（环形 buffer 最近 50 条错误 + 最近 N 轮统计），不持久化到磁盘，不随运行时间无限增长。

### 2. 集成改造

#### `src/app.py`

- 启动时 `health.initialize()`，传入 `config.json` 的三项新配置
- `_health_check()` 的结果写入 HealthTracker 首条记录
- `_run_loop()` 每轮结束时调用 `health.cycle_complete()`，非空则 `log_all()` 输出

#### `src/notifier.py`

- `send_member_message()` 每条通道推送后调 `health.record_channel()`：
  - NapCat 按群记录（`napcat:{gid}`）
  - QQ 官方 Bot 按 Bot 名记录（`official:{bot.name}`）
  - TG Bot 单通道记录
- **Bug 修复**：`send_alert_message()` 补回 NapCat 告警 + TG 告警，多 Bot 发送全部而非仅 `bots[0]`

#### `src/fetcher.py`

- `_fetch_member_messages()` 成功后 → `record_member_fetch(name, True)`
- 401 刷新后仍失败 → `record_member_fetch(name, False, tier=PERSISTENT)`
- 网络错误重试耗尽 → `record_member_fetch(name, False, tier=TRANSIENT)`
- `_push_member_messages()` → `record_member_push(name, ok)`

#### `config/credentials.py`

- Token 刷新成功后解码 JWT，调用 `health.record_token(acc_id, remaining)`
- 失败后调用 `health.record_token(acc_id, 0)`
- 告警冷却中调用 `health.record_alert_cooldown(acc_id, remaining)`

#### `config/config.py`

新增配置项（JSON + schema + `_KEY_TO_VAR`）：

| 键 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `health_summary_interval` | int | 10 | 每隔多少轮输出摘要 |
| `health_error_buffer` | int | 50 | 环形 buffer 保留最近错误数 |
| `health_token_warn_seconds` | int | 600 | Token 低于此值时显示 ⚠️ |

**Bug 修复**：`reload()` 末尾补上 `ENABLE_TG_BOT` / `TG_BOT_TOKEN` 的环境变量覆盖。

#### `src/app.py` — 健康检查日志修正

`_health_check()` 中 "QQ 推送通道均未启用" 的日志改为检查所有三个通道（NapCat + QQ 官方 Bot + TG Bot），仅在全部未启用时才输出。

### 3. 摘要输出格式

```
📊 [状态摘要 #12 · 运行 2h 34m]
  通道: NapCat:g123456 ✅ 12/12 | Official:bot_admin ✅ 12/12 | TG ✅ 12/12
  Token: nogizaka_main 58min · hinata_shared 3.2h · yodel_grad 0min 🔴
  成员: 8/8 拉取正常 · 8/8 推送正常

📊 [状态摘要 #13 · 运行 2h 38m]
  通道: NapCat:g123456 ✅ 13/13 | Official:bot_admin ⚠️ 10/13 | TG ✅ 13/13
  Token: nogizaka_main 54min · hinata_shared 3.1h · yodel_grad 失效 🔴
  成员: 8/8 拉取正常 · 8/8 推送正常
  ⚠️ 近期错误:
    └─ [PERSIST] yodel_grad Token 刷新失败（2轮前）
    └─ [TRANSIENT] Official:bot_admin HTTP 429（最近1轮内）
```

**符号语义：**
- `✅` — 100% 成功
- `⚠️` — 有 TRANSIENT 失败但通道仍可用
- `🔴` — PERSISTENT 错误，需要人工介入

**显示规则：**
- 一切正常 → 三行（通道 + Token + 成员）
- 有 PERSISTENT 错误 → 展开"近期错误"区，最多显示 5 条
- Token 剩余时间格式化：>1h 显示小时，<1h 显示分钟，0 显示 `失效 🔴`

### 4. 变更文件清单

| 文件 | 变更类型 |
|---|---|
| `src/health.py` | **新增** — HealthTracker 模块 |
| `src/app.py` | 改造 — 初始化、健康检查修正、摘要输出 |
| `src/notifier.py` | 改造 — 通道记录 + send_alert_message bug 修复 |
| `src/fetcher.py` | 改造 — 拉取/推送结果记录 |
| `config/credentials.py` | 改造 — Token 状态记录 |
| `config/config.py` | 改造 — 新增 3 个配置项 + 热重载修复 |
| `config/config.schema.json` | 改造 — Schema 新增 3 个字段 |
| `config/config.json` | 改造 — 添加 3 个新键的默认值 |

### 5. 不做什么

- 不引入 Web 面板或 HTTP 端点 —— 本地终端足够
- 不持久化健康状态到磁盘 —— 重启后重新计数即可
- 不改变现有推送语义 —— NapCat 失败仍然阻断时间戳，官方 Bot/TG 仍然旁路
