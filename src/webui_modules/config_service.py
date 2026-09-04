"""
src/webui_modules/config_service.py — Web 管理端配置校验、序列化、历史版本与密钥管理服务

将 schema 校验、JSONC 分区序列化、配置版本历史快照、.env 密钥写入以及
账号/官方 Bot 状态分析等业务逻辑从单一庞大的 Web 路由文件中解耦，提升模块化程度与可维护性。
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_SCHEMA_PATH = _BASE_DIR / "config" / "config.schema.json"
DEFAULT_CONFIG_PATH = _BASE_DIR / "config" / "config.json"
DEFAULT_ENV_PATH = _BASE_DIR / ".env"

CONFIG_PATH = DEFAULT_CONFIG_PATH
SCHEMA_PATH = DEFAULT_SCHEMA_PATH
ENV_PATH = DEFAULT_ENV_PATH


def _get_config_path(custom: Path | None = None) -> Path:
    if custom is not None:
        return custom
    return CONFIG_PATH


def _get_schema_path(custom: Path | None = None) -> Path:
    if custom is not None:
        return custom
    return SCHEMA_PATH


def _get_env_path(custom: Path | None = None) -> Path:
    if custom is not None:
        return custom
    return ENV_PATH


# ================================================================
# 校验
# ================================================================

def validate_config(raw: dict, schema_path: Path | None = None) -> list[str]:
    """校验新格式配置对象，返回错误列表（空列表 = 通过）。

    1. config.schema.json 结构校验
    2. 引用完整性：monitor[].account 必须在 accounts 中定义
    3. 同一账号下成员 id 不得重复
    """
    errors: list[str] = []
    schema_file = _get_schema_path(schema_path)

    import jsonschema
    try:
        with open(schema_file, "r", encoding="utf-8") as f:
            schema = json.load(f)
        jsonschema.validate(raw, schema)
    except (OSError, jsonschema.ValidationError) as e:
        if isinstance(e, jsonschema.ValidationError):
            loc = " → ".join(str(p) for p in e.absolute_path) if e.absolute_path else "(根)"
            errors.append(f"结构校验失败 [{loc}]: {e.message}")
        else:
            errors.append(f"读取 Schema 失败: {e}")
        return errors

    accounts = raw.get("accounts", {})
    seen: set[tuple[str, str]] = set()
    for i, m in enumerate(raw.get("monitor", [])):
        label = m.get("name") or f"#{i}"
        if not str(m.get("id", "")).strip():
            errors.append(f"成员 {label} 的 id 为空")
        if not str(m.get("name", "")).strip():
            errors.append(f"成员 #{i} 的 name 为空")
        if m.get("account") not in accounts:
            errors.append(f"成员 {label} 引用了未定义的账号: {m.get('account')!r}")
        key = (str(m.get("id")), str(m.get("account")))
        if key in seen:
            errors.append(f"成员 {label} 重复：同一账号下 id={m.get('id')} 出现多次")
        seen.add(key)

    bot_names: set[str] = set()
    for b in raw.get("qq_official_bots", []):
        name = b.get("name", "")
        if name in bot_names:
            errors.append(f"官方 Bot 名称重复: {name!r}")
        bot_names.add(name)

    # Telegram Bot 名称会映射到 .env 变量；重复或归一化后冲突会导致
    # 两个路由意外共用同一凭证，因此在保存前直接阻止。
    from config.config import tg_token_env_key
    tg_names: set[str] = set()
    tg_env_keys: dict[str, str] = {}
    for b in raw.get("tg_bots", []):
        name = str(b.get("name", "")).strip()
        if not name:
            errors.append("Telegram Bot 名称不能为空")
            continue
        if name in tg_names:
            errors.append(f"Telegram Bot 名称重复: {name!r}")
        tg_names.add(name)
        env_key = tg_token_env_key(name)
        if not env_key:
            errors.append(f"Telegram Bot 名称无法生成有效凭证变量: {name!r}")
            continue
        previous = tg_env_keys.get(env_key)
        if previous is not None and previous != name:
            errors.append(
                f"Telegram Bot 名称映射到同一凭证变量 {env_key}: {previous!r} 与 {name!r}"
            )
        tg_env_keys[env_key] = name

    return errors


# ================================================================
# 序列化：dict → 带分区注释的 JSONC 文本
# ================================================================

_SECTIONS: list[tuple[str, list[str]]] = [
    ("── 推送通道 ──",  ["channels", "napcat_api", "napcat_routes", "tg_bots", "qq_official_bots"]),
    ("── 网页管理 ──",  ["web_admin"]),
    ("── 消息归档 ──",  ["archive"]),
    ("── 每日摘要 ──",  ["daily_summary"]),
    ("── Bot 指令 ──",  ["qq_commands"]),
    ("── 账号系统 ──",  ["auth"]),
    ("── 账号池 ──",    ["accounts"]),
    ("── 监控成员 ──",  ["monitor"]),
    ("── 推送节奏 ──",  ["day_interval", "night_interval", "sleep_hours", "alert_cooldown"]),
]
_OPTIONAL_ORDER = ["qq_send_interval", "translate", "image_tagging", "gemini_models", "gemini_min_interval", "translate_timeout"]
_OPTIONAL_COMMENT = "── 可选覆盖 ──（不写则用内置默认值）"


def _dump(val) -> str:
    return json.dumps(val, ensure_ascii=False)


def _render_value(key: str, val) -> str:
    """按键渲染值：容器类展开为多行（账号/成员/模型每项一行），标量内联。"""
    if key == "accounts" and isinstance(val, dict) and val:
        rows = [f"    {_dump(k)}: {_dump(v)}" for k, v in val.items()]
        return "{\n" + ",\n".join(rows) + "\n  }"
    if key in ("monitor", "gemini_models", "qq_official_bots", "napcat_routes", "tg_bots") \
            and isinstance(val, list) and val:
        rows = [f"    {_dump(item)}" for item in val]
        return "[\n" + ",\n".join(rows) + "\n  ]"
    if key in ("channels", "web_admin", "archive", "daily_summary", "auth", "qq_commands") \
            and isinstance(val, dict) and val:
        rows = [f"    {_dump(k)}: {_dump(v)}" for k, v in val.items()]
        return "{\n" + ",\n".join(rows) + "\n  }"
    return _dump(val)


def serialize_config(raw: dict) -> str:
    """将新格式配置对象序列化为带标准分区注释的 JSONC 文本。"""
    remaining = dict(raw)
    blocks: list[tuple[str | None, str | None]] = []

    def emit_section(comment: str, keys: list[str]) -> None:
        present = [k for k in keys if k in remaining]
        if not present:
            return
        blocks.append((comment, None))
        for k in present:
            blocks.append((None, f"  {_dump(k)}: {_render_value(k, remaining.pop(k))}"))

    for comment, keys in _SECTIONS:
        emit_section(comment, keys)

    tail = [k for k in _OPTIONAL_ORDER if k in remaining]
    tail += sorted(k for k in remaining if k not in _OPTIONAL_ORDER)
    if tail:
        blocks.append((_OPTIONAL_COMMENT, None))
        for k in tail:
            blocks.append((None, f"  {_dump(k)}: {_render_value(k, remaining[k])}"))

    lines = ["{"]
    kv_indexes = [i for i, (_, kv) in enumerate(blocks) if kv is not None]
    last_kv = kv_indexes[-1] if kv_indexes else -1
    first = True
    for i, (comment, kv) in enumerate(blocks):
        if comment is not None:
            if not first:
                lines.append("")
            lines.append(f"  // {comment}")
            first = False
        else:
            lines.append(kv + ("," if i != last_kv else ""))
            first = False
    lines.append("}")
    return "\n".join(lines) + "\n"


# ================================================================
# 配置历史版本快照与持久化
# ================================================================

_HISTORY_KEEP = 10
_HISTORY_NAME_RE = re.compile(r"^config-[0-9-]+\.json$")


def _history_dir(path: Path | None = None) -> Path:
    return _get_config_path(path).parent / "history"


def _snapshot_config(path: Path) -> None:
    """把当前 config.json 存进 history/（保留最近 _HISTORY_KEEP 份）。"""
    if not path.exists():
        return
    hist = _history_dir(path)
    hist.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    shutil.copy2(path, hist / f"config-{stamp}.json")
    for old in sorted(hist.glob("config-*.json"))[:-_HISTORY_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def list_config_history(path: Path | None = None) -> list[dict]:
    """历史版本列表（新的在前）。"""
    hist = _history_dir(path)
    if not hist.exists():
        return []
    out = []
    for f in sorted(hist.glob("config-*.json"), reverse=True):
        st = f.stat()
        out.append({"name": f.name, "mtime_epoch": st.st_mtime, "size": st.st_size})
    return out


def save_config(raw: dict, path: Path | None = None) -> None:
    """序列化并原子写回 config.json（写入前把旧版本快照进 history/）。"""
    from src.logger import log_all

    target_path = _get_config_path(path)
    try:
        _snapshot_config(target_path)
    except OSError as e:
        log_all(f"⚠️ 配置历史快照失败（继续保存）: {e}", is_error=True)
    text = serialize_config(raw)
    try:
        tmp = target_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, target_path)
    except OSError:
        with open(target_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


_on_reload_cb = None


def set_on_reload_callback(cb) -> None:
    global _on_reload_cb
    _on_reload_cb = cb


def _trigger_reload() -> bool:
    """写回后触发进程内热重载（测试中可 monkeypatch 掉）。"""
    from config.config import reload as _reload
    from src.logger import log_all

    ok = _reload()
    cb = _on_reload_cb
    if cb is not None:
        try:
            cb(ok)
        except Exception as e:
            log_all(f"🚨 网页管理端 on_reload 回调异常: {e}", is_error=True)
    return ok


# ================================================================
# 凭证写入：网页填写的密钥落到 .env（与手动编辑同一存放处）
# ================================================================

_SECRET_KEY_RE = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*_(?:TOKEN|COOKIE|REFRESH_TOKEN|CLIENT_SECRET|APP_ID|TARGET_OPENID|SESSIONID|USER_ID)"
    r"|GEMINI_API_KEY|ZHIPU_API_KEY|INSTAGRAM_SESSIONID|INSTAGRAM_DS_USER_ID|X_AUTH_TOKEN|TIKTOK_SESSIONID)$"
)
# 管理端令牌不能从网页写入；旧版全局 TG Token 也只保留迁移检测，
# 防止新配置继续产生“默认 Bot”语义。
_FORBIDDEN_ENV_KEYS = {"WEB_ADMIN_TOKEN", "TG_BOT_TOKEN"}


def _quote_env(val: str) -> str:
    """给 .env 值加引号（python-dotenv 兼容），Cookie 里的空格/分号才能存活。"""
    if "'" not in val:
        return f"'{val}'"
    return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'


def validate_secret_values(values: dict) -> list[str]:
    """校验待写入的 .env 键值对，返回错误列表。"""
    errors = []
    if not values:
        errors.append("没有要写入的键值")
    for key, val in values.items():
        if not isinstance(key, str) or not isinstance(val, str):
            errors.append(f"键值必须是字符串: {key!r}")
            continue
        if key in _FORBIDDEN_ENV_KEYS or not _SECRET_KEY_RE.match(key):
            errors.append(f"不允许通过网页写入的变量: {key}")
        if not val.strip():
            errors.append(f"{key} 的值为空")
        if any(c in val for c in "\r\n\x00"):
            errors.append(f"{key} 的值包含换行等非法字符")
        if len(val) > 16384:
            errors.append(f"{key} 的值过长")
    return errors


def update_env_file(values: dict[str, str], path: Path | None = None,
                    remove: list[str] | None = None) -> None:
    """更新 .env：已有的键原地替换，新键追加到末尾；其余行（注释等）原样保留。
    remove 里的键会被整行删除。"""
    target_path = _get_env_path(path)
    lines = target_path.read_text(encoding="utf-8").splitlines() if target_path.exists() else [
        "# .env — 密钥和凭证（由网页管理端创建，参考 .env.example）",
    ]
    drop = set(remove or [])
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = m.group(1) if m else None
        if key and key in drop:
            continue
        if key and key in remaining:
            out.append(f"{key}={_quote_env(remaining.pop(key))}")
        else:
            out.append(line)
    lines = out
    for key, val in remaining.items():
        lines.append(f"{key}={_quote_env(val)}")
    content = "\n".join(lines) + "\n"
    try:
        tmp = target_path.with_suffix(target_path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target_path)
    except OSError:
        target_path.write_text(content, encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        pass


def _rotate_account_creds(account_id: str) -> None:
    """轮换账号凭证：删除数据库与磁盘持久化凭证 + 清除内存态。"""
    try:
        from src import auth
        auth.delete_account_credential(account_id)
    except (sqlite3.Error, OSError, AttributeError):
        pass
    import config.config as cfg
    if getattr(cfg, "CRED_DIR", None):
        cred_file = Path(cfg.CRED_DIR) / f"{account_id}.json"
        try:
            cred_file.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        from config import credentials as creds_mod
        creds_mod.ACCOUNT_CREDS.pop(account_id, None)
        clear_state = getattr(creds_mod, "clear_refresh_state", None)
        if clear_state is not None:
            clear_state(account_id)
    except Exception:
        pass


# ================================================================
# 凭证状态：按命名约定检查 .env 是否已提供各账号的凭证（只报有/无，不回传值）
# ================================================================

def _cred_status(raw: dict) -> dict:
    status: dict[str, dict] = {}
    for acc_id, acc in raw.get("accounts", {}).items():
        prefix = acc_id.upper()
        is_mobile = acc.get("auth") == "mobile"

        def has(suffix: str, p: str = prefix) -> bool:
            return bool(os.getenv(f"{p}_{suffix}") or os.getenv(f"ACCOUNT_{p}_{suffix}"))

        entry: dict = {}
        if is_mobile:
            entry["expected"] = [f"{prefix}_REFRESH_TOKEN"]
            entry["refresh_token"] = has("REFRESH_TOKEN") or bool(os.getenv("NOGIZAKA_REFRESH_TOKEN"))
            entry["ok"] = entry["refresh_token"]
        else:
            entry["expected"] = [f"{prefix}_TOKEN", f"{prefix}_COOKIE"]
            entry["token"] = has("TOKEN")
            entry["cookie"] = has("COOKIE")
            entry["ok"] = entry["token"] and entry["cookie"]
        status[acc_id] = entry
    return status


def _qq_bot_status(raw: dict) -> list[dict]:
    """QQ 官方 Bot 状态（凭证只报有/无，值不出服务端）。"""
    declared = raw.get("qq_official_bots") or []
    bots = []
    if declared:
        for b in declared:
            prefix = str(b.get("name", "")).upper()
            entry = {
                "name": b.get("name", ""),
                "remark": b.get("remark", ""),
                "declared": True,
                "app_id": bool(b.get("app_id") or os.getenv(f"{prefix}_APP_ID")),
                "client_secret": bool(os.getenv(f"{prefix}_CLIENT_SECRET")),
                "target_openid": bool(b.get("target_openid") or os.getenv(f"{prefix}_TARGET_OPENID")),
                "group_openid": bool(b.get("group_openid") or os.getenv(f"{prefix}_GROUP_OPENID")),
                "member_filter": b.get("member_filter") or [],
                "secret_env": f"{prefix}_CLIENT_SECRET",
            }
            entry["ok"] = entry["app_id"] and entry["client_secret"]
            bots.append(entry)
        return bots

    for i in range(1, 21):
        entry = {
            "name": f"BOT{i}",
            "declared": False,
            "app_id": bool(os.getenv(f"QQ_OFFICIAL_BOT{i}_APP_ID")),
            "client_secret": bool(os.getenv(f"QQ_OFFICIAL_BOT{i}_CLIENT_SECRET")),
            "target_openid": bool(os.getenv(f"QQ_OFFICIAL_BOT{i}_TARGET_OPENID")),
            "secret_env": f"QQ_OFFICIAL_BOT{i}_CLIENT_SECRET",
        }
        entry["ok"] = entry["app_id"] and entry["client_secret"]
        if entry["app_id"]:
            bots.append(entry)
    return bots
