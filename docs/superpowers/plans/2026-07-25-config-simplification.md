# 配置简化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `.env` (79行) + `config.json` (284行) 精简为 `.env` (~20行纯密钥) + `config.json` (~60行用户配置) + `config.py` 内置默认值的三层架构，删除所有 `$ENV:VAR` 跨文件引用，向后兼容旧格式。

**Architecture:** 加载顺序：`config.py` 内置默认值 → `config.json` 覆盖 → `.env` 环境变量覆盖。`config.py` 暴露的模块级变量名不变（`ENABLE_NAPCAT_QQ` 等），所有消费者无需改 import。旧格式自动检测并转换。

**Tech Stack:** Python 3.10+ stdlib (`json`, `os`, `pathlib`)，已有依赖 `json5`、`jsonschema`、`python-dotenv`。

## Global Constraints

- 所有 `from config.config import VAR` 的消费者模块无需改 import 行
- 不改变已持久化数据文件的格式和位置（`data/web_credentials/`、`data/time_records/`、`data/sent_ids/`）
- 不改变推送语义和业务逻辑
- 不引入新第三方依赖
- 向后兼容旧格式 `config.json` 至少一个版本（警告但不阻止运行）
- Schema 校验仅针对新格式；旧格式打印警告后自动转换

---

## File Structure

| 文件 | 职责 |
|---|---|
| `config/config.py` | **重构** — 新增 `_DEFAULTS` 字典、`_normalize_config()` 旧格式转换、`_match_account_credentials()` 自动匹配；删除 `_resolve_env()` |
| `config/config.json` | **重写** — 新格式 ~60 行 |
| `config/config.schema.json` | **重写** — 校验新格式字段 |
| `.env.example` | **重写** — ~20 行纯密钥 |
| `src/app.py` | **小改** — channel 检测适配（如需要） |
| `src/notifier.py` | **小改** — channel 检测适配（如需要） |
| `config/credentials.py` | **小改** — 凭证读取适配新方式 |
| `src/platforms/bilibili.py` | **小改** — `BILIBILI_COOKIE` 合并读取 |
| `README.md` | **更新** — 新配置说明 |

---

### Task 1: `config/config.py` — 核心重构（三层加载 + 旧格式兼容 + 凭证自动匹配）

**Files:**
- Modify: `config/config.py`

**Interfaces:**
- Produces (module-level variables — NAMES UNCHANGED):
  - `ENABLE_NAPCAT_QQ: bool` — from `channels.napcat` or old `enable_napcat_qq`
  - `ENABLE_QQ_OFFICIAL_BOT: bool` — from `channels.qq_official` or old `enable_qq_official_bot`
  - `ENABLE_TG_BOT: bool` — from `channels.tg` or old `enable_tg_bot`
  - `TG_BOT_TOKEN: str` — from `.env` only
  - `QQ_BOT_API: str` — from `napcat_api` or old `qq_bot_api`
  - `MONITOR_LIST: list[dict]` — normalized from `monitor` or old `monitor_list`; each item has: `{account_id, group_type, m_id, m_name, target_groups, tg_chat_id, post_to_bilibili}`
  - `ACCOUNTS: dict` — same structure as before, with credentials injected
  - `SLEEP_START_HOUR: int`, `SLEEP_END_HOUR: int` — from `sleep_hours[0]`/`[1]` or old keys
  - `ALERT_COOLDOWN_SECONDS: int` — from `alert_cooldown` or old `alert_cooldown_seconds`
  - `ENABLE_TRANSLATION: bool` — from `translate` or old `enable_translation`
  - `BILIBILI_FULL_COOKIE: str` — from `.env` `BILIBILI_COOKIE`
  - `BILIBILI_BILI_JCT: str` — extracted from `BILIBILI_COOKIE` (`bili_jct=xxx` part)
  - All other vars (paths, timeouts, intervals, debug flags, health tracker, Gemini, QQ official configs) — same names, from built-in defaults with config.json override
- Consumes: nothing new

- [ ] **Step 1: 添加 `_DEFAULTS` 字典**

在 `_CONFIG_PATH` / `_SCHEMA_PATH` 之后、`_KEY_TO_VAR` 之前，添加内置默认值：

```python
# ── 内置默认值（config.json 可覆盖，.env 可覆盖布尔开关）─────────
_DEFAULTS: dict = {
    # QQ 官方 Bot（不启用则不出现在 config.json 中）
    "qq_official_token_url":    "https://bots.qq.com/app/getAppAccessToken",
    "qq_official_api_base":     "https://api.sgroup.qq.com",
    "qq_official_min_interval": 1.2,
    "qq_official_timeout":      15,
    "qq_official_media_max_bytes": 26214400,
    "qq_official_bots":         [],   # 启用时由 config.py 从 .env 构建
    # Gemini
    "gemini_api_key":           "",   # 从 .env GEMINI_API_KEY
    "gemini_models": [
        {"name": "gemini-3.6-flash",       "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent",       "rpm": 10},
        {"name": "gemini-2.5-flash",       "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",       "rpm": 10},
        {"name": "gemini-3.5-flash-lite",  "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",  "rpm": 15},
        {"name": "gemini-3.1-flash-lite",  "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent",  "rpm": 15},
    ],
    "gemini_min_interval":      7.0,
    "translate_max_length":     2500,
    "translate_timeout":        30,
    # B站
    "bilibili_post_api":        "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create",
    "bilibili_min_interval":    3.0,
    # 文件路径
    "cred_dir":                 "data/web_credentials",
    "time_record_dir":          "data/time_records",
    "sent_ids_dir":             "data/sent_ids",
    "error_log_file":           "logs/error_debug.log",
    "response_log_file":        "logs/response_debug.log",
    "sent_ids_max":             500,
    # 并发 / 反爬
    "http_semaphore_limit":     3,
    "qq_send_interval":         1.5,
    "token_refresh_before_seconds": 300,
    "backtrack_hours":          24,
    "day_start_hour":           7,
    "night_start_hour":         0,
    # 消息过滤
    "skip_publish_types":       ["birthday"],
    "media_type_map":           {"video": "video", "voice": "record", "image": "image", "picture": "image"},
    # 调试
    "debug_log_response":       True,
    "debug_log_qq_payload":     False,
    "qq_user_agent":            "Mozilla/5.0 (Windows NT; Windows NT 10.0; en-US) WindowsPowerShell/5.1",
    # 健康追踪
    "health_summary_interval":  10,
    "health_error_buffer":      50,
    "health_token_warn_seconds": 600,
    # 告警
    "alert_cooldown_seconds":   3600,
    # 通道（config.json 的 channels 映射到旧变量名）
    "enable_napcat_qq":         True,
    "enable_qq_official_bot":   False,
    "enable_tg_bot":            False,
    "tg_bot_token":             "",
}
```

- [ ] **Step 2: 添加 `_normalize_config()` — 旧格式检测与转换**

在 `_apply_config` 之前添加：

```python
def _normalize_config(raw: dict) -> dict:
    """检测旧格式 config.json，转换为新格式内部表示。
    新格式直接返回，旧格式自动转换并打印迁移提示。
    注意：此函数不修改传入的 raw，返回新 dict。"""

    # 检测：旧格式的标志是顶层的 enable_napcat_qq / monitor_list
    is_old = "enable_napcat_qq" in raw or "monitor_list" in raw

    if not is_old:
        # 新格式：展开 channels 到顶层变量
        cfg = dict(raw)
        channels = cfg.pop("channels", {})
        cfg["enable_napcat_qq"]       = channels.get("napcat", True)
        cfg["enable_qq_official_bot"] = channels.get("qq_official", False)
        cfg["enable_tg_bot"]          = channels.get("tg", False)
        cfg["tg_bot_token"]           = _env("TG_BOT_TOKEN", "")

        # napcat_api → qq_bot_api
        if "napcat_api" in cfg:
            cfg["qq_bot_api"] = cfg.pop("napcat_api")

        # sleep_hours → sleep_start_hour / sleep_end_hour
        if "sleep_hours" in cfg:
            sh = cfg.pop("sleep_hours")
            cfg["sleep_start_hour"] = sh[0]
            cfg["sleep_end_hour"]   = sh[1]

        # alert_cooldown → alert_cooldown_seconds
        if "alert_cooldown" in cfg:
            cfg["alert_cooldown_seconds"] = cfg.pop("alert_cooldown")

        # translate → enable_translation
        if "translate" in cfg:
            cfg["enable_translation"] = cfg.pop("translate")

        # monitor → monitor_list (展开 group_type + 保留旧字段名兼容)
        if "monitor" in cfg:
            accounts = cfg.get("accounts", {})
            normalized = []
            for m in cfg["monitor"]:
                acc = accounts.get(m["account"], {})
                normalized.append({
                    "account_id":    m["account"],
                    "group_type":    acc.get("group", ""),
                    "m_id":          str(m["id"]),
                    "m_name":        m["name"],
                    "target_groups": m.get("groups", []),
                    "tg_chat_id":    m.get("tg", ""),
                    "post_to_bilibili": m.get("post_to_bilibili", False),
                })
            cfg["monitor_list"] = normalized
            del cfg["monitor"]

        return cfg

    # 旧格式：保持原样，但打印迁移提示
    _OLD_FORMAT_WARNED = True
    print(
        "⚠️  检测到旧格式 config.json（顶层 enable_*/monitor_list）。\n"
        "   系统将以兼容模式运行，但建议迁移到新格式。\n"
        "   新格式示例见: docs/superpowers/specs/2026-07-25-config-simplification-design.md"
    )
    return dict(raw)
```

- [ ] **Step 3: 修改 `_load_config()` — 三層合併**

修改 `_load_config()` 函数：

```python
def _load_config() -> dict:
    """加载配置：内置默认值 → config.json 覆盖 → .env 补充凭证。

    流程：
    1. 从 _DEFAULTS 拷贝内置默认值
    2. 读 config.json（JSONC，支持注释）
    3. Schema 校验（新格式）
    4. _normalize_config() 展开 + 旧格式兼容
    5. config.json 的值覆盖 _DEFAULTS
    6. _build_paths() 拼接绝对路径
    7. _match_account_credentials() 自动从 .env 注入凭证
    8. _build_qq_official_bots() 构建 Bot 列表（如启用）
    """

    # 1. 从默认值开始
    import copy as _copy
    cfg = _copy.deepcopy(_DEFAULTS)

    # 2. 读 JSONC
    try:
        import json5 as _json5
    except ImportError:
        _sys.exit(
            "❌ 缺少依赖 json5，请执行: pip install json5\n"
            "   json5 用于解析 config.json 中的 JSONC 格式（支持注释）。"
        )

    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = _json5.load(f)
    except FileNotFoundError:
        _sys.exit(f"❌ 找不到配置文件: {_CONFIG_PATH}")
    except Exception as e:
        _sys.exit(f"❌ config.json 解析失败: {e}")

    # 3. Schema 校验
    try:
        import jsonschema as _jsonschema
    except ImportError:
        _sys.exit(
            "❌ 缺少依赖 jsonschema，请执行: pip install jsonschema\n"
            "   jsonschema 用于校验 config.json 的结构正确性。"
        )
    try:
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = _json.load(f)
        _jsonschema.validate(raw, schema)
    except FileNotFoundError:
        _sys.exit(f"❌ 找不到 Schema 文件: {_SCHEMA_PATH}")
    except _jsonschema.ValidationError as e:
        _sys.exit(
            f"❌ config.json 校验失败:\n"
            f"   错误: {e.message}\n"
            f"   位置: {' → '.join(str(p) for p in e.absolute_path) if e.absolute_path else '(根)'}\n"
            f"   请对照 config.schema.json 修正后重试。"
        )

    # 4. 标准化（旧→新转换 + 展开 channels 等）
    normalized = _normalize_config(raw)

    # 5. config.json 的值覆盖默认值（深度合并）
    _deep_merge(cfg, normalized)

    # 6. 构建绝对路径
    cfg = _build_paths(cfg)

    # 7. 从 .env 补充密钥
    cfg["gemini_api_key"] = _env("GEMINI_API_KEY", cfg.get("gemini_api_key", ""))

    # 8. 账号凭证自动匹配
    cfg = _match_account_credentials(cfg)

    # 9. 构建 QQ 官方 Bot 列表
    cfg = _build_qq_official_bots(cfg)

    return cfg
```

- [ ] **Step 4: 添加 `_deep_merge()` 辅助函数**

```python
def _deep_merge(base: dict, override: dict) -> None:
    """将 override 的值深度合并到 base 中（修改 base in-place）。
    仅当 override 中显式提供的键才覆盖；base 中独有的键保留。"""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
```

- [ ] **Step 5: 添加 `_match_account_credentials()`**

```python
def _match_account_credentials(cfg: dict) -> dict:
    """为每个账号自动从 .env 匹配凭证。

    命名约定：
      account key "nogizaka_main" → 大写 "NOGIZAKA_MAIN"
        → NOGIZAKA_MAIN_TOKEN, NOGIZAKA_MAIN_COOKIE, NOGIZAKA_MAIN_REFRESH_TOKEN
      fallback: NOGIZAKA_REFRESH_TOKEN（全局移动端 refresh_token）

    web 账号（默认）需要 TOKEN + COOKIE
    mobile 账号需要 REFRESH_TOKEN（可从此处或从全局 fallback）
    """
    accounts = cfg.get("accounts", {})
    for acc_id, acc in accounts.items():
        prefix = acc_id.upper()

        # Token（web 和 mobile 都可能有 init_token）
        token = _env(f"{prefix}_TOKEN", "")
        cookie = _env(f"{prefix}_COOKIE", "")
        refresh = _env(f"{prefix}_REFRESH_TOKEN", "")

        if token:
            acc["init_token"] = token
        if cookie:
            acc["init_cookie"] = cookie

        if acc.get("auth") == "mobile":
            if refresh:
                acc["init_refresh_token"] = refresh
            else:
                # fallback 到全局
                global_refresh = _env("NOGIZAKA_REFRESH_TOKEN", "")
                if global_refresh:
                    acc["init_refresh_token"] = global_refresh

        # 清理内部键 "auth" → "auth_method"
        if "auth" in acc:
            acc["auth_method"] = acc.pop("auth")

        # 清理内部键 "group" → "group_type"
        if "group" in acc:
            acc["group_type"] = acc.pop("group")

    return cfg
```

- [ ] **Step 6: 添加 `_build_qq_official_bots()`**

```python
def _build_qq_official_bots(cfg: dict) -> dict:
    """如果启用了 QQ 官方 Bot，从 .env 读取 Bot 凭证构建 bots 列表。"""
    if not cfg.get("enable_qq_official_bot"):
        cfg["qq_official_bots"] = []
        return cfg

    bots = []
    for i in (1, 2):
        app_id = _env(f"QQ_OFFICIAL_BOT{i}_APP_ID", "")
        if not app_id:
            continue
        bots.append({
            "name":          f"bot_{i}",
            "app_id":        app_id,
            "client_secret": _env(f"QQ_OFFICIAL_BOT{i}_CLIENT_SECRET", ""),
            "target_openid": _env(f"QQ_OFFICIAL_BOT{i}_TARGET_OPENID", ""),
        })

    cfg["qq_official_bots"] = bots
    return cfg
```

- [ ] **Step 7: 删除 `_resolve_env()` 函数**

`_resolve_env` 不再需要（没有 `$ENV:VAR` 占位符了）。删除整个函数（原第 40-49 行）。

- [ ] **Step 8: 处理 B站 Cookie 合并**

在 `_match_account_credentials()` 之后，添加 B站 cookie 处理：

实际上，`_load_config` 中已经为 Gemini key 做了 .env 读取。B站也应该同样处理：

在 `_load_config` 末尾：
```python
    # B站统一 cookie
    cfg["bilibili_full_cookie"] = _env("BILIBILI_COOKIE", "")
    # 尝试从 cookie 中提取 bili_jct
    import re as _re
    _jct_match = _re.search(r"bili_jct=([^;]+)", cfg["bilibili_full_cookie"])
    cfg["bilibili_bili_jct"] = _jct_match.group(1) if _jct_match else _env("BILIBILI_BILI_JCT", "")
```

- [ ] **Step 9: 更新 `_KEY_TO_VAR` 和 `_CONTAINER_KEYS`**

`_KEY_TO_VAR` 保持不变（因为内部 normalize 后，`_apply_config` 看到的仍是旧键名）。

`_CONTAINER_KEYS` 中的 `"monitor_list"` 保持不动。

- [ ] **Step 10: 更新 `reload()` 函数**

reload() 需要重新做 account credential matching 和 QQ bot building：

在 `_apply_config(new_cfg)` 之后：
```python
        # 重新匹配凭证（.env 可能也变了）
        _match_account_credentials(new_cfg)
        _build_qq_official_bots(new_cfg)
        _apply_config(new_cfg)
```

但这样 `_apply_config` 被调了两次。更好的做法是让 `reload()` 调完整的 `_load_config()` 流程。实际上当前的 `reload()` 已经在调 `_load_config()` → `_apply_config()`，而 `_load_config()` 我们已经修改为包含 credential matching。所以 reload 不需要额外改动，只需要保留 env override 部分。

简化：`reload()` 调 `_load_config()`（已包含所有逻辑）→ `_apply_config()` → env override。不变。

- [ ] **Step 11: 验证**

```bash
python -c "
import config.config as cfg
print('ENABLE_NAPCAT_QQ:', cfg.ENABLE_NAPCAT_QQ)
print('ENABLE_TG_BOT:', cfg.ENABLE_TG_BOT)
print('TG_BOT_TOKEN:', cfg.TG_BOT_TOKEN[:10] if cfg.TG_BOT_TOKEN else 'N/A')
print('QQ_BOT_API:', cfg.QQ_BOT_API)
print('MONITOR_LIST length:', len(cfg.MONITOR_LIST))
print('First member:', cfg.MONITOR_LIST[0] if cfg.MONITOR_LIST else 'N/A')
print('ACCOUNTS keys:', list(cfg.ACCOUNTS.keys()))
print('SLEEP_START_HOUR:', cfg.SLEEP_START_HOUR, 'SLEEP_END_HOUR:', cfg.SLEEP_END_HOUR)
print('ALERT_COOLDOWN_SECONDS:', cfg.ALERT_COOLDOWN_SECONDS)
print('GEMINI_MODELS count:', len(cfg.GEMINI_MODELS))
print('BILIBILI_FULL_COOKIE:', cfg.BILIBILI_FULL_COOKIE[:20] if cfg.BILIBILI_FULL_COOKIE else 'N/A')
print('OK - all config variables accessible')
"
```

Expected: 所有变量正常输出，无报错。如果当前 config.json 仍是旧格式，应看到迁移警告。

- [ ] **Step 12: Commit**

```bash
git add config/config.py
git commit -m "refactor: three-layer config with built-in defaults, old-format compat, auto-credential matching"
```

---

### Task 2: 重写 `config/config.json` + `config/config.schema.json`

**Files:**
- Modify: `config/config.json` — 重写为新格式
- Modify: `config/config.schema.json` — 重写为新格式

**Interfaces:**
- Produces: `config.json` 的新格式（由 Task 1 的 `_load_config()` 解析）
- Consumes: Task 1 的 `_normalize_config()`、`_DEFAULTS`

- [ ] **Step 1: 备份当前 config.json**

```bash
cp config/config.json config/config.json.old
```

- [ ] **Step 2: 写新的 `config/config.json`**

内容使用 spec 中定义的新格式（spec 第 42-90 行）。注意保留你当前实际使用的值（成员列表、账号、通道开关等）。

新文件内容（与你当前的 config 等价）：

```json5
{
  // ── 推送通道 ──
  "channels": {
    "napcat": true,
    "tg": true
  },
  "napcat_api": "http://127.0.0.1:3000/send_group_msg",

  // ── 账号池 ──
  "accounts": {
    "nogizaka_main":  { "group": "nogizaka46", "auth": "mobile" },
    "nogizaka_shared": { "group": "nogizaka46" },
    "hinata_shared":   { "group": "hinatazaka46" },
    "yodel_grad":      { "group": "hinatazaka46", "app_tag": "yodel", "api_base": "https://api.service.yodel-app.com", "web_origin": "https://service.yodel-app.com" }
  },

  // ── 监控成员 ──
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

- [ ] **Step 3: 写新的 `config/config.schema.json`**

新 schema 只校验新格式的顶层字段：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "坂道联合监控系统配置（精简版）",
  "type": "object",
  "required": ["accounts", "monitor", "day_interval", "night_interval", "sleep_hours"],
  "properties": {
    "channels": {
      "type": "object",
      "properties": {
        "napcat":       { "type": "boolean", "default": true },
        "tg":           { "type": "boolean", "default": false },
        "qq_official":  { "type": "boolean", "default": false }
      }
    },
    "napcat_api": { "type": "string" },
    "accounts": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "required": ["group"],
        "properties": {
          "group":       { "type": "string", "enum": ["nogizaka46", "hinatazaka46", "sakurazaka46", "yodel"] },
          "auth":        { "type": "string", "enum": ["web", "mobile"], "default": "web" },
          "app_tag":     { "type": "string" },
          "api_base":    { "type": "string", "format": "uri" },
          "web_origin":  { "type": "string", "format": "uri" }
        }
      }
    },
    "monitor": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["id", "name", "account", "groups"],
        "properties": {
          "id":              { "type": "string" },
          "name":            { "type": "string" },
          "account":         { "type": "string" },
          "groups":          { "type": "array", "minItems": 1, "items": { "type": "integer" } },
          "tg":              { "type": "string" },
          "post_to_bilibili": { "type": "boolean", "default": false }
        }
      }
    },
    "day_interval":     { "type": "array", "minItems": 2, "maxItems": 2, "items": { "type": "integer", "minimum": 1 } },
    "night_interval":   { "type": "array", "minItems": 2, "maxItems": 2, "items": { "type": "integer", "minimum": 1 } },
    "sleep_hours":      { "type": "array", "minItems": 2, "maxItems": 2, "items": { "type": "integer", "minimum": 0, "maximum": 23 } },
    "alert_cooldown":   { "type": "integer", "minimum": 0 },
    "qq_send_interval": { "type": "number", "minimum": 0 },
    "translate":        { "type": "boolean" },
    "gemini_models":    {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "url", "rpm"],
        "properties": {
          "name": { "type": "string" },
          "url":  { "type": "string", "format": "uri" },
          "rpm":  { "type": "integer", "minimum": 1 }
        }
      }
    },
    "gemini_min_interval": { "type": "number", "minimum": 0 },
    "translate_timeout":   { "type": "integer", "minimum": 1 }
  }
}
```

- [ ] **Step 4: 验证**

```bash
python -c "
import config.config as cfg
print('MONITOR_LIST[0]:', cfg.MONITOR_LIST[0])
print('ACCOUNTS nogizaka_main:', cfg.ACCOUNTS.get('nogizaka_main', {}))
print('ENABLE_NAPCAT_QQ:', cfg.ENABLE_NAPCAT_QQ)
print('SLEEP:', cfg.SLEEP_START_HOUR, '-', cfg.SLEEP_END_HOUR)
print('ALERT_COOLDOWN_SECONDS:', cfg.ALERT_COOLDOWN_SECONDS)
print('OK')
"
```

Expected: 输出当前配置的真实值。

- [ ] **Step 5: Commit**

```bash
git add config/config.json config/config.schema.json
git commit -m "refactor: simplify config.json (284→60 lines) and schema for new format"
```

---

### Task 3: 重写 `.env.example`

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: 写新的 `.env.example`**

```bash
# ============================================================
# .env.example — 复制为 .env 并填入真实值
# 仅包含密钥和凭证；所有非敏感配置在 config/config.json
# ============================================================

# ── Gemini 翻译 ──
GEMINI_API_KEY=your_key_here

# ── Telegram Bot ──
TG_BOT_TOKEN=123456:ABC-DEF

# ── 账号凭证 ──
# 命名规则：config.json 中 accounts 的 key 大写 + _TOKEN / _COOKIE / _REFRESH_TOKEN
# web 账号（默认）需要 TOKEN + COOKIE
# mobile 账号（"auth": "mobile"）需要 REFRESH_TOKEN
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

# ── B 站（可选）──
BILIBILI_COOKIE=SESSDATA=xxx; bili_jct=yyy
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "refactor: simplify .env.example (79→24 lines) — keys only"
```

---

### Task 4: 适配消费者模块

**Files:**
- Modify: `config/credentials.py` — `load_all_accounts()` 适配新 accounts 结构
- Modify: `src/platforms/bilibili.py` — B站 cookie 读取适配（`BILIBILI_COOKIE` 合并）

**Interfaces:**
- Consumes: `config.config.ACCOUNTS`（结构不变，内部已由 `_match_account_credentials` 填充）、`config.config.BILIBILI_FULL_COOKIE`、`config.config.BILIBILI_BILI_JCT`
- Produces: nothing new

- [ ] **Step 1: 检查 `credentials.py` 是否需要改**

`load_all_accounts()` 访问 `ACCOUNTS.items()`，期望每个 account 有 `init_token`、`init_cookie` 或 `init_refresh_token`。这些字段现在由 `_match_account_credentials()` 自动注入，所以不需要改。

但需要确认 `accounts` 在新旧格式中的结构一致性。写一个快速验证：

```bash
python -c "
from config.config import ACCOUNTS
for k, v in ACCOUNTS.items():
    print(f'{k}: group_type={v.get(\"group_type\")}, auth_method={v.get(\"auth_method\", \"web\")}, has_init_token={bool(v.get(\"init_token\"))}, has_init_cookie={bool(v.get(\"init_cookie\"))}, has_init_refresh_token={bool(v.get(\"init_refresh_token\"))}')"
```

- [ ] **Step 2: 检查 `bilibili.py` 是否需要改**

`bilibili.py` 读取 `cfg.BILIBILI_FULL_COOKIE` 和 `cfg.BILIBILI_BILI_JCT`。这些变量名不变，值由 `_load_config` 从 `.env` 的 `BILIBILI_COOKIE` 自动提取。不需要改。

- [ ] **Step 3: 检查 `app.py` / `notifier.py`**

这些文件通过 `cfg.ENABLE_NAPCAT_QQ`、`cfg.ENABLE_TG_BOT` 等访问通道开关。变量名不变，由 `_normalize_config` 从 `channels` 自动映射。不需要改。

- [ ] **Step 4: 验证所有导入**

```bash
python -c "
import src.app
import src.fetcher
import src.notifier
import src.platforms.bilibili
import config.credentials
print('All modules import OK with new config')
"
```

- [ ] **Step 5: Commit（如有改动）**

如果没有任何代码改动（消费者完全兼容），跳过 commit。如有小的适配，执行：

```bash
git add -A
git commit -m "chore: adapt consumers to new config format"
```

---

### Task 5: 端到端验证 + README 更新

**Files:**
- Modify: `README.md` — 新配置说明
- Modify: none (validation only)

- [ ] **Step 1: 完整 import 链验证**

```bash
python -c "
import config.config as cfg
# 验证所有关键变量可访问
assert hasattr(cfg, 'ENABLE_NAPCAT_QQ')
assert hasattr(cfg, 'ENABLE_TG_BOT')
assert hasattr(cfg, 'TG_BOT_TOKEN')
assert hasattr(cfg, 'MONITOR_LIST')
assert hasattr(cfg, 'ACCOUNTS')
assert hasattr(cfg, 'QQ_OFFICIAL_BOTS')
assert hasattr(cfg, 'GEMINI_MODELS')
assert hasattr(cfg, 'HEALTH_SUMMARY_INTERVAL')
assert hasattr(cfg, 'BILIBILI_FULL_COOKIE')
assert hasattr(cfg, 'BILIBILI_BILI_JCT')
assert hasattr(cfg, 'ALERT_COOLDOWN_SECONDS')
# 验证 MONITOR_LIST 结构
m = cfg.MONITOR_LIST[0]
assert 'account_id' in m
assert 'group_type' in m
assert 'm_id' in m
assert 'm_name' in m
assert 'target_groups' in m
print('All assertions passed')
"
```

- [ ] **Step 2: 热重载测试**

```bash
python -c "
import config.config as cfg
print('ENABLE_NAPCAT_QQ before reload:', cfg.ENABLE_NAPCAT_QQ)
result = cfg.reload()
print('Reload result:', result)
print('ENABLE_NAPCAT_QQ after reload:', cfg.ENABLE_NAPCAT_QQ)
print('OK')
"
```

- [ ] **Step 3: 旧格式兼容测试**

将当前 config.json 临时替换为备份的旧格式，验证警告提示和正常加载：

```bash
cp config/config.json config/config.json.new
cp config/config.json.old config/config.json
python -c "import config.config as cfg; print('MONITOR_LIST:', len(cfg.MONITOR_LIST)); print('ENABLE_NAPCAT_QQ:', cfg.ENABLE_NAPCAT_QQ)"
# Expected: 打印迁移警告 + 正常输出
cp config/config.json.new config/config.json
```

- [ ] **Step 4: 更新 `README.md`**

重写"配置参考"部分，说明新格式的两文件分工：

在 README.md 中找到"配置参考"部分，替换为：

```markdown
## 配置

项目使用两个配置文件，职责分明：

| 文件 | 内容 | 示例 |
|---|---|---|
| `.env` | **密钥和凭证**（敏感，不提交 Git） | API Key、Token、Cookie、Bot 密钥 |
| `config/config.json` | **用户配置**（非敏感，可提交） | 成员列表、账号结构、轮询间隔、通道开关 |

### .env — 密钥

```bash
GEMINI_API_KEY=xxx          # Gemini 翻译
TG_BOT_TOKEN=xxx            # Telegram Bot
NOGIZAKA_MAIN_TOKEN=xxx     # 账号凭证（命名规则：ACCOUNT_KEY大写_TOKEN/COOKIE/REFRESH_TOKEN）
# ...
```

完整模板见 `.env.example`（~20 行）。

### config.json — 用户配置

精简后的配置文件 ~60 行，只包含你会手动改的项：

- **channels** — 推送通道开关（`napcat` / `tg` / `qq_official`）
- **accounts** — 账号池（只需定义 `group` 和 `auth`，密钥自动从 `.env` 匹配）
- **monitor** — 监控成员列表（`id` / `name` / `account` / `groups`）
- **推送参数** — 轮询间隔、休眠时段、告警冷却
- **可选覆盖** — Gemini 模型列表、翻译开关等

未写在 config.json 中的配置项使用内置默认值（见 `config/config.py` 的 `_DEFAULTS`），一般无需修改。
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: update README for simplified config format"
```

如果没有其他改动：

```bash
git add -A
git commit -m "chore: end-to-end validation, all modules compatible with new config"
```
