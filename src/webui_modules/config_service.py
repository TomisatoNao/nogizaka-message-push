"""
src/webui_modules/config_service.py — Web 管理端配置校验、序列化、历史版本与密钥管理服务

将 schema 校验、JSONC 分区序列化、配置版本历史快照、.env 密钥写入以及
账号/官方 Bot 状态分析等业务逻辑从单一庞大的 Web 路由文件中解耦，提升模块化程度与可维护性。
"""

from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
from urllib.parse import parse_qsl, urlsplit

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_SCHEMA_PATH = _BASE_DIR / "config" / "config.schema.json"
DEFAULT_CONFIG_PATH = _BASE_DIR / "config" / "config.json"
DEFAULT_ENV_PATH = _BASE_DIR / ".env"

CONFIG_PATH = DEFAULT_CONFIG_PATH
SCHEMA_PATH = DEFAULT_SCHEMA_PATH
ENV_PATH = DEFAULT_ENV_PATH

# Revision values are process-scoped opaque tokens. HMAC keeps configuration and
# credential bytes out of responses and prevents the token becoming an offline
# guessing oracle for low-entropy secret values.
_REVISION_KEY = secrets.token_bytes(32)
_PREVIEW_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:token|secret|cookie|password|passwd|passphrase|api[_-]?key|"
    r"authorization|credential|session(?:id|[_-]?id)?)(?:$|[_-])",
    re.IGNORECASE,
)


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


def config_state_revision(
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> str:
    """Return an opaque revision covering config, managed secrets, and IG cookies.

    Including .env makes a staged config/secret commit stale when another
    management request changes credentials after the browser loaded the form.
    The Instagram cookie store contributes only its non-sensitive version marker,
    never cookie values. Missing files have distinct markers from empty files.
    """
    parts = bytearray(b"webui-config-state-v1\0")
    for label, path in ((b"config\0", _get_config_path(config_path)),
                         (b"env\0", _get_env_path(env_path))):
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            content = None
        parts.extend(label)
        if content is None:
            parts.extend(b"missing\0")
        else:
            parts.extend(b"present\0")
            parts.extend(len(content).to_bytes(8, "big"))
            parts.extend(content)
    from src.social import ig_session

    cookie_db = Path(ig_session.DB_PATH)
    cookie_marker = (
        ("present", str(cookie_db.resolve()), ig_session.cookie_store_version())
        if cookie_db.exists()
        else ("missing", str(cookie_db.resolve()))
    )
    parts.extend(b"instagram-cookie-store\0")
    parts.extend(json.dumps(cookie_marker, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
    parts.extend(b"\0")
    return hmac.new(_REVISION_KEY, bytes(parts), hashlib.sha256).hexdigest()


def _is_secret_preview_value(key: str, value) -> bool:
    if _PREVIEW_SECRET_KEY_RE.search(key) or key.casefold() in {"napcat_api", "proxy", "http_proxy", "https_proxy"}:
        return True
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password:
            return True
        return any(_PREVIEW_SECRET_KEY_RE.search(name) for name, _ in parse_qsl(parsed.query))
    except ValueError:
        return False


def redact_config_preview(raw: dict) -> dict:
    """Copy a config for read-only history preview with secret values removed."""

    def redact(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                key_text = str(key)
                result[key] = "[redacted]" if _is_secret_preview_value(key_text, item) else redact(item)
            return result
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return redact(raw)


def redact_config_error_messages(errors: list[str], *sources: object) -> list[str]:
    """Remove serialized secret-bearing config values from validation errors."""
    fragments: set[str] = set()

    def collect(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if _is_secret_preview_value(str(key), item):
                    try:
                        fragments.add(repr(item))
                        fragments.add(json.dumps(item, ensure_ascii=False))
                    except (TypeError, ValueError):
                        pass
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for source in sources:
        collect(source)

    replacements = sorted((item for item in fragments if item), key=len, reverse=True)
    redacted = []
    for error in errors:
        message = str(error)
        for fragment in replacements:
            message = message.replace(fragment, "[redacted]")
        redacted.append(message)
    return redacted


def config_preview_diff(current: dict, target: dict) -> dict:
    """Return nested change paths and kinds without including config values."""
    changes: dict[str, dict[str, str]] = {}

    def stable_item_key(root: str, item: object) -> tuple[str, ...] | None:
        if not isinstance(item, dict):
            return None
        if root == "monitor" and item.get("account") is not None and item.get("id") is not None:
            return ("member", str(item["account"]), str(item["id"]))
        if root in {"qq_official_bots", "tg_bots"} and item.get("name") is not None:
            return ("name", str(item["name"]))
        if root == "napcat_routes" and item.get("group_id") is not None:
            return ("group", str(item["group_id"]))
        for name in ("id", "name", "group_id"):
            if item.get(name) is not None:
                return (name, str(item[name]))
        return None

    def add(path: tuple[str, ...], kind: str) -> None:
        if not path:
            return
        root = path[0]
        path_text = ".".join(path)
        changes.setdefault(root, {})[path_text] = kind

    def walk(before: object, after: object, path: tuple[str, ...], before_exists=True, after_exists=True) -> None:
        if before_exists == after_exists and (not before_exists or before == after):
            return
        if not before_exists:
            add(path, "added")
            return
        if not after_exists:
            add(path, "removed")
            return

        if isinstance(before, dict) and isinstance(after, dict):
            keys = sorted(set(before) | set(after), key=lambda item: str(item))
            if not keys:
                add(path, "changed")
                return
            for key in keys:
                key_text = str(key)
                walk(
                    before.get(key), after.get(key), path + (key_text,),
                    key in before, key in after,
                )
            return

        if isinstance(before, list) and isinstance(after, list):
            root = path[0] if path else ""
            all_items = [*before, *after]
            keys_before = [stable_item_key(root, item) for item in before]
            keys_after = [stable_item_key(root, item) for item in after]
            keyed = (
                all(not isinstance(item, dict) or key is not None
                    for item, key in zip(all_items, keys_before + keys_after))
                and all(key is not None for key in keys_before + keys_after)
                and len(set(keys_before)) == len(keys_before)
                and len(set(keys_after)) == len(keys_after)
            )
            if not keyed:
                add(path, "changed")
                return

            before_map = dict(zip(keys_before, before))
            after_map = dict(zip(keys_after, after))
            before_common = [key for key in keys_before if key in after_map]
            after_common = [key for key in keys_after if key in before_map]
            if before_common != after_common:
                add(path + ("[顺序]",), "changed")
            item_path = path + ("[项目]",)
            for key in keys_before:
                if key not in after_map:
                    add(item_path, "removed")
                else:
                    walk(before_map[key], after_map[key], item_path)
            if any(key not in before_map for key in keys_after):
                add(item_path, "added")
            return

        add(path, "changed")

    walk(current, target, (), True, True)
    groups = []
    for root in sorted(changes):
        groups.append({
            "label": root,
            "changes": [
                {"path": path, "kind": kind}
                for path, kind in sorted(changes[root].items())
            ],
        })
    return {"groups": groups}


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

    # NapCat 的新结构将服务基地址与 Token 分开。Schema 负责类型，
    # 这里补充跨字段无法表达的 URL/密钥约束；旧版 napcat_api 完整地址
    # 仍保留兼容，不强制用户在升级时手工迁移。
    napcat_base = str(raw.get("napcat_api_base") or "").strip()
    if napcat_base:
        try:
            if any(ch.isspace() for ch in napcat_base):
                raise ValueError("不能包含空白字符")
            parsed = urlsplit(napcat_base)
            endpoint = (parsed.path or "").rstrip("/").rsplit("/", 1)[-1]
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("必须是有效的 http(s) URL")
            if parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("基地址不能包含查询参数、片段或账号密码")
            if endpoint in {"send_group_msg", "send_group_forward_msg", "get_status"}:
                raise ValueError("基地址不能包含 OneBot 动作路径")
        except ValueError as exc:
            errors.append(f"NapCat API 基地址无效: {exc}")
    napcat_token = str(raw.get("napcat_api_token") or "")
    if len(napcat_token) > 512 or any(ch.isspace() for ch in napcat_token) or "\x00" in napcat_token:
        errors.append("NapCat 访问密钥不能包含空白字符，且最多 512 个字符")

    accounts = raw.get("accounts", {})
    seen: set[tuple[str, str]] = set()
    social_patterns = {
        "x": re.compile(r"^[A-Za-z0-9_]{1,15}$"),
        "instagram": re.compile(r"^[A-Za-z0-9._]{1,30}$"),
        "tiktok": re.compile(r"^[A-Za-z0-9._]{1,50}$"),
        "tiktok_live": re.compile(r"^[A-Za-z0-9._]{1,50}$"),
    }
    social_owners: dict[tuple[str, str], tuple[int, str]] = {}
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
        social = m.get("social") or {}
        if not isinstance(social, dict):
            errors.append(f"成员 {label} 的社媒绑定必须是对象")
            continue
        for platform, pattern in social_patterns.items():
            values = social.get(platform) or []
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                errors.append(f"成员 {label} 的 {platform} 账号必须是列表或字符串")
                continue
            if platform != "tiktok_live" and len(values) > 1:
                errors.append(f"成员 {label} 的 {platform} 只能绑定一个账号")
            local_seen: set[str] = set()
            for value in values:
                account = str(value).strip().lstrip("@").strip()
                identity = account.lower()
                if not pattern.fullmatch(account):
                    errors.append(f"成员 {label} 的 {platform} 账号格式无效: {value!r}")
                    continue
                if identity in local_seen:
                    errors.append(f"成员 {label} 的 {platform} 账号重复: {account}")
                    continue
                local_seen.add(identity)
                identity_platform = "tiktok" if platform == "tiktok_live" else platform
                owner = social_owners.get((identity_platform, identity))
                if owner is not None and owner[0] != i:
                    errors.append(f"{identity_platform} 账号 {account} 同时绑定成员 {owner[1]} 与 {label}")
                else:
                    social_owners[(identity_platform, identity)] = (i, str(label))

    bot_names: set[str] = set()
    for b in raw.get("qq_official_bots", []):
        name = b.get("name", "")
        if name in bot_names:
            errors.append(f"官方 Bot 名称重复: {name!r}")
        bot_names.add(name)

    # Telegram Bot 名称会映射到 .env 变量；重复或归一化后冲突会导致
    # 两个路由意外共用同一凭证，因此在保存前直接阻止。
    from src.config.config import tg_token_env_key
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
    ("── 推送通道 ──",  ["channels", "napcat_api_base", "napcat_api_token", "napcat_api", "napcat_forward_user_id", "napcat_forward_nickname", "napcat_media_base_url", "napcat_media_signing_secret", "napcat_routes", "napcat_inbound", "napcat_photo", "napcat_ai_chat", "tg_bots", "qq_official_bots"]),
    ("── 网页管理 ──",  ["web_admin"]),
    ("── 消息归档 ──",  ["archive"]),
    ("── 每日摘要 ──",  ["daily_summary"]),
    ("── Bot 指令 ──",  ["qq_commands"]),
    ("── 账号系统 ──",  ["auth"]),
    ("── 账号池 ──",    ["accounts"]),
    ("── 监控成员 ──",  ["monitor"]),
    ("── 推送节奏 ──",  ["day_interval", "night_interval", "sleep_hours", "alert_cooldown"]),
    ("── 全局监控调度（JST）──", ["monitor_schedule"]),
]
_OPTIONAL_ORDER = ["qq_send_interval", "napcat_send_timeout_seconds", "translate", "image_tagging", "gemini_models", "gemini_blog_models", "gemini_min_interval", "translate_timeout", "translate_total_timeout"]
_OPTIONAL_COMMENT = "── 可选覆盖 ──（不写则用内置默认值）"


def _dump(val) -> str:
    return json.dumps(val, ensure_ascii=False)


def _render_value(key: str, val) -> str:
    """按键渲染值：容器类展开为多行（账号/成员/模型每项一行），标量内联。"""
    if key == "accounts" and isinstance(val, dict) and val:
        rows = [f"    {_dump(k)}: {_dump(v)}" for k, v in val.items()]
        return "{\n" + ",\n".join(rows) + "\n  }"
    if key in ("monitor", "gemini_models", "gemini_blog_models", "qq_official_bots", "napcat_routes", "tg_bots") \
            and isinstance(val, list) and val:
        rows = [f"    {_dump(item)}" for item in val]
        return "[\n" + ",\n".join(rows) + "\n  ]"
    if key in ("channels", "web_admin", "archive", "daily_summary", "auth", "qq_commands", "monitor_schedule", "napcat_inbound", "napcat_photo", "napcat_ai_chat") \
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
    text = serialize_config(raw)
    before_config = target_path.read_bytes() if target_path.exists() else None
    history_dir = _history_dir(target_path)
    history_dir_existed = history_dir.exists()
    history_before = {
        entry: entry.read_bytes()
        for entry in history_dir.glob("config-*.json")
    } if history_dir_existed else {}

    def restore_history() -> None:
        try:
            if history_dir.exists():
                for entry in history_dir.glob("config-*.json"):
                    entry.unlink(missing_ok=True)
            if history_before:
                history_dir.mkdir(parents=True, exist_ok=True)
                for entry, content in history_before.items():
                    entry.write_bytes(content)
            elif not history_dir_existed and history_dir.exists():
                history_dir.rmdir()
        except OSError as exc:
            log_all(f"🚨 配置历史回滚失败 ({type(exc).__name__})", is_error=True)

    try:
        _snapshot_config(target_path)
    except OSError as e:
        restore_history()
        log_all(f"⚠️ 配置历史快照失败（继续保存）: {type(e).__name__}", is_error=True)

    tmp = target_path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, target_path)
    except Exception:
        _restore_file_bytes(target_path, before_config)
        restore_history()
        raise
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


_on_reload_cb = None


def set_on_reload_callback(cb) -> None:
    global _on_reload_cb
    _on_reload_cb = cb


def _trigger_reload() -> bool:
    """Compatibility wrapper returning only whether the config facade loaded."""
    return _trigger_reload_report()["reloaded"]


def _trigger_reload_report() -> dict:
    """Reload the config facade and report callback dispatch separately.

    The callback may schedule asynchronous component work and can swallow its
    own failures. A normal return therefore means only that dispatch completed;
    it does not verify that every runtime component applied the new settings.
    """
    import inspect

    from src.logger import log_all
    from src.webui_modules.reload_status import (
        create_reload_report,
        get_reload_report,
        mark_pending_unconfirmed,
        record_config_load,
        record_reload_callback,
    )

    reload_id = create_reload_report()

    try:
        from src.config.config import reload as _reload
        loaded = bool(_reload())
    except Exception as exc:
        loaded = False
        log_all(
            f"🚨 网页管理端配置 facade 重载异常 ({type(exc).__name__})",
            is_error=True,
        )
    record_config_load(reload_id, loaded)

    cb = _on_reload_cb
    if cb is None:
        components = "unavailable"
        callback_status = "unavailable" if loaded else "not_scheduled"
    else:
        try:
            signature = inspect.signature(cb)
            signature.bind(loaded, reload_id=reload_id)
            supports_reload_id = True
        except (TypeError, ValueError):
            supports_reload_id = False
        try:
            if supports_reload_id:
                cb(loaded, reload_id=reload_id)
            else:
                cb(loaded)
        except Exception as exc:
            components = "dispatch_failed"
            callback_status = "dispatch_failed"
            log_all(
                f"🚨 网页管理端 on_reload 回调派发异常 ({type(exc).__name__})",
                is_error=True,
            )
        else:
            components = "pending" if loaded else "not_scheduled"
            callback_status = "dispatched" if loaded else "not_scheduled"
    record_reload_callback(
        reload_id,
        callback_status,
        reason_code=(
            "callback_unavailable" if callback_status == "unavailable"
            else "callback_dispatch_failed" if callback_status == "dispatch_failed"
            else None
        ),
    )
    if loaded and cb is not None and callback_status == "dispatched" and not supports_reload_id:
        # Legacy callbacks remain callable, but provide no way to correlate
        # component acknowledgements with this reload.
        mark_pending_unconfirmed(
            reload_id, reason_code="component_confirmation_unavailable",
        )

    return {
        "reloaded": loaded,
        "reload": {
            "id": reload_id,
            "config": "loaded" if loaded else "failed",
            "components": components,
            "application": get_reload_report(reload_id),
        },
    }


# ================================================================
# 凭证写入：网页填写的密钥落到 .env（与手动编辑同一存放处）
# ================================================================

_SECRET_KEY_RE = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*_(?:TOKEN|COOKIE|REFRESH_TOKEN|CLIENT_SECRET|APP_ID|TARGET_OPENID|SESSIONID|USER_ID)"
    r"|GEMINI_API_KEY|ZHIPU_API_KEY|INSTAGRAM_SESSIONID|INSTAGRAM_DS_USER_ID|X_AUTH_TOKEN|TIKTOK_SESSIONID"
    r"|CPA_API_KEY|NAPCAT_EVENT_TOKEN)$"
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
    tmp = target_path.with_suffix(target_path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        pass


def _restore_file_bytes(path: Path, content: bytes | None) -> None:
    """恢复组合配置提交前的文件状态（仅供同一 mutation lock 使用）。"""
    if content is None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    tmp = path.with_suffix(path.suffix + ".rollback.tmp")
    try:
        tmp.write_bytes(content)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _read_env_assignments(path: Path) -> dict[str, str]:
    """读取 .env 中可迁移的键值；无 python-dotenv 时使用轻量回退解析。"""
    try:
        from dotenv import dotenv_values
        parsed = dotenv_values(path) if path.exists() else {}
        return {str(k): str(v) for k, v in parsed.items() if v is not None}
    except Exception:
        parsed: dict[str, str] = {}
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    text = line.strip()
                    if not text or text.startswith("#"):
                        continue
                    if text.startswith("export "):
                        text = text[7:].lstrip()
                    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", text)
                    if not match:
                        continue
                    key, value = match.groups()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                        value = value[1:-1]
                        if text.endswith('"'):
                            value = value.replace("\\\"", '"').replace("\\\\", "\\")
                    parsed[key] = value
            except OSError:
                pass
        if parsed:
            return parsed
        return {
            key: value for key, value in os.environ.items()
            if isinstance(key, str) and isinstance(value, str)
        }


def commit_config_and_secrets(
    raw: dict,
    *,
    secret_updates: dict[str, str] | None = None,
    secret_removes: list[str] | None = None,
    secret_renames: list[dict[str, str]] | None = None,
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> dict:
    """在调用方 mutation lock 内提交 config 与 Bot 凭证变更。

    配置和 .env 都使用现有的原子写入函数；如果后续写入失败，则恢复
    本次请求前的两个文件。函数不触发 reload，也不把密钥值返回给调用方。
    """
    target_config = _get_config_path(config_path)
    target_env = _get_env_path(env_path)
    updates = dict(secret_updates or {})
    removes = list(secret_removes or [])
    renames = list(secret_renames or [])

    before_config = target_config.read_bytes() if target_config.exists() else None
    before_env = target_env.read_bytes() if target_env.exists() else None
    history_dir = _history_dir(target_config)
    history_before = {
        path: path.read_bytes()
        for path in history_dir.glob("config-*.json")
    } if history_dir.exists() else {}
    history_dir_existed = history_dir.exists()
    env_values = _read_env_assignments(target_env)
    touched_env_keys = {
        key for key in (set(updates) | set(removes))
        if isinstance(key, str)
    }
    for item in renames:
        touched_env_keys.update({str(item.get("from", "")).strip(), str(item.get("to", "")).strip()})
    env_before_process = {
        key: os.environ.get(key)
        for key in touched_env_keys
        if key
    }
    resolved_updates = dict(updates)
    resolved_removes = set(removes)
    resolved_renames: list[dict[str, str]] = []

    for item in renames:
        old = str(item.get("from", "")).strip()
        new = str(item.get("to", "")).strip()
        if not old or not new or old == new:
            continue
        old_exists = old in env_values or old in os.environ
        new_exists = new in env_values or new in os.environ
        if old_exists and new_exists:
            raise ValueError(f"凭证迁移冲突：{old} 与 {new} 同时存在")
        if old_exists:
            value = env_values.get(old, os.environ.get(old, ""))
            resolved_updates[new] = value
            resolved_removes.add(old)
            resolved_renames.append({"from": old, "to": new})

    # 显式更新优先于同名删除；调用方应在校验阶段阻止冲突，这里再次防御。
    overlap = set(resolved_updates) & resolved_removes
    if overlap:
        raise ValueError("凭证更新与删除不能使用相同变量: " + ", ".join(sorted(overlap)))

    try:
        save_config(raw, path=target_config)
        if resolved_updates or resolved_removes:
            update_env_file(resolved_updates, path=target_env, remove=sorted(resolved_removes))
        for key, value in resolved_updates.items():
            os.environ[key] = value
        for key in resolved_removes:
            os.environ.pop(key, None)
    except Exception:
        _restore_file_bytes(target_config, before_config)
        _restore_file_bytes(target_env, before_env)
        for key, value in env_before_process.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # save_config 会先写入历史快照；组合提交失败时也恢复快照目录，
        # 避免“失败请求”在回滚后留下看似成功的历史版本。
        try:
            if history_dir.exists():
                for path in history_dir.glob("config-*.json"):
                    path.unlink(missing_ok=True)
            if history_before:
                history_dir.mkdir(parents=True, exist_ok=True)
                for path, content in history_before.items():
                    path.write_bytes(content)
            elif not history_dir_existed:
                history_dir.rmdir()
        except OSError:
            pass
        raise

    return {
        "updated": sorted(resolved_updates),
        "removed": sorted(resolved_removes),
        "renamed": resolved_renames,
    }


def _rotate_account_creds(account_id: str) -> None:
    """轮换账号凭证：删除数据库与磁盘持久化凭证 + 清除内存态。"""
    try:
        from src import auth
        auth.delete_account_credential(account_id)
    except (sqlite3.Error, OSError, AttributeError):
        pass
    import src.config.config as cfg
    if getattr(cfg, "CRED_DIR", None):
        cred_file = Path(cfg.CRED_DIR) / f"{account_id}.json"
        try:
            cred_file.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        from src.config import credentials as creds_mod
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
