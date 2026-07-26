# 优化批次 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 2026-07-26 第二轮审查（升级/优化视角）中的第一层 7 条与第二层 5 条：HTTP 连接复用、消除无意义 I/O、死配置/死代码清理、官方 Bot 媒体去重、热重载一致性、SIGTERM 优雅退出、健康统计滚动窗口、CI 与依赖锁版本。第三层（通道抽象 / 中立消息结构 / SQLite / 去 PTB）明确**不做**，留待确定要加新通道时一并重构。

**Architecture:** 不改变推送可靠性语义与持久化数据格式。`config.config` 暴露的变量名不变；工具脚本（list_members / test_models）在**不注入** client 的情况下仍须可独立运行（凭证模块保留按需临时建 client 的回退）。

## Global Constraints

- 每个 commit 后的树可编译、两个测试脚本可通过
- 不引入运行时新依赖（ruff 仅进 CI，不进 requirements）
- Windows（开发机）与 Linux（容器）都要能跑：信号处理必须处理 `add_signal_handler` 的 `NotImplementedError`

---

## Commit A — 性能与清理（第一层 1–7）

### Task A1: 共享 httpx client 注入
**Files:** `src/translator.py`, `config/credentials.py`, `src/app.py`

- [ ] translator: `initialize(client=None)` 注入；请求走共享 client（`timeout` 按请求传 `cfg.TRANSLATE_TIMEOUT`），未注入时按需临时创建（工具脚本回退）
- [ ] credentials: 新增 `initialize(client)` + `_post()` helper，`refresh_token` / `refresh_mobile_token` 复用；同样保留回退
- [ ] app.main: 调整初始化顺序 —— 先建 client，再统一注入（credentials / translator / napcat / qq_official / fetcher），重排步骤注释

### Task A2: 时间戳文件值未变时跳过写盘
**Files:** `config/credentials.py`

- [ ] `write_time_record` 增加模块级 `_last_time_written` 缓存，值相同直接 return（9 成员 × 日间 2min/轮 ≈ 每天 6500 次无意义写入）
- [ ] 仅在 `os.replace` 成功后更新缓存

### Task A3: 删除死配置 `gemini_models[].rpm`
**Files:** `config/config.py`, `config/config.json`, `config/config.schema.json`

- [ ] `_DEFAULTS` 与 `config.json` 中删掉 `rpm`；schema 的 `required` 改为 `["name", "url"]` 并移除 `rpm` 属性（未限制 additionalProperties，带 rpm 的旧配置仍可通过校验）

### Task A4: 死代码清理
**Files:** `src/utils.py`, `src/notifier.py`

- [ ] 删 `RateLimiter.update_interval`（零调用）
- [ ] 删 `send_alert_message` 内与顶部重复的两处函数内 import

### Task A5: `reload()` 失败时打印原因
**Files:** `config/config.py`

- [ ] `except SystemExit as e` 打印 `e.code`；`except Exception` 打印 traceback（config 不能 import logger——循环依赖，用 print）

### Task A6+A7: 官方 Bot 媒体一次下载 + 独立超时
**Files:** `src/platforms/qq_official.py`, `src/notifier.py`, `config/config.py`

- [ ] qq_official 整体换 `import config.config as cfg` 访问（本文件顺带完成 Task B1 的转换）
- [ ] `_download_media` 从 Bot 方法提为模块函数；新增 `download_media_payloads(member, chain) -> list[(type, bytes|None)]`
- [ ] `send_message_chain(..., media_payloads=None)`：传入时直接用，None 时自行下载（兼容旧调用）
- [ ] notifier 在 Bot 循环前下载一次，分发给所有 Bot（上传仍按 Bot 各自进行——file_info 与 app_id 绑定）
- [ ] 新增 `qq_official_media_timeout`（默认 60s），下载 25MB 视频不再复用 15s 的 API 超时

## Commit B — 一致性与健壮性（第二层 8/9/10）

### Task B1: 热重载标量一致性
**Files:** `config/credentials.py`, `src/dedup.py`（qq_official 已在 A6 完成）

- [ ] 两模块从 `from config.config import X` 改为 `import config.config as cfg` + `cfg.X`，使 `alert_cooldown` / `sent_ids_max` / `token_refresh_before_seconds` 热改后真实生效

### Task B2: SIGTERM 优雅退出
**Files:** `src/app.py`

- [ ] `_install_stop_handlers(stop_event)`：优先 `loop.add_signal_handler`（Linux/容器），`NotImplementedError` 时回退 `signal.signal` + `call_soon_threadsafe`（Windows）
- [ ] main 改为 `asyncio.wait({loop_task, stop_task}, FIRST_COMPLETED)`，收到信号后 cancel 主循环，保证 `finally` 的 observer/client 清理执行
- [ ] 保留 `KeyboardInterrupt` 兜底

### Task B3: 通道成功率滚动清零
**Files:** `src/health.py`, `README.md`, `tests/test_units.py`

- [ ] `cycle_complete` 输出摘要后重置各通道 `success/total`（保留 `last_error`）——跑一周后 `1847/1848` 已无判断价值
- [ ] README 摘要计数说明改为「自上次摘要以来」
- [ ] test_units 补 `test_health_rolling` 与 `test_time_record_skip`

## Commit C — 工程基建（第二层 11/12）

### Task C1: 依赖锁版本
**Files:** `requirements.txt`

- [ ] 全部加上下界；`websockets` 标注仅 tools 需要

### Task C2: CI + ruff
**Files:** `.github/workflows/ci.yml`, `ruff.toml`

- [ ] workflow：Python 3.10 + 3.12 矩阵，`pip install -r requirements.txt` → `ruff check .` → `compileall` → 两个测试脚本
- [ ] ruff 先本地跑通再进 CI，规则从默认集起步

## 执行记录（2026-07-26）

| Commit | 内容 | 验证 |
|---|---|---|
| A `perf: reuse shared HTTP clients...` | A1–A7 全部 | compileall + 双测试 + import 冒烟 |
| B `fix: graceful SIGTERM shutdown...` | B1–B3 | 同上 + reload 失败输出实测 + 新增 2 个单测 |
| C `chore: add CI...` | C1–C2 | 本地 `ruff check .` 全绿（0.16.0）+ 双测试 |

补充说明：

- ruff 首跑发现 3 个真问题并已修：`health.py` 死导入 `field`、`notifier.py` 无占位符 f-string、`test_units.py` 死导入 `ROLE_TRANSLATION`。`config.py` 的 7 个 F821 是 `_apply_config` 动态 `setattr` 的误报，`test_config_load.py` 的 F401 是「导入即测试」的设计——均入 `ruff.toml` 的 per-file-ignores。
- SIGTERM 路径在 Windows 上只能验证注册不抛异常；容器内 `docker stop` 的完整优雅退出**未实测**，属遗留验证项。
- 官方 Bot 媒体去重的真实收益需双 Bot + 真实媒体消息验证，同样遗留。
- 依赖上界按 2026-05 已知版本设定（PTB <23、websockets <16、httpx <1.0），若 pip 解析冲突可放宽。

## 明确不做（第三层，留待加新通道时）

- 通道抽象（Channel 协议）与中立 Message 结构 —— 一次重构，动 notifier/所有 platform
- SQLite 替代 27 个状态小文件
- 去掉 python-telegram-bot 改裸 HTTP —— 取舍不明确，倾向不动
