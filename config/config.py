# ============================================================
# config.py — 配置 facade：加载 config.json → 校验 → 暴露变量
# ============================================================
# 所有现有 import（from config.config import X）无需任何修改。
# 敏感值在 config.json 中用 $ENV:VAR_NAME 占位符，启动时替换。
# ============================================================
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


def _resolve_env(value):
    """递归遍历 dict/list/str，把 "$ENV:KEY" 替换为 os.getenv("KEY", "")。
       非字符串值原样返回。"""
    if isinstance(value, str) and value.startswith("$ENV:"):
        return _os.getenv(value[5:], "")
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def _build_paths(cfg: dict) -> dict:
    """将 JSON 中的相对路径字符串拼接为项目根目录下的绝对路径。"""
    _path_keys = {
        "cred_dir", "time_record_dir", "sent_ids_dir",
        "error_log_file", "response_log_file",
    }
    for key in _path_keys:
        if key in cfg:
            cfg[key] = str(_BASE_DIR / cfg[key])
    return cfg


# JSON key → Python 变量名映射表
_KEY_TO_VAR: dict[str, str] = {
    "enable_napcat_qq":             "ENABLE_NAPCAT_QQ",
    "enable_qq_official_bot":       "ENABLE_QQ_OFFICIAL_BOT",
    "qq_bot_api":                   "QQ_BOT_API",
    "qq_user_agent":                "QQ_USER_AGENT",
    "qq_official_token_url":        "QQ_OFFICIAL_TOKEN_URL",
    "qq_official_api_base":         "QQ_OFFICIAL_API_BASE",
    "qq_official_min_interval":     "QQ_OFFICIAL_MIN_INTERVAL",
    "qq_official_timeout":          "QQ_OFFICIAL_TIMEOUT",
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
    "bilibili_full_cookie":         "BILIBILI_FULL_COOKIE",
    "bilibili_bili_jct":            "BILIBILI_BILI_JCT",
    "bilibili_post_api":            "BILIBILI_POST_API",
    "bilibili_min_interval":        "BILIBILI_MIN_INTERVAL",
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
# 核心加载
# ================================================================

def _load_config() -> dict:
    """读取 config.json → 校验 Schema → 解析 $ENV → 构建路径。
       任何失败都抛异常，由调用方决定是否 exit。"""
    # 1. 读 JSONC
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

    # 2. 校验 Schema
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

    # 3. 解析 $ENV 占位符
    resolved = _resolve_env(raw)

    # 4. 构建绝对路径
    resolved = _build_paths(resolved)

    return resolved


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
        global ENABLE_NAPCAT_QQ, ENABLE_QQ_OFFICIAL_BOT, DEBUG_LOG_QQ_PAYLOAD
        ENABLE_NAPCAT_QQ       = _env_bool("ENABLE_NAPCAT_QQ",       ENABLE_NAPCAT_QQ)
        ENABLE_QQ_OFFICIAL_BOT = _env_bool("ENABLE_QQ_OFFICIAL_BOT", ENABLE_QQ_OFFICIAL_BOT)
        DEBUG_LOG_QQ_PAYLOAD   = _env_bool("DEBUG_LOG_QQ_PAYLOAD",   DEBUG_LOG_QQ_PAYLOAD)

        return True
    except SystemExit:
        # _load_config 在致命错误时调用 sys.exit，我们拦截住
        return False
    except Exception:
        return False


def get(key: str):
    """按 JSON key 读取当前配置值（绕过 import 缓存，始终反映最新值）。
       对于需要热重载的标量值，推荐使用此方法而非模块级 import。"""
    return getattr(_sys.modules[__name__], _KEY_TO_VAR.get(key, key), None)
