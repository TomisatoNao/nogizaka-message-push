# ============================================================
# config.py — 配置 facade：三层加载（内置默认 → config.json → .env）
# ============================================================
# 所有现有 import（from config.config import X）无需任何修改。
# 敏感值不再使用 $ENV:VAR 占位符 — 账号凭证按命名约定自动从 .env 匹配。
# ============================================================
import copy as _copy
import json as _json
import os as _os
import sys as _sys
from pathlib import Path as _Path

# ── 加载 .env（如已安装 python-dotenv）────────────────────────
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# ── 路径常量（运行时推导，不放入 JSON）─────────────────────────
_BASE_DIR = _Path(__file__).resolve().parent.parent
_CONFIG_PATH = _Path(__file__).resolve().parent / "config.json"
_SCHEMA_PATH = _Path(__file__).resolve().parent / "config.schema.json"


# ── 内置默认值（config.json 可覆盖，.env 可覆盖布尔开关）─────────
_DEFAULTS: dict = {
    # QQ 官方 Bot
    "qq_official_token_url":    "https://bots.qq.com/app/getAppAccessToken",
    "qq_official_api_base":     "https://api.sgroup.qq.com",
    "qq_official_min_interval": 1.2,
    "qq_official_timeout":      15,
    "qq_official_media_timeout": 60,   # 下载/上传媒体的独立超时（25MB 视频跑不进 15s）
    "qq_official_media_max_bytes": 26214400,
    "qq_official_bots":         [],
    # Gemini
    "gemini_api_key":           "",
    # 注：限速是全局串行的 gemini_min_interval，模型级 rpm 从未生效，已移除
    "gemini_models": [
        {"name": "gemini-3.6-flash",       "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"},
        {"name": "gemini-2.5-flash",       "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"},
        {"name": "gemini-3.5-flash-lite",  "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"},
        {"name": "gemini-3.1-flash-lite",  "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"},
    ],
    "gemini_min_interval":      7.0,
    "translate_max_length":     2500,
    "translate_timeout":        30,
    # 文件路径
    "cred_dir":                 "data/web_credentials",
    "time_record_dir":          "data/time_records",
    "sent_ids_dir":             "data/sent_ids",
    "error_log_file":           "logs/error_debug.log",
    "response_log_file":        "logs/response_debug.log",
    "sent_ids_max":             500,
    # 账号与成员（必须由 config.json 提供，这里仅为缺失时的安全兜底）
    "accounts":                 {},
    "monitor_list":             [],
    # 并发 / 反爬
    "http_semaphore_limit":     3,
    "qq_send_interval":         1.5,
    "token_refresh_before_seconds": 300,
    "backtrack_hours":          24,
    # 轮询节奏（config.json 的 day_interval / night_interval / sleep_hours 可覆盖）
    "day_start_hour":           7,
    "night_start_hour":         0,
    "sleep_start_hour":         2,
    "sleep_end_hour":           7,
    "day_interval":             [120, 180],
    "night_interval":           [1500, 1800],
    "enable_translation":       True,
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
    # 网页管理端（config.json 的 web_admin 可覆盖；token 在 .env 的 WEB_ADMIN_TOKEN）
    "web_admin_enabled":        False,
    "web_admin_host":           "127.0.0.1",
    "web_admin_port":           8787,
    # 消息归档（config.json 的 archive 可覆盖）
    "archive_enabled":          False,
    "archive_dir":              "data/archive",
    "archive_media":            True,
    # 通道（默认值，config.json 的 channels / napcat_api 可覆盖）
    "qq_bot_api":               "http://127.0.0.1:3000/send_group_msg",
    "enable_napcat_qq":         True,
    "enable_qq_official_bot":   False,
    "enable_tg_bot":            False,
    "tg_bot_token":             "",
}


# ================================================================
# 内部工具
# ================================================================

def _env(key: str, default: str = "") -> str:
    """读取环境变量，不存在时返回 default。"""
    return _os.getenv(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    """读取布尔环境变量，支持 1/true/yes/on 和 0/false/no/off。"""
    raw = _env(key, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _deep_merge(base: dict, override: dict) -> None:
    """深度合并 override 到 base（in-place）。"""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


def _normalize_config(raw: dict) -> dict:
    """检测旧格式 config.json，转换为新格式内部表示。"""
    is_old = "enable_napcat_qq" in raw or "monitor_list" in raw

    if not is_old:
        cfg = dict(raw)
        channels = cfg.pop("channels", {})
        cfg["enable_napcat_qq"]       = channels.get("napcat", True)
        cfg["enable_qq_official_bot"] = channels.get("qq_official", False)
        cfg["enable_tg_bot"]          = channels.get("tg", False)
        # tg_bot_token 由 _load_config 统一从 .env 读取（步骤 7），此处不重复赋值

        if "napcat_api" in cfg:
            cfg["qq_bot_api"] = cfg.pop("napcat_api")

        if "sleep_hours" in cfg:
            sh = cfg.pop("sleep_hours")
            cfg["sleep_start_hour"] = sh[0]
            cfg["sleep_end_hour"]   = sh[1]

        if "alert_cooldown" in cfg:
            cfg["alert_cooldown_seconds"] = cfg.pop("alert_cooldown")

        if "web_admin" in cfg:
            wa = cfg.pop("web_admin")
            if "enabled" in wa:
                cfg["web_admin_enabled"] = wa["enabled"]
            if "host" in wa:
                cfg["web_admin_host"] = wa["host"]
            if "port" in wa:
                cfg["web_admin_port"] = wa["port"]

        if "archive" in cfg:
            ar = cfg.pop("archive")
            if "enabled" in ar:
                cfg["archive_enabled"] = ar["enabled"]
            if "dir" in ar:
                cfg["archive_dir"] = ar["dir"]
            if "media" in ar:
                cfg["archive_media"] = ar["media"]

        if "translate" in cfg:
            cfg["enable_translation"] = cfg.pop("translate")

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
                    "target_groups": m.get("groups") or [],
                    "tg_chat_id":    m.get("tg", ""),
                })
            cfg["monitor_list"] = normalized
            del cfg["monitor"]

        return cfg

    # 旧格式：打印迁移提示，原样返回
    print(
        "⚠️  检测到旧格式 config.json。\n"
        "   系统将以兼容模式运行，建议迁移到新格式。\n"
        "   新格式示例见: docs/superpowers/specs/2026-07-25-config-simplification-design.md"
    )
    return dict(raw)


def _match_account_credentials(cfg: dict) -> dict:
    """为每个账号自动从 .env 匹配凭证。"""
    accounts = cfg.get("accounts", {})
    for acc_id, acc in accounts.items():
        prefix = acc_id.upper()
        # 新命名: {PREFIX}_TOKEN, 旧命名兼容: ACCOUNT_{PREFIX}_TOKEN
        token = _env(f"{prefix}_TOKEN", "") or _env(f"ACCOUNT_{prefix}_TOKEN", "")
        cookie = _env(f"{prefix}_COOKIE", "") or _env(f"ACCOUNT_{prefix}_COOKIE", "")
        refresh = _env(f"{prefix}_REFRESH_TOKEN", "") or _env(f"ACCOUNT_{prefix}_REFRESH_TOKEN", "")

        if token:
            acc["init_token"] = token
        if cookie:
            acc["init_cookie"] = cookie

        if acc.get("auth") == "mobile":
            if refresh:
                acc["init_refresh_token"] = refresh
            else:
                global_refresh = _env("NOGIZAKA_REFRESH_TOKEN", "")
                if global_refresh:
                    acc["init_refresh_token"] = global_refresh

        if "auth" in acc:
            acc["auth_method"] = acc.pop("auth")
        else:
            acc.setdefault("auth_method", "web")
        if "group" in acc:
            acc["group_type"] = acc.pop("group")

    return cfg


def _build_qq_official_bots(cfg: dict) -> dict:
    """构建 QQ 官方 Bot 列表（数量不限）。

    新方式：config.json 的 qq_official_bots 声明 Bot（name / app_id / target_openid），
            client_secret 按 {NAME大写}_CLIENT_SECRET 从 .env 匹配；
            app_id / target_openid 未写在 JSON 时也回退到 {NAME大写}_APP_ID / _TARGET_OPENID。
            （给 Bot 起名 qq_official_bot1 即可复用旧 .env 变量名，无需迁移。）
    旧方式兼容：config.json 未声明时，扫描 .env 的 QQ_OFFICIAL_BOT{1..20}_* 编号槽位。
    """
    if not cfg.get("enable_qq_official_bot"):
        cfg["qq_official_bots"] = []
        return cfg

    declared = cfg.get("qq_official_bots") or []
    bots = []
    if declared:
        for b in declared:
            prefix = str(b["name"]).upper()
            bots.append({
                "name":          b["name"],
                "app_id":        b.get("app_id") or _env(f"{prefix}_APP_ID", ""),
                "client_secret": _env(f"{prefix}_CLIENT_SECRET", ""),
                "target_openid": b.get("target_openid") or _env(f"{prefix}_TARGET_OPENID", ""),
            })
    else:
        for i in range(1, 21):
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


def _build_paths(cfg: dict) -> dict:
    """将 JSON 中的相对路径字符串拼接为项目根目录下的绝对路径。"""
    _path_keys = {
        "cred_dir", "time_record_dir", "sent_ids_dir",
        "error_log_file", "response_log_file", "archive_dir",
    }
    for key in _path_keys:
        if key in cfg:
            cfg[key] = str(_BASE_DIR / cfg[key])
    return cfg


# JSON key → Python 变量名映射表
_KEY_TO_VAR: dict[str, str] = {
    "enable_napcat_qq":             "ENABLE_NAPCAT_QQ",
    "enable_qq_official_bot":       "ENABLE_QQ_OFFICIAL_BOT",
    "enable_tg_bot":               "ENABLE_TG_BOT",
    "tg_bot_token":                "TG_BOT_TOKEN",
    "qq_bot_api":                   "QQ_BOT_API",
    "qq_user_agent":                "QQ_USER_AGENT",
    "qq_official_token_url":        "QQ_OFFICIAL_TOKEN_URL",
    "qq_official_api_base":         "QQ_OFFICIAL_API_BASE",
    "qq_official_min_interval":     "QQ_OFFICIAL_MIN_INTERVAL",
    "qq_official_timeout":          "QQ_OFFICIAL_TIMEOUT",
    "qq_official_media_timeout":    "QQ_OFFICIAL_MEDIA_TIMEOUT",
    "qq_official_media_max_bytes":  "QQ_OFFICIAL_MEDIA_MAX_BYTES",
    "qq_official_bots":             "QQ_OFFICIAL_BOTS",
    "accounts":                     "ACCOUNTS",
    "monitor_list":                 "MONITOR_LIST",
    "enable_translation":           "ENABLE_TRANSLATION",
    "skip_publish_types":           "SKIP_PUBLISH_TYPES",
    "media_type_map":               "MEDIA_TYPE_MAP",
    "day_start_hour":               "DAY_START_HOUR",
    "night_start_hour":             "NIGHT_START_HOUR",
    "sleep_start_hour":             "SLEEP_START_HOUR",
    "sleep_end_hour":               "SLEEP_END_HOUR",
    "day_interval":                 "DAY_INTERVAL",
    "night_interval":               "NIGHT_INTERVAL",
    "backtrack_hours":              "BACKTRACK_HOURS",
    "alert_cooldown_seconds":       "ALERT_COOLDOWN_SECONDS",
    "web_admin_enabled":            "WEB_ADMIN_ENABLED",
    "web_admin_host":               "WEB_ADMIN_HOST",
    "web_admin_port":               "WEB_ADMIN_PORT",
    "archive_enabled":              "ARCHIVE_ENABLED",
    "archive_dir":                  "ARCHIVE_DIR",
    "archive_media":                "ARCHIVE_MEDIA",
    "http_semaphore_limit":         "HTTP_SEMAPHORE_LIMIT",
    "qq_send_interval":             "QQ_SEND_INTERVAL",
    "token_refresh_before_seconds": "TOKEN_REFRESH_BEFORE_SECONDS",
    "cred_dir":                     "CRED_DIR",
    "time_record_dir":              "TIME_RECORD_DIR",
    "sent_ids_dir":                 "SENT_IDS_DIR",
    "error_log_file":               "ERROR_LOG_FILE",
    "response_log_file":            "RESPONSE_LOG_FILE",
    "sent_ids_max":                 "SENT_IDS_MAX",
    "debug_log_response":           "DEBUG_LOG_RESPONSE",
    "debug_log_qq_payload":         "DEBUG_LOG_QQ_PAYLOAD",
    "gemini_api_key":               "GEMINI_API_KEY",
    "gemini_models":                "GEMINI_MODELS",
    "gemini_min_interval":          "GEMINI_MIN_INTERVAL",
    "translate_max_length":         "TRANSLATE_MAX_LENGTH",
    "translate_timeout":            "TRANSLATE_TIMEOUT",
    "health_summary_interval":      "HEALTH_SUMMARY_INTERVAL",
    "health_error_buffer":          "HEALTH_ERROR_BUFFER",
    "health_token_warn_seconds":    "HEALTH_TOKEN_WARN_SECONDS",
}

# 可在热重载时通过 in-place mutation 更新的容器类型 key
# 注意：tuple 不可变（如 day_interval），不在其中
_CONTAINER_KEYS = frozenset({
    "accounts", "monitor_list", "qq_official_bots",
    "gemini_models", "skip_publish_types", "media_type_map",
})

# 需要特殊类型转换的 key（JSON 类型 → Python 类型）
_TYPE_CONVERTERS = {
    "skip_publish_types": set,       # list → set
    "day_interval":       tuple,     # list → tuple
    "night_interval":     tuple,     # list → tuple
}


# ================================================================
# 核心加载：三层合并
#   1. 内置默认值 (_DEFAULTS)
#   2. config.json 覆盖
#   3. .env 补充（密钥、凭证自动匹配）
# ================================================================

def _load_config() -> dict:
    """三层加载配置：默认 → config.json → .env。
       任何失败都抛异常，由调用方决定是否 exit。"""
    # 1. 从内置默认值开始（深拷贝，避免污染 _DEFAULTS）
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

    # 3. 校验 Schema（对新格式配置进行校验）
    #    Schema 现在定义新格式键名（channels / monitor / sleep_hours 等），
    #    因此在校验之后再做规范化（新格式 → 旧格式内部表示）。
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

    # 5. 标准化（新格式 → 旧格式内部表示）并覆盖默认值
    normalized = _normalize_config(raw)
    _deep_merge(cfg, normalized)

    # 6. 构建绝对路径
    cfg = _build_paths(cfg)

    # 7. 从 .env 补充密钥（覆盖 config.json 中的 $ENV: 占位符）
    cfg["gemini_api_key"] = _env("GEMINI_API_KEY", "")
    cfg["tg_bot_token"]   = _env("TG_BOT_TOKEN", "")

    # 8. 账号凭证自动匹配（按命名约定从 .env 读取）
    cfg = _match_account_credentials(cfg)

    # 9. QQ 官方 Bot 构建（从 .env 读取）
    cfg = _build_qq_official_bots(cfg)

    return cfg


def _mutate_container(old, new):
    """将 new 的值原位写入 old 容器（保持引用不变，供热重载使用）。
       支持 dict → dict / list → list / set → set / tuple → list 的原地更新。"""
    if isinstance(old, dict) and isinstance(new, dict):
        old.clear()
        old.update(new)
    elif isinstance(old, list) and isinstance(new, (list, set, tuple)):
        old.clear()
        old.extend(new)
    elif isinstance(old, set) and isinstance(new, (list, set, tuple)):
        old.clear()
        old.update(new)
    elif isinstance(old, list) and isinstance(new, tuple):
        # tuple → list: 清空后扩展（DAY_INTERVAL 存为 list 方便热重载）
        old.clear()
        old.extend(new)


def _apply_config(cfg: dict) -> None:
    """将加载后的配置字典应用到模块级变量。"""
    mod = _sys.modules[__name__]

    for json_key, var_name in _KEY_TO_VAR.items():
        val = cfg.get(json_key)

        # 特殊类型转换
        if json_key in _TYPE_CONVERTERS:
            val = _TYPE_CONVERTERS[json_key](val)

        # 容器类型：尝试原位更新已有变量（支持热重载）
        existing = getattr(mod, var_name, None)
        if json_key in _CONTAINER_KEYS and existing is not None and type(existing).__name__ == type(val).__name__:
            _mutate_container(existing, val)
        else:
            setattr(mod, var_name, val)


# ================================================================
# 首次加载（模块导入时执行）
# ================================================================

_config = _load_config()
_apply_config(_config)


# ================================================================
# 向后兼容：QQ_OFFICIAL_BOTS 旧版环境变量自动迁移
# ================================================================

_old_app_id = _env("QQ_OFFICIAL_APP_ID")
if _old_app_id and not any(b.get("app_id") for b in QQ_OFFICIAL_BOTS):  # type: ignore[attr-defined]
    # noinspection PyUnresolvedReferences
    QQ_OFFICIAL_BOTS.clear()  # type: ignore[attr-defined]
    # noinspection PyUnresolvedReferences
    QQ_OFFICIAL_BOTS.extend([{  # type: ignore[attr-defined]
        "name":          "default",
        "app_id":        _old_app_id,
        "client_secret": _env("QQ_OFFICIAL_CLIENT_SECRET"),
        "target_openid": _env("QQ_OFFICIAL_TARGET_OPENID"),
    }])


# ================================================================
# 环境变量覆盖布尔开关（优先级：env > JSON）
# ================================================================

# enable_napcat_qq / enable_qq_official_bot / debug_log_qq_payload
# 可通过同名环境变量覆盖 JSON 中的值
ENABLE_NAPCAT_QQ       = _env_bool("ENABLE_NAPCAT_QQ",       ENABLE_NAPCAT_QQ)       # type: ignore[has-type]
ENABLE_QQ_OFFICIAL_BOT = _env_bool("ENABLE_QQ_OFFICIAL_BOT", ENABLE_QQ_OFFICIAL_BOT) # type: ignore[has-type]
ENABLE_TG_BOT          = _env_bool("ENABLE_TG_BOT",          ENABLE_TG_BOT)          # type: ignore[has-type]
DEBUG_LOG_QQ_PAYLOAD   = _env_bool("DEBUG_LOG_QQ_PAYLOAD",   DEBUG_LOG_QQ_PAYLOAD)   # type: ignore[has-type]


# ================================================================
# 公开 API
# ================================================================

def reload() -> bool:
    """手动热重载 config.json。
       - 容器类型变量（MONITOR_LIST / ACCOUNTS / GEMINI_MODELS 等）
         通过原地更新实现热重载，所有模块无需重新 import 即可看到变化。
       - 标量类型变量（布尔/整数/浮点/字符串）更新模块级引用，
         但已通过 from config.config import VAR 导入的模块不会自动更新
         （Python 限制），需通过 import config.config 后访问
         config.config.VAR 才能看到新值。
       - 校验失败时保留旧配置并返回 False。"""
    try:
        new_cfg = _load_config()
        _apply_config(new_cfg)

        # 重新应用环境变量覆盖
        global ENABLE_NAPCAT_QQ, ENABLE_QQ_OFFICIAL_BOT, DEBUG_LOG_QQ_PAYLOAD, \
               ENABLE_TG_BOT, TG_BOT_TOKEN
        ENABLE_NAPCAT_QQ       = _env_bool("ENABLE_NAPCAT_QQ",       ENABLE_NAPCAT_QQ)
        ENABLE_QQ_OFFICIAL_BOT = _env_bool("ENABLE_QQ_OFFICIAL_BOT", ENABLE_QQ_OFFICIAL_BOT)
        DEBUG_LOG_QQ_PAYLOAD   = _env_bool("DEBUG_LOG_QQ_PAYLOAD",   DEBUG_LOG_QQ_PAYLOAD)

        # 补回 TG Bot 热重载环境变量覆盖
        ENABLE_TG_BOT  = _env_bool("ENABLE_TG_BOT", ENABLE_TG_BOT)
        TG_BOT_TOKEN   = _env("TG_BOT_TOKEN", TG_BOT_TOKEN)

        return True
    except SystemExit as e:
        # _load_config 在致命错误时调用 sys.exit(message)，拦截并把原因打出来
        # （不能 import src.logger —— 会形成循环依赖，用 print）
        print(f"🚨 配置重载失败（保留旧配置）: {e.code}")
        return False
    except Exception:
        import traceback as _tb
        print(f"🚨 配置重载失败（保留旧配置）:\n{_tb.format_exc()}")
        return False


def get(key: str):
    """按 JSON key 读取当前配置值（绕过 import 缓存，始终反映最新值）。
       对于需要热重载的标量值，推荐使用此方法而非模块级 import。"""
    return getattr(_sys.modules[__name__], _KEY_TO_VAR.get(key, key), None)
