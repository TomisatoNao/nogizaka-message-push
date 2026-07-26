# 代码审查修复 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉 2026-07-26 代码审查发现的 4 类问题：① 启动顺序与异常兜底导致的告警丢失/进程退出；② Telegram 通道的 HTML 转义与超长 caption 缺陷；③ 配置层缺省值缺口与跨模块隐式协议；④ 坏掉的测试与与实现不符的文档。

**Architecture:** 不改变任何持久化数据格式（`data/web_credentials/`、`data/time_records/`、`data/sent_ids/`），不改变推送可靠性语义（NapCat 失败仍阻断时间戳推进，TG/官方 Bot 仍为旁路），不引入新第三方依赖。`config.config` 暴露的模块级变量名一律不变。

**Tech Stack:** Python 3.10+ stdlib，已有依赖 `httpx`、`json5`、`jsonschema`、`python-dotenv`、`python-telegram-bot`。

## Global Constraints

- 消费者模块的 `from config.config import VAR` 全部保持可用
- 发给 NapCat/OneBot 的 payload 结构不得出现新增字段
- 不引入 pytest 等新依赖，测试沿用 plain `assert` 脚本，放在 `tests/`，用 `python tests/xxx.py` 手动执行
- `.env` 语义不变（仍只在进程启动时加载一次），凭证优先级不变（磁盘 > `.env`），仅补充告警与文档
- 每个批次独立可提交、可回滚；批次 4 的 Task 4.3 依赖 Task 4.2 的实测结果

## 决策记录（2026-07-26 与用户确认）

| 议题 | 结论 |
|---|---|
| 本轮范围 | 批次 1 + 2 + 3 + 4；批次 5（client 复用、群号迁移）延后 |
| Token 轮换 | 不改优先级语义，只加「不一致告警」+ 修正 README |
| 翻译段识别 | chain 加 `_role` 标记，napcat 发送前剥离下划线键 |
| 测试形式 | 保持 plain assert 脚本，移入 `tests/` |

---

## File Structure

| 文件 | 职责 |
|---|---|
| `src/app.py` | 启动顺序调整、Phase 2 异常兜底、watcher 回调接入 |
| `src/utils.py` | `utc_to_jst` 容错解析 |
| `src/logger.py` | 截断改为逐行 |
| `src/constants.py` | **新增** — 翻译段分隔线与 role 常量 |
| `src/platforms/napcat.py` | 打 `_role` 标记、发送前剥离下划线键、引用常量 |
| `src/platforms/tgbot.py` | HTML 转义重写、按 `_role` 提取、caption 超长分条 |
| `src/fetcher.py` | B 站文案引用常量 |
| `src/notifier.py` | 通道名统一、`target_group == 0` 时跳过 NapCat 告警 |
| `config/credentials.py` | `.env` 与磁盘凭证不一致时告警 |
| `config/config.py` | `_DEFAULTS` 补齐、删除重复赋值 |
| `config/config.schema.json` | `required` 收窄、`groups` 转可选 |
| `config/watcher.py` | `on_reload` 回调签名沿用，由 app 注入 `load_all_accounts` |
| `tests/test_config_load.py` | **移动+修复** — 由根目录迁入，修 account key |
| `tests/test_units.py` | **新增** — `utc_to_jst` / `_escape_html` / `_chain_extract` 断言 |
| `tools/test_models.py` | `__main__` 守卫、payload 对齐生产、打印 finishReason |
| `src/translator.py` | （依赖 4.2 结果）thinking 配置与 parts 取值加固 |
| `README.md` | Token 轮换流程、成员表 key、TG 依赖可选性、热重载边界 |

---

## 批次 1 — 稳定性（P0）

### Task 1.1: 修正启动顺序，让启动期告警能发出

**Files:** Modify `src/app.py`

**背景:** `_init_mobile_accounts()` 目前在步骤 1 执行，此时 `napcat._client` 与 `tgbot._bot` 均为 `None`。启动时 refresh_token 失效 → `send_alert_message` 全通道静默失败（NapCat 侧还会白等 3 次重试 3s）。

- [ ] **Step 1:** 将 `await _init_mobile_accounts()` 从步骤 1 末尾移到步骤 4（`fetcher.initialize`）之后、步骤 5 `_health_check()` 之前
- [ ] **Step 2:** 更新 `main()` 内的步骤编号注释，保持注释与实际顺序一致
- [ ] **Step 3:** 人工验证 — 临时把某 mobile 账号的 `REFRESH_TOKEN` 改错，启动后应在已启用通道收到「移动端续期失败」告警，且日志中不出现 `'NoneType' object has no attribute 'post'`

### Task 1.2: Phase 2 加异常兜底

**Files:** Modify `src/app.py`

- [ ] **Step 1:** `_run_loop` 中 `fetcher.push_member_messages(...)` 调用包 `try/except Exception`，异常时 `log_all(traceback)` + `health.get_tracker().record_member_push(name, False)` + `record_error(..., ErrorTier.TRANSIENT)`，并计入 `error_members`，继续处理下一个成员
- [ ] **Step 2:** 整轮循环体包一层 `try/except Exception`，保证异常后仍执行 `_next_interval()` 与 `asyncio.sleep(wait_time)`（避免退化成空转死循环）
- [ ] **Step 3:** 验证 — 临时在 `_handle_message` 首行 `raise RuntimeError("inject")`，确认单成员失败不影响其余成员，且循环按间隔继续

### Task 1.3: `utc_to_jst` 容错解析

**Files:** Modify `src/utils.py`

**Interfaces:** `utc_to_jst(utc_str: str, fmt: str = "%m/%d %H:%M:%S") -> str` — 签名不变；解析失败返回原字符串而非抛异常

- [ ] **Step 1:** 改为优先 `datetime.fromisoformat`（把结尾 `Z` 换成 `+00:00`），兼容小数秒与 `+09:00` 偏移；无 tzinfo 时按 UTC 处理
- [ ] **Step 2:** 保留原 `strptime` 作为回退；两者都失败时返回入参原文并 `log_all(..., is_debug=True)`
- [ ] **Step 3:** 在 `tests/test_units.py` 覆盖 `2026-07-26T12:00:00Z` / `...T12:00:00.123Z` / `...T21:00:00+09:00` / `garbage` 四种输入

### Task 1.4: 状态摘要不再被截断

**Files:** Modify `src/logger.py`

- [ ] **Step 1:** `log_all` 的 120 字符截断改为逐行截断（按 `\n` 拆分后各自截断再拼回），单行日志行为保持不变
- [ ] **Step 2:** 验证 — `health_summary_interval` 临时设 1，确认终端输出的摘要与 `logs/error_debug.log` 中的内容一致、无 `...[TRUNCATED]`

---

## 批次 2 — Telegram 通道正确性

### Task 2.1: 重写 HTML 转义

**Files:** Modify `src/platforms/tgbot.py`

**背景:** 现有 `_escape_html` 先全转义再「还原白名单标签」，会把成员文本里的 `<b>` 变成真标签，`&lt;a ` → `<a ` 还会产出畸形 HTML 导致 Telegram 400。该 hack 仅为了让 `send_alert` 的 `<b>` 生效。

**Interfaces:**
- `_escape_html(text: str) -> str` — 纯转义 `& < >`，无任何还原
- `_send_html(chat_id: str, html: str) -> bool`（新增，内部）— 接收**已转义并已拼好标签**的 HTML，负责长度裁剪、重试、flood control
- `_post_message(chat_id: str, text: str) -> bool` — 保持签名；内部 = 转义 → `_send_html`

- [ ] **Step 1:** 删除 `_escape_html` 中的白名单还原循环
- [ ] **Step 2:** 抽出 `_send_html`，把 `_post_message` 现有的重试/裁剪逻辑搬进去
- [ ] **Step 3:** `send_alert` 改为 `_send_html(chat_id, f"<b>📢 系统警报</b>\n{_escape_html(text)}")`
- [ ] **Step 4:** `_post_media` 的 caption 同样只用纯转义结果
- [ ] **Step 5:** 在 `tests/test_units.py` 断言含 `<b>`、`&`、`<a href="x">`、`</i>` 的输入全部实体化
- [ ] **Step 6:** 人工验证 — 向测试频道实发一条含 `<` 与 `&` 的消息，确认送达且无 400

### Task 2.2: 翻译段改用显式 `_role` 标记

**Files:** Create `src/constants.py`; Modify `src/platforms/napcat.py`, `src/platforms/tgbot.py`, `src/fetcher.py`

**Interfaces:**
- `src/constants.py`：`TRANSLATION_SEPARATOR: str`（`"\n\n" + "-" * 40 + "\n\n"`）、`ROLE_KEY = "_role"`、`ROLE_TRANSLATION = "translation"`
- `build_message_chain` 产出的翻译段形如 `{"type": "text", "data": {...}, "_role": "translation"}`

- [ ] **Step 1:** 新建 `src/constants.py`
- [ ] **Step 2:** `napcat.build_message_chain` 引用 `TRANSLATION_SEPARATOR` 并给翻译段加 `_role`
- [ ] **Step 3:** `napcat._post_message` 序列化前深拷贝 chain 并剥离所有 `_` 开头的键（确保 OneBot payload 干净）；`_split_video_record_chain` 的分批逻辑不受影响
- [ ] **Step 4:** `fetcher._handle_message` 的 B 站文案改用 `TRANSLATION_SEPARATOR`
- [ ] **Step 5:** `tgbot._chain_extract` 改为按 `item.get(ROLE_KEY) == ROLE_TRANSLATION` 判断，删除分隔线字符串匹配
- [ ] **Step 6:** `tests/test_units.py` 覆盖 `_chain_extract` 的三种 chain（纯文本 / 文本+翻译 / 图片+翻译），并断言剥离函数输出不含 `_role`
- [ ] **Step 7:** 人工验证 — `DEBUG_LOG_QQ_PAYLOAD=1` 启动，确认发给 NapCat 的 payload 预览中无 `_role`

### Task 2.3: caption 超长不再吞掉翻译

**Files:** Modify `src/platforms/tgbot.py`

- [ ] **Step 1:** `send_member_message` 中按**转义后**长度判断：`len(_escape_html(full_text)) > 1024` 时改为「先发一条完整文本消息，再发不带 caption 的媒体」；未超长仍走「首个媒体带 caption」
- [ ] **Step 2:** `_post_media` 的 1024 裁剪保留作为兜底
- [ ] **Step 3:** 人工验证 — 构造长正文 + 图片的消息，确认送达两条且原文与翻译均完整

---

## 批次 3 — 配置健壮性

### Task 3.1: `_DEFAULTS` 补齐缺口

**Files:** Modify `config/config.py`, `config/config.schema.json`

**背景:** `_KEY_TO_VAR` 中 `qq_bot_api` / `enable_translation` / `sleep_start_hour` / `sleep_end_hour` / `day_interval` / `night_interval` / `accounts` / `monitor_list` 没有内置默认值，完全依赖 schema 的 `required` 兜底。一旦 schema 放宽，`tuple(None)` 与 `in_hour_range(h, None, None)` 会在启动时炸。

- [ ] **Step 1:** `_DEFAULTS` 补入：`qq_bot_api="http://127.0.0.1:3000/send_group_msg"`、`enable_translation=True`、`sleep_start_hour=2`、`sleep_end_hour=7`、`day_interval=[120,180]`、`night_interval=[1500,1800]`、`accounts={}`、`monitor_list=[]`
- [ ] **Step 2:** schema 的 `required` 收窄为 `["accounts", "monitor"]`
- [ ] **Step 3:** 验证 — 临时把 `config.json` 精简到只剩 `accounts` + `monitor`，`python tests/test_config_load.py` 通过且能正常启动

### Task 3.2: 支持只推 TG 的成员

**Files:** Modify `config/config.schema.json`, `src/fetcher.py`, `src/notifier.py`, `src/app.py`

- [ ] **Step 1:** schema 中 `monitor.items.required` 去掉 `groups`；`groups` 的 `minItems` 降为 0
- [ ] **Step 2:** `fetcher._fetch_member_messages` 的 `target_group` 改为 `member["target_groups"][0] if member["target_groups"] else 0`
- [ ] **Step 3:** `notifier.send_alert_message` 在 `target_group == 0` 时跳过 NapCat 分支，只走官方 Bot / TG
- [ ] **Step 4:** `app._health_check` 增加校验：某成员既无 `target_groups` 又无 `tg_chat_id` 且未启用官方 Bot 时 → 打印 🔴 并计入 `all_ok = False`
- [ ] **Step 5:** 验证 — 临时加一个只配 `tg` 的成员，`tests/test_config_load.py` 通过、启动健康检查无报错、消息只走 TG

### Task 3.3: 热重载后补加载新账号凭证

**Files:** Modify `src/app.py`

**注意:** 这只覆盖「`config.json` 新增账号 + 磁盘已有凭证文件」的情形。`.env` 仍只在进程启动时加载一次，新增/修改 `.env` 值必须重启 —— 该边界需在 README 写明（Task 4.4）。

- [ ] **Step 1:** `start_watcher(config_path, on_reload=...)` 传入回调：重载成功时调用 `load_all_accounts()`（幂等，已有账号自动 skip）并打印结果
- [ ] **Step 2:** 验证 — 安装 watchdog 后运行中往 `config.json` 加一个已有凭证文件的账号，确认日志出现「📂 读取账号凭证」而非「无可用凭据」

### Task 3.4: 配置层清理

**Files:** Modify `config/config.py`, `src/notifier.py`

- [ ] **Step 1:** 删除 `_normalize_config` 里重复的 `cfg["tg_bot_token"]` 赋值（`_load_config` 步骤 7 已统一处理）
- [ ] **Step 2:** `notifier.send_member_message` 的 `record_channel(f"napcat:{gid}", ok)` 改为 `record_channel("napcat", ok, f"群 {gid}")`，与启动健康检查的通道名统一
- [ ] **Step 3:** 验证 — 状态摘要中通道列表不再出现同一通道的两种命名

---

## 批次 4 — 测试与 Gemini 链路

### Task 4.1: 修复并归位配置测试

**Files:** Create `tests/test_config_load.py`; Delete `test_config_load.py`

- [ ] **Step 1:** 迁移到 `tests/test_config_load.py`，补 `sys.path` 处理（上溯一级到项目根）与 `if __name__ == "__main__"` 守卫
- [ ] **Step 2:** Test 3 的 `yodel_graduated` 改为 `yodel_grad`（实测当前退出码 1）
- [ ] **Step 3:** 验证 — `python tests/test_config_load.py` 全绿

### Task 4.2: `tools/test_models.py` 对齐生产

**Files:** Modify `tools/test_models.py`

- [ ] **Step 1:** 加 `if __name__ == "__main__"` 守卫
- [ ] **Step 2:** payload 改为复用 `src.translator._PROMPT_TEMPLATE` 与生产同款 `generationConfig`（`temperature: 0.3`、`maxOutputTokens: 4096`）
- [ ] **Step 3:** 打印每个模型的 `finishReason`、`usageMetadata`（含 `thoughtsTokenCount` 若有）、`parts` 数量与各 part 是否带 `thought`
- [ ] **Step 4:** 执行 `python tools/test_models.py`，记录 4 个模型的实际返回形态 —— **这是 Task 4.3 的输入**

### Task 4.3: 按实测结果加固翻译取值（依赖 4.2）

**Files:** Modify `src/translator.py`

**前置:** 只有 Task 4.2 实测确认「思考 token 撞 `maxOutputTokens` 上限」或「`parts[0]` 可能是 thought 段」之后才动此文件。若实测正常则本 Task 记为 no-change。

- [ ] **Step 1:** 视实测结果在 `generationConfig` 中加 `thinkingConfig`（关闭或限制思考预算）
- [ ] **Step 2:** 取文本改为遍历 `candidates[0].content.parts`，取第一个不带 `"thought": true` 且含 `text` 的 part；`parts` 缺失时按失败处理
- [ ] **Step 3:** `finishReason == "MAX_TOKENS"` 时 `log_all(..., is_error=True)` 后降级下一个模型，避免静默返回截断译文
- [ ] **Step 4:** 验证 — 重跑 `python tools/test_models.py`，并用一条真实长日文消息走完整链路

### Task 4.4: 文档同步

**Files:** Modify `README.md`, `requirements.txt`

- [ ] **Step 1:** 「换一个 Token」章节改为真实流程：编辑 `.env` → **删除 `data/web_credentials/{account}.json`** → 重启（说明热重载与 `.env` 的边界）
- [ ] **Step 2:** 「添加一个新账号」补充「新增 `.env` 凭证需重启」
- [ ] **Step 3:** 监控成员表的 `yodel_graduated` 改为 `yodel_grad`
- [ ] **Step 4:** 依赖表说明 `python-telegram-bot` 已在 `requirements.txt` 中无条件安装（或改为可选安装说明，二者取一，保持文档与文件一致）
- [ ] **Step 5:** 「常见操作」补充只推 TG 的成员写法（`groups` 可省略）
- [ ] **Step 6:** 状态摘要示例改为与实际输出一致（通道计数为累计消息数而非轮次）

### Task 4.5: 凭证不一致告警

**Files:** Modify `config/credentials.py`

- [ ] **Step 1:** `load_all_accounts()` 在磁盘凭证存在的分支中，比对 `acc_cfg.get("init_token")` / `init_refresh_token` 与磁盘值；不一致时 `log_all("⚠️ {acc} 的 .env 凭证与磁盘凭证不同，当前使用磁盘凭证；如需强制轮换请删除 data/web_credentials/{acc}.json", is_error=True)`
- [ ] **Step 2:** 空的 `.env` 值不触发告警（避免 mobile 账号 `TOKEN=` 留空时误报）
- [ ] **Step 3:** 验证 — 把 `.env` 中某账号 `TOKEN` 改成 `eyJtest`，启动应看到该告警且仍使用磁盘凭证

---

## 执行记录（2026-07-26，以本节为准，上方 checkbox 未逐项勾选）

| Task | 状态 | 说明 |
|---|---|---|
| 1.1 启动顺序 | ✅ 已改 + 已验证 | 冒烟脚本断言 `napcat._client` 在 `_init_mobile_accounts` 前已注入，并实测 `send_alert_message` 返回 False 而非 AttributeError |
| 1.2 Phase 2 兜底 | ✅ 已改 | 拆出 `_run_cycle()`；单成员 push 与整轮各包一层 except，`CancelledError` 显式放行 |
| 1.3 时间解析 | ✅ 已改 + 单测 | 7 种输入断言通过 |
| 1.4 摘要截断 | ✅ 已改 + 已验证 | 冒烟输出中多行摘要完整显示；超长单行仍截断 |
| 2.1 HTML 转义 | ✅ 已改 + 单测 | 删除白名单还原；新增 `_send_html` / `_to_html`（明文层二分裁剪，不切断实体）；重试逻辑统一为 `_send_with_retry` |
| 2.2 `_role` 标记 | ✅ 已改 + 单测 | 新增 `src/constants.py`、`napcat.strip_internal_keys`；断言 payload 无下划线键、正文含分隔线时不再误判 |
| 2.3 caption 超长 | ✅ 已改 | 超 1000 字符（转义后）改为「先文本后无 caption 媒体」 |
| 3.1 `_DEFAULTS` | ✅ 已改 + 已验证 | 最小配置（仅 accounts + monitor）实测可加载，间隔/休眠取到默认值 |
| 3.2 只推 TG | ✅ 已改 + 已验证 | schema 放开 `groups`；`fetcher` / `_alert_group_for_account` / `send_alert_message` 均处理 0；健康检查新增孤儿成员校验 |
| 3.3 热重载补凭证 | ✅ 已改 | 接入 `_on_config_reload`；`.env` 不重读的边界已写进 README |
| 3.4 清理 | ✅ 已改 | 删重复 `tg_bot_token` 赋值；通道名统一为 `napcat` |
| 4.1 配置测试 | ✅ 已改 + 通过 | 迁入 `tests/`，修 `yodel_grad`，新增 monitor 规范化校验（Test 4） |
| 4.2 `test_models` | ✅ 已改，⚠️ 未实测 | 本机无 `.env`，`GEMINI_API_KEY` 未配置，脚本直接提示后退出 |
| 4.3 Gemini 加固 | ⚠️ **部分完成** | 见下方说明 |
| 4.4 文档 | ✅ 已改 | Token 轮换流程、新增账号需重启、`yodel_grad`、TG 依赖、只推 TG 写法、摘要示例与计数口径 |
| 4.5 凭证不一致告警 | ✅ 已改 | 用 `_env_seen.json` 指纹判断 `.env` 是否真的变过 |

### Task 4.3 为何只做一半

`GEMINI_API_KEY` 未配置 → 无法测得 `finishReason` / `thoughtsTokenCount`，因此：

- **已做**（不依赖实测，纯防御）：新增 `_extract_text()` —— 遍历 parts 取第一个非 `thought` 段、`MAX_TOKENS` 截断时降级下一个模型、结构异常时记录 `finishReason` 而非静默 KeyError。
- **未做**（需实测）：`generationConfig.thinkingConfig`。并非所有模型端点都接受该字段，传错会让整条翻译链路 400 —— 宁可不加。代码里已留注释指向 `tools/test_models.py`。

**待你补 `.env` 后执行：** `python tools/test_models.py`，若输出中 `thoughts=` 有值且 `finishReason=MAX_TOKENS`，再按 Task 4.3 Step 1 加 `thinkingConfig`。

### 顺手修掉的额外问题（审查时未列出）

- `tgbot.health_check()` 在 `_bot is None` 时静默返回 False → 启动检查里 TG 那一行凭空消失。已改为明确打印 🔴 及原因（冒烟时发现）。
- `load_all_accounts()` 对缺少 `init_cookie` / `init_token` 的 web 账号会抛 `KeyError`，掩盖健康检查本该给出的「凭证缺失」提示。已改为留空 + 由 `validate_account_cred` 统一报告。
- 孤儿成员校验最初漏算 QQ 官方 Bot（用户指出）。已改为「官方 Bot 可用则覆盖所有成员」，并收紧为 `has_bots()` 而非只看开关。

### 无凭证环境下**无法**验证的项（需要你这边跑）

1. 真实消息在 NapCat / TG 两侧的送达效果（含图片 + 长文的 caption 分条、翻译分隔线渲染）
2. `DEBUG_LOG_QQ_PAYLOAD=1` 下 NapCat payload 确实不含 `_role`（单测已断言 `strip_internal_keys`，但未过真实 NapCat）
3. Gemini 模型级联（见上）
4. 连续跑 ≥11 轮的稳定性与状态摘要
5. watchdog 热重载后新增账号的凭证补加载

## 验收标准

- [ ] `python tests/test_config_load.py` 全绿
- [ ] `python tests/test_units.py` 全绿
- [ ] `python tools/test_models.py` 至少一个模型可用，且输出含 `finishReason`
- [ ] `python main.py` 启动健康检查全绿，连续跑满 ≥ 11 轮（触发一次状态摘要），摘要在终端完整显示
- [ ] 一条含图片 + 长日文正文的真实消息在 NapCat 与 TG 两侧均完整送达（含翻译）
- [ ] `git diff` 中不含任何 `data/`、`logs/`、`.env` 内容
