# 配置简化：两文件分工清晰 + config.py 内置默认值

## 背景

当前 `.env`（79 行 / ~18 变量）+ `config.json`（284 行 / 30+ 键），三者问题并存：

- **首次部署繁琐**：需要理解 `$ENV:VAR` 跨文件引用机制，填写大量不常用的配置项
- **日常维护模糊**：加成员、换 token 不知道改哪个文件
- **两文件职责不清**：敏感信息分散在两处，`monitor_list` 冗余字段多（`group_type` 重复账号已有的信息）

## 目标

- `config.json` 从 284 行缩减到 ~60 行，只包含用户真正会改的配置
- `.env` 从 79 行缩减到 ~15 行，只包含密钥和凭证
- 删除所有 `$ENV:VAR` 跨文件引用
- 用户不关心的框架级默认值内置在 `config.py`，不出现在配置文件
- 成员列表字段精简（去 `group_type`、去 `post_to_bilibili` 默认值、去冗余前缀）
- 向后兼容至少一个版本

---

## 设计

### 1. 简化后的 `config.json`

删除的内容及理由：

| 删除项 | 理由 |
|---|---|
| 所有 `$ENV:VAR` 占位符 | 密钥由 `config.py` 按命名约定自动从 `.env` 匹配 |
| `qq_official_bots` 数组 + URL/速率配置 | 内置默认值；不启用 QQ 官方 Bot 则不出现 |
| 账号中的 `init_token` / `init_cookie` / `init_refresh_token` | 按 `{ACCOUNT_KEY}_TOKEN` / `_COOKIE` / `_REFRESH_TOKEN` 约定自动匹配 |
| `skip_publish_types` / `media_type_map` | 内置默认值 |
| `backtrack_hours` / `day_start_hour` / `night_start_hour` / `token_refresh_before_seconds` | 内置默认值 |
| 文件路径全部 6 项 + `sent_ids_max` | 内置默认值 |
| `http_semaphore_limit` / `debug_log_response` / `debug_log_qq_payload` / `qq_user_agent` | 内置默认值 |
| 健康追踪 3 项 | 内置默认值 |
| 旧字段：`enable_*` 系列、`qq_bot_api`、`bilibili_*` | 键名统一 + 内置默认值 |

保留的内容（用户会手动改的）：

```json5
{
  // ── 推送通道 ──
  "channels": {
    "napcat": true,
    "tg": true
  },
  "napcat_api": "http://127.0.0.1:3000/send_group_msg",

  // ── 账号池 ──
  // 密钥自动从 .env 按命名约定匹配，无需在此写 $ENV:VAR
  "accounts": {
    "nogizaka_main":  { "group": "nogizaka46", "auth": "mobile" },
    "nogizaka_shared": { "group": "nogizaka46" },
    "hinata_shared":   { "group": "hinatazaka46" },
    "yodel_grad":      { "group": "hinatazaka46", "app_tag": "yodel", "api_base": "https://api.service.yodel-app.com", "web_origin": "https://service.yodel-app.com" }
  },

  // ── 监控成员 ──（字段名精简，group 从账号推导）
  "monitor": [
    { "id": "55", "name": "冨里奈央",    "account": "nogizaka_main",  "groups": [533072575], "tg": "-1004219007326" },
    { "id": "30", "name": "賀喜遥香",    "account": "nogizaka_shared", "groups": [752269366] },
    { "id": "34", "name": "金村美玖",    "account": "hinata_shared",   "groups": [752269366] },
    { "id": "36", "name": "小坂菜绪",    "account": "hinata_shared",   "groups": [752269366] },
    { "id": "84", "name": "大野愛実",    "account": "hinata_shared",   "groups": [752269366] },
    { "id": "88", "name": "佐藤優羽",    "account": "hinata_shared",   "groups": [752269366] },
    { "id": "77", "name": "松田好花",    "account": "yodel_grad",      "groups": [752269366] },
    { "id": "81", "name": "松田好花 Staff", "account": "yodel_grad",   "groups": [752269366] },
    { "id": "47", "name": "丹生明里",    "account": "yodel_grad",      "groups": [752269366] }
  ],

  // ── 推送配置 ──
  "day_interval": [120, 180],
  "night_interval": [1500, 1800],
  "sleep_hours": [2, 7],
  "alert_cooldown": 3600,

  // ── 可选覆盖 ──（不写则用内置默认值）
  "qq_send_interval": 1.5,
  "translate": true,
  "gemini_models": [
    { "name": "gemini-3.6-flash",       "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent",       "rpm": 10 },
    { "name": "gemini-2.5-flash",       "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",       "rpm": 10 },
    { "name": "gemini-3.5-flash-lite",  "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",  "rpm": 15 },
    { "name": "gemini-3.1-flash-lite",  "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent",  "rpm": 15 }
  ],
  "gemini_min_interval": 7.0,
  "translate_timeout": 30
}
```

### 2. 简化后的 `.env`

变化：
- 删通道开关、`QQ_BOT_API` → 进 `config.json`
- QQ 官方 Bot 凭证保留，命名不变
- 账号凭证改用命名约定（`{ACCOUNT_KEY_UPPER}_TOKEN`, `_COOKIE`, `_REFRESH_TOKEN`）
- B站合并为单一 `BILIBILI_COOKIE`

```bash
# ── Gemini 翻译 ──
GEMINI_API_KEY=your_key_here

# ── Telegram Bot ──
TG_BOT_TOKEN=123456:ABC-DEF

# ── 账号凭证 ──
# 命名规则：config.json 中 accounts 的 key 大写 + _TOKEN / _COOKIE / _REFRESH_TOKEN
NOGIZAKA_MAIN_REFRESH_TOKEN=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
NOGIZAKA_MAIN_TOKEN=
NOGIZAKA_SHARED_TOKEN=eyJ...
NOGIZAKA_SHARED_COOKIE=session=xxx
HINATA_SHARED_TOKEN=eyJ...
HINATA_SHARED_COOKIE=session=xxx
YODEL_GRAD_TOKEN=eyJ...
YODEL_GRAD_COOKIE=S5SI=xxx; 4FB852B4CF8A4CFF=yyy

# ── QQ 官方 Bot（可选，不启用则留空）──
QQ_OFFICIAL_BOT1_APP_ID=
QQ_OFFICIAL_BOT1_CLIENT_SECRET=
QQ_OFFICIAL_BOT1_TARGET_OPENID=
QQ_OFFICIAL_BOT2_APP_ID=
QQ_OFFICIAL_BOT2_CLIENT_SECRET=
QQ_OFFICIAL_BOT2_TARGET_OPENID=

# ── B站（可选）──
BILIBILI_COOKIE=SESSDATA=xxx; bili_jct=yyy
```

### 3. `config.py` 内置默认值

启动时加载顺序：内置默认值 → `config.json` 覆盖 → `.env` 覆盖（布尔开关）。

内置默认值清单（与当前值一致，用户不写就不出现在配置文件中）：

| 键 | 默认值 |
|---|---|
| `qq_official_token_url` | `https://bots.qq.com/app/getAppAccessToken` |
| `qq_official_api_base` | `https://api.sgroup.qq.com` |
| `qq_official_min_interval` | `1.2` |
| `qq_official_timeout` | `15` |
| `qq_official_media_max_bytes` | `26214400` |
| `gemini_api_key` | `$ENV:GEMINI_API_KEY` |
| `gemini_models` | 当前 4 模型列表 |
| `gemini_min_interval` | `7.0` |
| `translate_max_length` | `2500` |
| `translate_timeout` | `30` |
| `bilibili_post_api` | `https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create` |
| `bilibili_min_interval` | `3.0` |
| `cred_dir` | `data/web_credentials` |
| `time_record_dir` | `data/time_records` |
| `sent_ids_dir` | `data/sent_ids` |
| `error_log_file` | `logs/error_debug.log` |
| `response_log_file` | `logs/response_debug.log` |
| `sent_ids_max` | `500` |
| `http_semaphore_limit` | `3` |
| `token_refresh_before_seconds` | `300` |
| `backtrack_hours` | `24` |
| `day_start_hour` | `7` |
| `night_start_hour` | `0` |
| `skip_publish_types` | `["birthday"]` |
| `media_type_map` | `{"video":"video","voice":"record","image":"image","picture":"image"}` |
| `debug_log_response` | `true` |
| `debug_log_qq_payload` | `false` |
| `qq_user_agent` | 当前值 |
| `health_summary_interval` | `10` |
| `health_error_buffer` | `50` |
| `health_token_warn_seconds` | `600` |

### 4. 字段名映射（旧 → 新）

| 旧键 | 新键 |
|---|---|
| `enable_napcat_qq` | `channels.napcat` |
| `enable_qq_official_bot` | `channels.qq_official` |
| `enable_tg_bot` | `channels.tg` |
| `tg_bot_token` | `.env` 的 `TG_BOT_TOKEN`（不再出现在 json） |
| `qq_bot_api` | `napcat_api` |
| `monitor_list` | `monitor` |
| `m_id` / `m_name` / `account_id` / `target_groups` / `tg_chat_id` | `id` / `name` / `account` / `groups` / `tg` |
| `sleep_start_hour` + `sleep_end_hour` | `sleep_hours: [start, end]` |
| `bilibili_full_cookie` + `bilibili_bili_jct` | `.env` 的 `BILIBILI_COOKIE`（合并） |
| `alert_cooldown_seconds` | `alert_cooldown` |
| `day_start_hour` / `night_start_hour` / `token_refresh_before_seconds` | 内置默认值（config.json 可选覆盖） |

删除的字段：`post_to_bilibili`（默认 false，不写）、`bilibili_cookie`（成员专属，当前未使用）、`group_type`（从 account 推导）。

### 5. 账户凭证自动匹配规则

`config.py` 在加载时对每个 account key 执行：

```
key = "nogizaka_main"  →  大写: "NOGIZAKA_MAIN"
                          找 .env 的 NOGIZAKA_MAIN_TOKEN
                          找 .env 的 NOGIZAKA_MAIN_COOKIE（web 专用）
                          找 .env 的 NOGIZAKA_MAIN_REFRESH_TOKEN（mobile 专用）
                          找不到则 fallback 到 NOGIZAKA_REFRESH_TOKEN（全局）
```

`.env` 中没有对应变量的，启动时报错、但继续运行（部分账号不可用）。

### 6. `config.schema.json`

重新生成，仅校验简化后的 `config.json` 顶层字段。旧格式的键不在 schema 中，加载时由 `config.py` 做兼容转换。

### 7. 向后兼容

- `config.py` 检测旧格式键（`monitor_list`, `enable_napcat_qq` 顶层等），自动转换为新格式内部表示
- 启动时打印 `⚠️ 检测到旧格式 config.json，建议迁移（详见 docs/...）` — 不阻止运行
- 兼容期为一个主版本号

### 8. 不做什么

- 不改动已持久化的数据文件（`data/web_credentials/`, `data/time_records/`, `data/sent_ids/`）
- 不改动推送语义和业务逻辑
- 不提供自动迁移脚本（手动改 9 个成员即可，影响太小的不值得写脚本）
- 不引入新的配置格式（YAML、TOML 等）

---

## 变更文件清单

| 文件 | 变更 |
|---|---|
| `config/config.json` | **重写** — 284 → ~60 行 |
| `config/config.schema.json` | **重写** — 校验简化后的键 |
| `config/config.py` | **重构** — 内置默认值 → 读 json → 读 .env 三层合并；新旧键名映射；凭证自动匹配 |
| `.env.example` | **重写** — 79 → ~20 行 |
| `src/app.py` | **小改** — 引用方式从 `cfg.ENABLE_NAPCAT_QQ` 等改为 `cfg.CHANNELS["napcat"]` 或兼容层 |
| `src/notifier.py` | **小改** — 同上，通道检测适配新键名 |
| `config/credentials.py` | **小改** — 凭证读取适配新方式 |
| `README.md` | **更新** — 新配置说明 |
