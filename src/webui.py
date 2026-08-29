# ============================================================
# webui.py — 网页管理端：在浏览器里编辑 config.json 并热重载
# ============================================================
# 零新增依赖：用 stdlib http.server 跑在后台守护线程（与 watcher 同模式）。
#
# 端点：
#   GET  /            → 管理页面（src/webui_static/index.html）
#   GET  /api/config  → 读取 config.json（解析后的对象 + 凭证状态）
#   PUT  /api/config  → 校验 → 原子写回 config.json → 热重载
#   POST /api/reload  → 仅触发热重载
#
# 鉴权：.env 里设置 WEB_ADMIN_TOKEN 后，所有 /api 请求须带
#       X-Auth-Token 头。默认只绑定 127.0.0.1，本机访问可不设 token。
#
# 保存说明：config.json 会按固定分区重新生成（含标准分区注释），
#           手写的自定义注释会丢失。
# ============================================================
from __future__ import annotations

import asyncio
import hmac
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import threading
import time as _time
from datetime import datetime, timedelta
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

_BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = _BASE_DIR
CONFIG_PATH = _BASE_DIR / "config" / "config.json"
SCHEMA_PATH = _BASE_DIR / "config" / "config.schema.json"
ENV_PATH = _BASE_DIR / ".env"
_STATIC_PATH = Path(__file__).resolve().parent / "webui_static" / "index.html"
_ARCHIVE_HTML_PATH = Path(__file__).resolve().parent / "webui_static" / "archive.html"
_LOGIN_HTML_PATH = Path(__file__).resolve().parent / "webui_static" / "login.html"

_SESSION_COOKIE = "sakamichi_session"

# 首页 API 缓存（基于 archive.db 的 mtime + 日期，保证每天随机结果不同）
_home_cache: dict | None = None
_home_cache_key: tuple[float, str] | None = None

# 热重载成功后的补偿回调（由 start_webui 注入，签名 on_reload(success: bool)）
_on_reload_cb = None

# 重启回调（由主程序注入；触发优雅停机 + 进程自替换。独立模式下为 None）
_on_restart_cb = None

# 立即巡查回调（由主程序注入；唤醒主循环跳过等待。独立模式下为 None）
_on_poll_cb = None

# 测试推送回调（由主程序注入；签名 (channel, target, text) -> (ok, err)）
_on_test_push_cb = None

# openid 监听回调（由主程序注入；把协程调度到主事件循环执行）
_on_openid_cb = None

# 串行化所有写操作（config.json / .env 都是 read-modify-write，
# ThreadingHTTPServer 的并发请求不加锁会互相丢更新）
_mutation_lock = threading.Lock()

# 绑定回环地址时校验 Host 头，阻断 DNS rebinding（恶意网页把自己域名解析到
# 127.0.0.1 就能绕过浏览器同源策略直接调本地 API）。绑定其他地址时不启用 ——
# 局域网访问的合法 Host 无法穷举，此时安全性由 WEB_ADMIN_TOKEN 保证。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_enforce_host_check = False


# ================================================================
# 校验
# ================================================================

def validate_config(raw: dict) -> list[str]:
    """校验新格式配置对象，返回错误列表（空列表 = 通过）。

    1. config.schema.json 结构校验
    2. 引用完整性：monitor[].account 必须在 accounts 中定义
    3. 同一账号下成员 id 不得重复
    """
    errors: list[str] = []

    import jsonschema
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = json.load(f)
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as e:
        loc = " → ".join(str(p) for p in e.absolute_path) if e.absolute_path else "(根)"
        errors.append(f"结构校验失败 [{loc}]: {e.message}")
        return errors  # 结构不对时后续检查没有意义

    accounts = raw.get("accounts", {})
    seen: set[tuple[str, str]] = set()
    for i, m in enumerate(raw.get("monitor", [])):
        label = m.get("name") or f"#{i}"
        # schema 只查存在性和类型，空字符串会通过——在这里兜住
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

    return errors


# ================================================================
# 序列化：dict → 带分区注释的 JSONC 文本
# ================================================================

_SECTIONS: list[tuple[str, list[str]]] = [
    ("── 推送通道 ──",  ["channels", "napcat_api", "qq_official_bots"]),
    ("── 网页管理 ──",  ["web_admin"]),
    ("── 消息归档 ──",  ["archive"]),
    ("── 每日摘要 ──",  ["daily_summary"]),
    ("── Bot 指令 ──",  ["qq_commands"]),
    ("── 账号系统 ──",  ["auth"]),
    ("── 账号池 ──",    ["accounts"]),
    ("── 监控成员 ──",  ["monitor"]),
    ("── 推送节奏 ──",  ["day_interval", "night_interval", "sleep_hours", "alert_cooldown"]),
]
# 其余键统一归入「可选覆盖」分区，按此顺序排列，未列出的键按字母序跟在后面
_OPTIONAL_ORDER = ["qq_send_interval", "translate", "image_tagging", "gemini_models", "gemini_min_interval", "translate_timeout"]
_OPTIONAL_COMMENT = "── 可选覆盖 ──（不写则用内置默认值）"


def _dump(val) -> str:
    return json.dumps(val, ensure_ascii=False)


def _render_value(key: str, val) -> str:
    """按键渲染值：容器类展开为多行（账号/成员/模型每项一行），标量内联。"""
    if key == "accounts" and isinstance(val, dict) and val:
        rows = [f"    {_dump(k)}: {_dump(v)}" for k, v in val.items()]
        return "{\n" + ",\n".join(rows) + "\n  }"
    if key in ("monitor", "gemini_models", "qq_official_bots") and isinstance(val, list) and val:
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
    # blocks: (comment | None, key_lines | None) — 注释行不参与逗号逻辑
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


_HISTORY_KEEP = 10
_HISTORY_NAME_RE = re.compile(r"^config-[0-9-]+\.json$")


def _history_dir(path: Path | None = None) -> Path:
    return (path or CONFIG_PATH).parent / "history"


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

    path = path or CONFIG_PATH
    try:
        _snapshot_config(path)
    except OSError as e:
        log_all(f"⚠️ 配置历史快照失败（继续保存）: {e}", is_error=True)
    text = serialize_config(raw)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _trigger_reload() -> bool:
    """写回后触发进程内热重载（测试中可 monkeypatch 掉）。"""
    from config.config import reload as _reload
    from src.logger import log_all

    ok = _reload()
    if _on_reload_cb is not None:
        try:
            _on_reload_cb(ok)
        except Exception as e:
            log_all(f"🚨 网页管理端 on_reload 回调异常: {e}", is_error=True)
    return ok


# ================================================================
# 凭证写入：网页填写的密钥落到 .env（与手动编辑同一存放处）
# ================================================================

# 允许通过网页写入的 .env 变量名（白名单）。
# WEB_ADMIN_TOKEN 故意排除：管理端令牌只能手动设置，防止误操作把自己锁在门外。
_SECRET_KEY_RE = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*_(?:TOKEN|COOKIE|REFRESH_TOKEN|CLIENT_SECRET|APP_ID|TARGET_OPENID|SESSIONID|USER_ID)"
    r"|GEMINI_API_KEY|ZHIPU_API_KEY|INSTAGRAM_SESSIONID|INSTAGRAM_DS_USER_ID|X_AUTH_TOKEN|TIKTOK_SESSIONID)$"
)
_FORBIDDEN_ENV_KEYS = {"WEB_ADMIN_TOKEN"}


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
    path = path or ENV_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else [
        "# .env — 密钥和凭证（由网页管理端创建，参考 .env.example）",
    ]
    drop = set(remove or [])
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = m.group(1) if m else None
        if key and key in drop:
            continue                       # 整行删除
        if key and key in remaining:
            out.append(f"{key}={_quote_env(remaining.pop(key))}")
        else:
            out.append(line)
    lines = out
    for key, val in remaining.items():
        lines.append(f"{key}={_quote_env(val)}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _rotate_account_creds(account_id: str) -> None:
    """轮换账号凭证：删除数据库与磁盘持久化凭证 + 清除内存态。"""
    try:
        from src import auth
        auth.delete_account_credential(account_id)
    except Exception:
        pass
    import config.config as cfg
    if getattr(cfg, "CRED_DIR", None):
        cred_file = Path(cfg.CRED_DIR) / f"{account_id}.json"
        try:
            cred_file.unlink(missing_ok=True)
        except OSError:
            pass
    creds_mod = sys.modules.get("config.credentials")
    if creds_mod is not None:
        creds_mod.ACCOUNT_CREDS.pop(account_id, None)


# ================================================================
# 凭证状态：按命名约定检查 .env 是否已提供各账号的凭证（只报有/无，不回传值）
# ================================================================

def _cred_status(raw: dict) -> dict:
    status: dict[str, dict] = {}
    for acc_id, acc in raw.get("accounts", {}).items():
        prefix = acc_id.upper()
        is_mobile = acc.get("auth") == "mobile"

        def has(suffix: str) -> bool:
            return bool(os.getenv(f"{prefix}_{suffix}") or os.getenv(f"ACCOUNT_{prefix}_{suffix}"))

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
    """QQ 官方 Bot 状态（凭证只报有/无，值不出服务端）。

    config.json 声明了 qq_official_bots 时按声明报告（secret 从 .env 匹配）；
    未声明时回落到旧的 .env 编号槽位（QQ_OFFICIAL_BOT{1..20}_*）。
    """
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

    # 兼容模式：只报告 .env 里确实配过的编号槽位。
    # 早先无条件展示前两个空槽位，结果没配过 Bot 的用户也会看到两行"未使用"，
    # 白白造成困惑。
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
        # 必须有 APP_ID 才算一个真实的旧槽位：app_id 才是 Bot 的身份，
        # 只剩 SECRET 的多半是删除 Bot 后的残留，不该再显示成槽位
        if entry["app_id"]:
            bots.append(entry)
    return bots


# ================================================================
# HTTP 服务
# ================================================================

def _load_raw_config() -> dict:
    """从磁盘读取 config.json（JSONC），返回新格式对象。"""
    import json5
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json5.load(f)


def _env_status() -> dict:
    return {
        "GEMINI_API_KEY": bool(os.getenv("GEMINI_API_KEY")),
        "ZHIPU_API_KEY": bool(os.getenv("ZHIPU_API_KEY")),
        "TG_BOT_TOKEN": bool(os.getenv("TG_BOT_TOKEN")),
        "INSTAGRAM_SESSIONID": bool(os.getenv("INSTAGRAM_SESSIONID")),
        "X_AUTH_TOKEN": bool(os.getenv("X_AUTH_TOKEN")),
        "TIKTOK_SESSIONID": bool(os.getenv("TIKTOK_SESSIONID")),
    }


_TAIL_READ_BYTES = 262144   # 只读文件末尾 256KB，滚动日志单文件 10MB，够看不卡


def _tail_file(path: Path, max_lines: int) -> list[str]:
    """读取文件末尾 max_lines 行（长行截断到 2000 字符）。"""
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - _TAIL_READ_BYTES))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > _TAIL_READ_BYTES and lines:
        lines = lines[1:]   # 首行可能被截断，丢弃
    return [ln[:2000] for ln in lines[-max_lines:]]


BLOG_IMAGE_DIR = Path("data/blog_images")
_blog_db_local = threading.local()


def _get_blog_db() -> sqlite3.Connection:
    """获取线程本地的博客 DB 连接（WAL 模式 + 并发隔离）。"""
    from src.blog_fetcher import init_blog_db
    conn = getattr(_blog_db_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1;")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _blog_db_local.conn = None
    conn = init_blog_db()
    _blog_db_local.conn = conn
    return conn


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        """处理 HTTP 请求，静默忽略客户端主动断连或刷新等无害网络异常。"""
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def _query_params(self) -> dict[str, str]:
        """解析 URL 查询参数。"""
        from urllib.parse import urlparse, parse_qs
        qs = urlparse(self.path).query
        result = {}
        for k, v in parse_qs(qs).items():
            result[k] = v[0] if v else ""
        return result
    server_version = "SakamichiWebUI/1.0"

    # ── 工具 ─────────────────────────────────────────────
    def _send_json(self, obj: dict, code: int = 200) -> None:
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _check_host(self) -> bool:
        """绑定回环地址时校验 Host 头（防 DNS rebinding）。"""
        if not _enforce_host_check:
            return True
        host = self.headers.get("Host", "")
        if host.startswith("["):                    # IPv6: [::1]:8787
            hostname = host[1:].split("]", 1)[0]
        else:
            hostname = host.rsplit(":", 1)[0] if ":" in host else host
        if hostname in _LOOPBACK_HOSTS:
            return True
        self._send_json({"ok": False, "errors": [f"拒绝非本机 Host: {host!r}"]}, 403)
        return False

    # ── 认证与授权 ─────────────────────────────────
    def _api_token_ok(self) -> bool:
        token = os.getenv("WEB_ADMIN_TOKEN", "").strip()
        if not token:
            return False
        supplied = self.headers.get("X-Auth-Token", "").strip()
        if not supplied:
            authz = self.headers.get("Authorization", "").strip()
            if authz.startswith("Bearer "):
                supplied = authz[7:].strip()
        if not supplied:
            # <img>/<video> 标签无法带自定义头，归档媒体通过 query 传 token
            from urllib.parse import parse_qs
            supplied = (parse_qs(self.path.partition("?")[2]).get("token") or [""])[0]
        return bool(supplied) and hmac.compare_digest(supplied, token)

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == _SESSION_COOKIE:
                return value
        return ""

    def _current_user(self) -> dict | None:
        """解析当前身份：会话 cookie 优先，其次 API token（视为 admin）。"""
        import config.config as cfg
        from src import auth as _auth
        ttl = max(1, int(getattr(cfg, "AUTH_SESSION_HOURS", 12))) * 3600
        sess = _auth.get_session(self._cookie_token(), ttl_seconds=ttl)
        if sess:
            return {"username": sess["username"], "role": sess["role"], "via": "session"}
        if self._api_token_ok():
            return {"username": "(api-token)", "role": "admin", "via": "token"}
        return None

    def _check_origin(self) -> bool:
        """非 GET 请求校验 Origin（配合 SameSite=Strict cookie 防 CSRF）。
        无 Origin 头的请求（curl / 脚本）放行——它们不携带浏览器 cookie 语义。"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        from urllib.parse import urlparse
        host = (urlparse(origin).hostname or "")
        if host in _LOOPBACK_HOSTS or host == (self.headers.get("Host", "").rsplit(":", 1)[0]):
            return True
        self._send_json({"ok": False, "errors": [f"拒绝跨站请求 Origin: {origin!r}"]}, 403)
        return False

    def _guard(self, need_admin: bool = True, is_page: bool = False) -> bool:
        """路由守卫。通过返回 True；否则已发送响应（页面 302 / API 401·403）返回 False。"""
        import config.config as cfg
        from src import auth as _auth

        if not getattr(cfg, "AUTH_ENABLED", False):
            # 账号系统未启用：沿用 WEB_ADMIN_TOKEN 语义（未设 token 则全放行）
            if not os.getenv("WEB_ADMIN_TOKEN", ""):
                return True
            if self._api_token_ok():
                return True
            self._send_json({"ok": False, "errors": ["未授权：X-Auth-Token 缺失或错误"]}, 401)
            return False

        # 归档在 archive_public 下免登录
        if not need_admin and getattr(cfg, "AUTH_ARCHIVE_PUBLIC", False):
            return True

        # 1. 优先校验已登录身份（已登录则直接根据角色放行，避免并发冲突与额外开销）
        user = self._current_user()
        if user is not None:
            if need_admin and user["role"] != "admin":
                if is_page:
                    self._send_html_text(
                        "权限不足",
                        f"当前账号 {user['username']}（{user['role']}）只能访问归档。",
                        403, link=("/archive", "前往消息归档"))
                else:
                    self._send_json({"ok": False, "errors": ["需要管理员权限"]}, 403)
                return False
            return True

        # 2. 未登录情况下：若用户库为空，提示初始化创建；否则重定向登录
        if not _auth.has_users():
            msg = ("账号系统已启用但还没有任何用户。请在服务器上执行："
                   "python tools/manage_users.py add <用户名>")
            if is_page:
                self._send_html_text("尚未创建账号", msg, 503)
            else:
                self._send_json({"ok": False, "errors": [msg]}, 503)
            return False

        if is_page:
            self._redirect("/login?next=" + quote(self.path, safe=""))
        else:
            self._send_json({"ok": False, "errors": ["未登录"]}, 401)
        return False

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_html_text(self, title: str, message: str, code: int = 200,
                        link: tuple[str, str] | None = None) -> None:
        """极简提示页（未登录 / 权限不足 / 未初始化）。"""
        extra = (f'<p><a href="{link[0]}">{html_escape(link[1])}</a></p>') if link else ""
        body = (
            "<style>body{font-family:system-ui,sans-serif;background:#f5f6f8;color:#1c2333;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
            "div{background:#fff;padding:32px 38px;border-radius:14px;max-width:520px;"
            "box-shadow:0 2px 12px rgba(0,0,0,.06);line-height:1.8}"
            "code{background:#f0f1f4;padding:2px 6px;border-radius:4px}"
            "a{color:#6d5bd0}</style>"
            f"<div><h2>{html_escape(title)}</h2><p>{html_escape(message)}</p>{extra}</div>"
        ).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        """管理端 API 守卫（需要 admin）。"""
        return self._guard(need_admin=True)

    def _read_body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 5 * 1024 * 1024:
            self._send_json({"ok": False, "errors": ["请求体为空或过大"]}, 400)
            return None
        data = self.rfile.read(length)
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._send_json({"ok": False, "errors": [f"JSON 解析失败: {e}"]}, 400)
            return None
        if not isinstance(obj, dict):
            self._send_json({"ok": False, "errors": ["请求体必须是 JSON 对象"]}, 400)
            return None
        return obj

    # ── 路由 ─────────────────────────────────────────────
    def _send_static(self, name: str) -> None:
        """主题 CSS / JS / 图标 —— 白名单文件名，不接受任意路径。"""
        allowed = {
            "theme.css": "text/css", "theme.js": "application/javascript",
            "archive.css": "text/css", "archive.js": "application/javascript",
            "admin_icon.svg": "image/svg+xml", "archive_icon.svg": "image/svg+xml",
        }
        ctype = allowed.get(name)
        if ctype is None:
            self._send_json({"ok": False, "errors": ["未知静态资源"]}, 404)
            return
        try:
            body = (_STATIC_PATH.parent / name).read_bytes()
        except OSError:
            self._send_json({"ok": False, "errors": ["静态资源缺失"]}, 404)
            return
        import hashlib
        etag = f'"{hashlib.md5(body).hexdigest()}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


    def _send_html(self, file_path: Path) -> None:
        try:
            body = file_path.read_bytes()
        except OSError:
            self._send_json({"ok": False, "errors": ["页面文件缺失"]}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
        if not self._check_host():
            return
        path = self.path.split("?", 1)[0]
        # 共享静态资源（主题 token / 切换脚本 / 样式 / 图标）：登录页也要用，故不设鉴权
        if path in ("/static/theme.css", "/static/theme.js",
                    "/static/archive.css", "/static/archive.js",
                    "/static/admin_icon.svg", "/static/archive_icon.svg"):
            self._send_static(path.rsplit("/", 1)[1])
            return
        if path == "/login":
            user = self._current_user()
            if user is not None:
                from urllib.parse import parse_qs, unquote
                qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                next_raw = qs.get("next", [""])[0]
                target = unquote(next_raw) if (next_raw.startswith("/") and not next_raw.startswith("//")) else ("/" if user.get("role") == "admin" else "/archive")
                if user.get("role") != "admin" and not target.startswith("/archive"):
                    target = "/archive"
                self._redirect(target)
                return
            self._send_html(_LOGIN_HTML_PATH)
            return
        if path == "/api/auth/me":
            self._handle_auth_me()
            return
        if path in ("/", "/index.html"):
            if not self._guard(need_admin=True, is_page=True):
                return
            self._send_html(_STATIC_PATH)
            return
        if path == "/archive":
            if not self._guard(need_admin=False, is_page=True):
                return
            self._send_html(_ARCHIVE_HTML_PATH)
            return
        if path.startswith("/api/archive/"):
            if not self._guard(need_admin=False):
                return
            self._handle_archive(path[len("/api/archive/"):])
            return

        if path == "/api/config":
            if not self._check_auth():
                return
            try:
                raw = _load_raw_config()
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"读取 config.json 失败: {e}"]}, 500)
                return
            self._send_json({
                "ok": True,
                "config": raw,
                "cred_status": _cred_status(raw),
                "qq_bot_status": _qq_bot_status(raw),
                "env_status": _env_status(),
                "config_path": str(CONFIG_PATH),
                "auth_required": bool(os.getenv("WEB_ADMIN_TOKEN", "")),
                "can_restart": _on_restart_cb is not None,
                "can_poll": _on_poll_cb is not None,
                "can_test_push": _on_test_push_cb is not None,
            })
            return

        if path == "/api/logs":
            if not self._check_auth():
                return
            self._handle_logs()
            return

        if path == "/api/status":
            if not self._check_auth():
                return
            self._handle_status()
            return

        if path == "/api/members":
            if not self._check_auth():
                return
            self._handle_members()
            return

        if path == "/api/users":
            if not self._check_auth():
                return
            from src import auth as _auth
            me = self._current_user() or {}
            users = [{
                "username": name,
                "role": u.get("role", "viewer"),
                "created_at": u.get("created_at", 0),
                "is_me": name == me.get("username"),
            } for name, u in sorted(_auth.load_users().items())]
            self._send_json({"ok": True, "users": users,
                             "min_password_len": _auth.MIN_PASSWORD_LEN})
            return

        if path == "/api/qq_openid/status":
            if not self._check_auth():
                return
            from src import qq_openid
            state = qq_openid.get_state()
            state["ok"] = True
            state["available"] = _on_openid_cb is not None
            self._send_json(state)
            return

        if path == "/api/social/push_targets":
            if not self._check_auth():
                return
            raw = _load_raw_config()
            targets = []

            ch = raw.get("channels") or {}
            enable_qq = ch.get("qq_official", raw.get("enable_qq_official_bot", True))
            enable_nap = ch.get("napcat", raw.get("enable_napcat_qq", False))
            enable_tg = ch.get("tg", raw.get("enable_tg_bot", False))

            # 1. QQ 官方机器人
            if enable_qq:
                for b in raw.get("qq_official_bots") or []:
                    bname = b.get("name") or b.get("app_id") or "official_bot"
                    remark = (b.get("remark") or "").strip()
                    display_base = f"{remark} ({bname})" if remark else bname
                    t_openid = (b.get("target_openid") or "").strip()
                    g_openid = (b.get("group_openid") or "").strip()
                    if t_openid:
                        targets.append({
                            "id": f"official:{bname}:private",
                            "name": f"🤖 {display_base} · 私聊 ({t_openid[:6]}...{t_openid[-4:] if len(t_openid) > 10 else ''})",
                            "channel": "qq_official",
                            "type": "private",
                        })
                    if g_openid:
                        targets.append({
                            "id": f"official:{bname}:group",
                            "name": f"👥 {display_base} · 群聊 ({g_openid[:6]}...{g_openid[-4:] if len(g_openid) > 10 else ''})",
                            "channel": "qq_official",
                            "type": "group",
                        })

            # 2. NapCat QQ
            if enable_nap:
                routes = raw.get("napcat_routes") or []
                for r in routes:
                    gid = str(r.get("group_id", "")).strip()
                    remark = (r.get("remark") or "").strip()
                    if gid:
                        display = f"{remark} ({gid})" if remark else f"QQ群 {gid}"
                        targets.append({
                            "id": f"napcat:{gid}",
                            "name": f"🐾 NapCat · {display}",
                            "channel": "napcat",
                            "type": "group",
                        })
                if not routes:
                    targets.append({
                        "id": "napcat",
                        "name": "🐾 NapCat QQ 群广播",
                        "channel": "napcat",
                        "type": "group",
                    })

            # 3. Telegram
            if enable_tg:
                tg_bots = raw.get("tg_bots") or []
                for b in tg_bots:
                    bname = b.get("name") or b.get("target_chat") or "tg_bot"
                    remark = (b.get("remark") or "").strip()
                    tchat = str(b.get("target_chat") or "").strip()
                    if remark:
                        display = f"{remark} ({bname} · {tchat})" if tchat else f"{remark} ({bname})"
                    else:
                        display = f"{bname}" + (f" ({tchat})" if tchat and tchat != bname else "")
                    targets.append({
                        "id": f"tg:{tchat or bname}",
                        "name": f"✈️ Telegram · {display}",
                        "channel": "tg",
                        "type": "chat",
                    })
                if not tg_bots:
                    targets.append({
                        "id": "tg",
                        "name": "✈️ Telegram 广播",
                        "channel": "tg",
                        "type": "chat",
                    })

            self._send_json({"ok": True, "targets": targets})
            return

        if path == "/api/config/history":
            if not self._check_auth():
                return
            from urllib.parse import parse_qs
            name = (parse_qs(self.path.partition("?")[2]).get("name") or [""])[0]
            if name:
                # 查看单份快照内容
                if not _HISTORY_NAME_RE.match(name):
                    self._send_json({"ok": False, "errors": [f"非法历史版本名: {name!r}"]}, 400)
                    return
                f = _history_dir() / name
                if not f.exists():
                    self._send_json({"ok": False, "errors": [f"历史版本不存在: {name}"]}, 404)
                    return
                self._send_json({"ok": True, "name": name, "content": f.read_text(encoding="utf-8")})
                return
            self._send_json({"ok": True, "history": list_config_history()})
            return

        if path == "/api/system/storage":
            if not self._check_auth():
                return
            from src.utils import get_storage_breakdown
            from urllib.parse import parse_qs
            qs = parse_qs(self.path.partition("?")[2])
            refresh = qs.get("refresh", ["0"])[0] in ("1", "true")
            self._send_json({"ok": True, "storage": get_storage_breakdown(force_refresh=refresh)})
        if path == "/api/subscriptions":
            if not self._check_auth():
                return
            from src.member_directory import get_all_subscriptions
            subs = get_all_subscriptions()
            self._send_json({"ok": True, "subscriptions": subs})
            return

        self._send_json({"ok": False, "errors": ["未知路径"]}, 404)

    def _handle_members(self) -> None:
        """拉取账号可见的成员目录（网页选择器用）。?account=<账号ID>"""
        from urllib.parse import parse_qs
        qs = parse_qs(self.path.partition("?")[2])
        account = (qs.get("account") or [""])[0]

        import config.config as cfg
        if account not in cfg.ACCOUNTS:
            self._send_json({"ok": False, "errors": [f"未知账号: {account!r}"]}, 400)
            return

        from config.credentials import load_all_accounts, validate_account_cred
        load_all_accounts()   # 幂等；独立模式下补加载磁盘凭证
        ok, reason = validate_account_cred(account)
        if not ok:
            self._send_json({"ok": False, "errors": [f"账号凭证不可用: {reason}"]}, 400)
            return


        import httpx

        from src.member_directory import fetch_member_directory

        async def _run():
            async with httpx.AsyncClient(timeout=20) as client:
                return await fetch_member_directory(client, account)

        try:
            members, err = asyncio.run(_run())
        except Exception as e:
            self._send_json({"ok": False, "errors": [f"拉取失败: {type(e).__name__}: {e}"]}, 500)
            return
        if err:
            self._send_json({"ok": False, "errors": [err]}, 502)
            return

        slim = []
        for m in members:
            sub = m.get("subscription")
            is_sub = False
            is_past_sub = False
            sub_state = ""
            sub_start = ""
            sub_end = ""
            sub_type = ""
            auto_renew = False
            if isinstance(sub, dict) and sub:
                sub_state = str(sub.get("state") or "").lower()
                is_sub = (sub_state == "active")
                is_past_sub = (sub_state == "expired" or (not is_sub and bool(sub_state)))
                sub_start = str(sub.get("start_at") or "")
                sub_end = str(sub.get("end_at") or "")
                sub_type = str(sub.get("type") or "")
                auto_renew = bool(sub.get("auto_renewing", False))

            slim.append({
                "id": str(m.get("id", "")),
                "name": m.get("name") or "(无名)",
                "state": m.get("state", "?"),
                "tags": [str(t) for t in (m.get("tags") or [])],
                "is_subscribed": is_sub,
                "is_past_subscribed": is_past_sub,
                "sub_state": sub_state,
                "sub_start": sub_start,
                "sub_end": sub_end,
                "sub_type": sub_type,
                "auto_renewing": auto_renew,
                "thumbnail": m.get("thumbnail") or "",
            })

        sub_count = sum(1 for x in slim if x["is_subscribed"])
        past_count = sum(1 for x in slim if x["is_past_subscribed"])
        open_count = sum(1 for x in slim if x["state"] == "open")
        self._send_json({
            "ok": True,
            "account": account,
            "total": len(slim),
            "subscribed_count": sub_count,
            "past_subscribed_count": past_count,
            "open_count": open_count,
            "members": slim,
        })

    # ── 消息归档查看器 ─────────────────────────────
    _ARCHIVE_TYPES = ("text", "picture", "image", "video", "voice")

    # ── 登录 / 登出 / 身份 ─────────────────────────
    def _handle_auth_me(self) -> None:
        import config.config as cfg
        try:
            cfg._load_env_and_json()
        except Exception:
            pass
        from src import auth as _auth
        user = self._current_user() if getattr(cfg, "AUTH_ENABLED", False) else None
        self._send_json({
            "ok": True,
            "auth_enabled": bool(getattr(cfg, "AUTH_ENABLED", False)),
            "archive_public": bool(getattr(cfg, "AUTH_ARCHIVE_PUBLIC", False)),
            "has_users": _auth.has_users(),
            "user": {"username": user["username"], "role": user["role"]} if user else None,
        })

    def _handle_login(self) -> None:
        import config.config as cfg
        from src import auth as _auth
        from src.logger import log_all
        if not getattr(cfg, "AUTH_ENABLED", False):
            self._send_json({"ok": False, "errors": ["账号系统未启用"]}, 400)
            return
        body = self._read_body_json()
        if body is None:
            return
        ip = self.client_address[0] if self.client_address else "?"
        locked = _auth.is_locked_out(ip)
        if locked > 0:
            self._send_json({"ok": False, "errors": [
                f"登录失败次数过多，请 {int(locked // 60) + 1} 分钟后再试"]}, 429)
            return

        username = str(body.get("username", ""))[:64]
        password = str(body.get("password", ""))[:256]
        user = _auth.authenticate(username, password)
        if user is None:
            _auth.record_failure(ip)
            log_all(f"🔒 网页登录失败: {username!r} 来自 {ip}", is_error=True)
            self._send_json({"ok": False, "errors": ["用户名或密码错误"]}, 401)
            return

        _auth.clear_failures(ip)
        ttl = max(1, int(getattr(cfg, "AUTH_SESSION_HOURS", 12))) * 3600
        token = _auth.create_session(user["username"], user["role"], ttl)
        log_all(f"🔓 网页登录成功: {user['username']}（{user['role']}）来自 {ip}")

        body_out = json.dumps({"ok": True, "user": user}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            f"{_SESSION_COOKIE}={token}; Path=/; Max-Age={ttl}; HttpOnly; SameSite=Strict",
        )
        self.end_headers()
        self.wfile.write(body_out)

    def _handle_logout(self) -> None:
        from src import auth as _auth
        _auth.destroy_session(self._cookie_token())
        body_out = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        self.send_header("Set-Cookie",
                         f"{_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
        # 让浏览器丢弃已缓存的归档媒体副本（localhost 视为安全上下文，该头生效）。
        # 刻意不含 "storage"：那会把 localStorage 里的主题偏好一并清掉；
        # 其中真正敏感的 webAdminToken 由前端登出逻辑单独删除。
        self.send_header("Clear-Site-Data", '"cache", "cookies"')
        self.end_headers()
        self.wfile.write(body_out)

    def _handle_openid(self, action: str) -> None:
        """openid 捕获会话：start 需要 app_id + client_secret，
        mode: 'user'（单聊）| 'group'（群聊）。
        secret 只用于本次连接，不落盘、不回显。"""
        if _on_openid_cb is None:
            self._send_json({"ok": False, "errors": [
                "独立模式下不可用（需要主程序的事件循环来跑 WebSocket 监听）"]}, 400)
            return

        def _call_openid(act: str, aid: str, sec: str, md: str = "user") -> tuple[bool, str]:
            try:
                return _on_openid_cb(act, aid, sec, md)
            except TypeError:
                return _on_openid_cb(act, aid, sec)

        if action == "stop":
            ok, msg = _call_openid("stop", "", "", "")
            self._send_json({"ok": ok, "message": msg})
            return

        body = self._read_body_json()
        if body is None:
            return
        app_id = str(body.get("app_id", "")).strip()
        secret = str(body.get("client_secret", "")).strip()
        mode = str(body.get("mode", "user")).strip() or "user"
        if not app_id:
            self._send_json({"ok": False, "errors": ["缺少 App ID"]}, 400)
            return
        if not secret:
            bot_name = str(body.get("bot_name", "")).strip().upper()
            secret = os.getenv(f"{bot_name}_CLIENT_SECRET", "") if bot_name else ""
            if not secret:
                self._send_json({"ok": False, "errors": [
                    "缺少 Client Secret（.env 里也没有该 Bot 的密钥）"]}, 400)
                return

        ok, msg = _call_openid("start", app_id, secret, mode)
        self._send_json({"ok": ok, "message": msg} if ok
                        else {"ok": False, "errors": [msg]}, 200 if ok else 409)

    def _handle_users_write(self) -> None:
        """用户管理（仅 admin）：add / passwd / role / delete。
        密码只接收不回显；关键防误锁规则在 auth 模块内统一保证。"""
        from src import auth as _auth
        from src.logger import log_all

        body = self._read_body_json()
        if body is None:
            return
        action = str(body.get("action", ""))
        username = str(body.get("username", ""))[:64]
        password = str(body.get("password", ""))[:256]
        role = str(body.get("role", "viewer"))
        me = (self._current_user() or {}).get("username", "?")

        if action == "add":
            ok, msg = _auth.add_user(username, password, role)
        elif action == "passwd":
            ok, msg = _auth.set_password(username, password)
        elif action == "role":
            ok, msg = _auth.set_role(username, role)
        elif action == "delete":
            if username == me:
                ok, msg = False, "不能删除当前登录的账号"
            else:
                ok, msg = _auth.delete_user(username)
        else:
            self._send_json({"ok": False, "errors": [f"未知操作: {action!r}"]}, 400)
            return

        if ok:
            log_all(f"👤 用户管理[{me}]: {action} {username} — {msg}")
            self._send_json({"ok": True, "message": msg})
        else:
            self._send_json({"ok": False, "errors": [msg]}, 400)

    def _handle_archive(self, sub: str) -> None:
        from urllib.parse import parse_qs, unquote
        import config.config as cfg
        from src import archive as _archive
        qs = parse_qs(self.path.partition("?")[2])

        def qp(key: str, default: str = "") -> str:
            return (qs.get(key) or [default])[0]

        if sub == "avatar":
            name = qp("name")
            group = qp("group")
            from src import avatar_manager
            rel_path = avatar_manager.get_member_avatar_path(name, group)
            if rel_path:
                full_path = Path("data/avatars") / rel_path
                if full_path.exists():
                    ext = full_path.suffix.lower()
                    ctype = "image/jpeg"
                    if ext == ".png":
                        ctype = "image/png"
                    elif ext == ".webp":
                        ctype = "image/webp"
                    try:
                        data = full_path.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "public, max-age=2592000")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    except Exception:
                        pass
            self._send_json({"ok": False, "errors": ["Avatar not found"]}, 404)
            return

        if sub == "members":
            members = []
            monitor_map = {}
            monitor_order = {}
            for idx, m in enumerate(getattr(cfg, "MONITOR_LIST", [])):
                norm = m.get("m_name", "").replace(" ", "").replace("　", "").replace("_", "")
                monitor_map[norm] = {
                    "display": m.get("m_name", ""),
                    "group": m.get("group_type", "")
                }
                monitor_order[norm] = idx

            from src import avatar_manager
            avatar_map = avatar_manager.get_member_avatar_map()

            raw_members = _archive.list_members()
            group_priority = {"nogizaka": 0, "sakurazaka": 1, "hinatazaka": 2}

            for name in raw_members:
                months = _archive.list_months(name)
                norm = name.replace(" ", "").replace("　", "").replace("_", "")
                info = monitor_map.get(norm) or {}
                display = info.get("display") or name.replace("_", " ")
                group = info.get("group") or _archive.infer_member_group(name)
                avatar = avatar_map.get(f"{group}:{norm}") or avatar_map.get(norm) or ""
                members.append({
                    "name": name,
                    "display": display,
                    "group": group,
                    "avatar": avatar,
                    "months": len(months),
                    "total": sum(m["count"] for m in months),
                    "_g_pri": group_priority.get(group, 9),
                    "_m_order": monitor_order.get(norm, 999),
                })

            members.sort(key=lambda x: (x["_g_pri"], x["_m_order"], x["display"]))
            for m_item in members:
                m_item.pop("_g_pri", None)
                m_item.pop("_m_order", None)

            self._send_json({"ok": True, "members": members})
            return

        if sub == "months":
            raw_m = qp("member")
            member = _archive.member_dir_name(raw_m) if raw_m else ""
            if not member or member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
                return
            self._send_json({"ok": True, "member": member, "months": _archive.list_months(member)})
            return

        if sub == "messages":
            raw_m = qp("member")
            member = _archive.member_dir_name(raw_m) if raw_m else ""
            if not member or member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
                return
            try:
                year, month = int(qp("year")), int(qp("month"))
                page = max(1, int(qp("page", "1")))
                per_page = min(200, max(1, int(qp("per_page", "50"))))
            except ValueError:
                self._send_json({"ok": False, "errors": ["year/month/page 必须是数字"]}, 400)
                return
            type_filter = qp("type")
            msgs = _archive.load_month(member, year, month)
            if type_filter:
                if type_filter not in self._ARCHIVE_TYPES:
                    self._send_json({"ok": False, "errors": [f"未知类型: {type_filter!r}"]}, 400)
                    return
                wanted = {"picture", "image"} if type_filter in ("picture", "image") else {type_filter}
                msgs = [m for m in msgs if m.get("type") in wanted]
            import config.config as cfg
            show_auto_tags = bool(getattr(cfg, "ENABLE_IMAGE_TAGGING", False))
            total = len(msgs)
            start = (page - 1) * per_page
            grp = _archive.infer_member_group(member)
            slim = [{
                "id": m.get("id"),
                "type": m.get("type"),
                "text": m.get("text", ""),
                "translation": m.get("_translation", ""),
                "tags": m.get("_tags", "") if show_auto_tags else "",
                "custom_tags": m.get("_custom_tags", ""),
                "published_at": m.get("published_at") or m.get("updated_at", ""),
                "upload_at": _archive.extract_upload_time(m),
                "group": grp,
                "media_url": (f"/api/archive/media/{member}/{m['_local_file']}"
                              if m.get("_local_file") else None),
                "download_failed": bool(m.get("_download_failed")),
                "w": m.get("thumbnail_width"),
                "h": m.get("thumbnail_height"),
            } for m in msgs[start:start + per_page]]
            self._send_json({
                "ok": True, "member": member, "group": grp, "year": year, "month": month,
                "total": total, "page": page,
                "total_pages": max(1, -(-total // per_page)), "messages": slim,
            })
            return

        if sub == "calendar":
            raw_m = qp("member")
            member = _archive.member_dir_name(raw_m) if raw_m else ""
            if not member or member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
                return
            type_filter = qp("type")
            wanted = None
            if type_filter:
                if type_filter not in self._ARCHIVE_TYPES:
                    self._send_json({"ok": False, "errors": [f"未知类型: {type_filter!r}"]}, 400)
                    return
                wanted = {"picture", "image"} if type_filter in ("picture", "image") else {type_filter}
            self._send_json({"ok": True, "member": member,
                             "days": _archive.day_counts(member, type_filter=wanted)})
            return

        if sub == "search":
            raw_m = qp("member")
            member = _archive.member_dir_name(raw_m) if raw_m else ""
            if not member or member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
                return
            query = qp("q").strip()
            if not query:
                self._send_json({"ok": False, "errors": ["缺少搜索关键词 q"]}, 400)
                return
            if len(query) > 100:
                self._send_json({"ok": False, "errors": ["搜索关键词不能超过 100 个字符"]}, 400)
                return
            try:
                page = max(1, int(qp("page", "1")))
                per_page = min(200, max(1, int(qp("per_page", "50"))))
            except ValueError:
                self._send_json({"ok": False, "errors": ["page 必须是数字"]}, 400)
                return
            type_filter = qp("type")
            wanted = None
            if type_filter:
                if type_filter not in self._ARCHIVE_TYPES:
                    self._send_json({"ok": False, "errors": [f"未知类型: {type_filter!r}"]}, 400)
                    return
                wanted = {"picture", "image"} if type_filter in ("picture", "image") else {type_filter}
            hits = _archive.search(member, query, type_filter=wanted)
            import config.config as cfg
            show_auto_tags = bool(getattr(cfg, "ENABLE_IMAGE_TAGGING", False))
            grp = _archive.infer_member_group(member)
            total = len(hits)
            start = (page - 1) * per_page
            slim = [{
                "id": m.get("id"),
                "type": m.get("type"),
                "text": m.get("text", ""),
                "translation": m.get("_translation", ""),
                "tags": m.get("_tags", "") if show_auto_tags else "",
                "custom_tags": m.get("_custom_tags", ""),
                "published_at": m.get("published_at") or m.get("updated_at", ""),
                "upload_at": _archive.extract_upload_time(m),
                "group": grp,
                "media_url": (f"/api/archive/media/{member}/{m['_local_file']}"
                              if m.get("_local_file") else None),
                "download_failed": bool(m.get("_download_failed")),
                "w": m.get("thumbnail_width"),
                "h": m.get("thumbnail_height"),
                "year": m.get("_year"),
                "month": m.get("_month"),
            } for m in hits[start:start + per_page]]
            self._send_json({
                "ok": True, "member": member, "group": grp, "q": query, "total": total,
                "page": page, "total_pages": max(1, -(-total // per_page)),
                "capped": total >= 500, "messages": slim,
            })
            return

        if sub == "tags":
            raw_m = qp("member")
            member = _archive.member_dir_name(raw_m) if raw_m else ""
            if not member or member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
                return
            body = self._read_body_json()
            if body is None:
                return
            msg_id = str(body.get("id") or "")
            tags = (body.get("custom_tags") or "").strip()
            year = body.get("year")
            month = body.get("month")
            if not msg_id or not year or not month:
                self._send_json({"ok": False, "errors": ["缺少 id/year/month"]}, 400)
                return
            # 只读合并 — 不碰其它字段
            msgs = _archive.load_month(member, year, month)
            found = None
            for m in msgs:
                if str(m.get("id", "")) == msg_id:
                    m["_custom_tags"] = tags
                    found = True
                    break
            if not found:
                self._send_json({"ok": False, "errors": [f"消息 {msg_id} 不存在"]}, 404)
                return
            # 写回 — 同步简化版（单字段修改可以同步，风险低）
            json_path = (_archive.archive_root() / member / f"{year:04d}" / f"{month:02d}" / "messages.json")
            tmp = json_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(msgs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, json_path)
            self._send_json({"ok": True, "id": int(msg_id), "custom_tags": tags})
            return

        if sub == "retry_download":
            raw_m = qp("member")
            member = _archive.member_dir_name(raw_m) if raw_m else ""
            if not member or member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {raw_m!r}"]}, 404)
                return
            body = self._read_body_json() or {}
            msg_id = str(body.get("id") or "")
            year = body.get("year")
            month = body.get("month")
            if not msg_id or not year or not month:
                self._send_json({"ok": False, "errors": ["缺少 id/year/month"]}, 400)
                return
            msgs = _archive.load_month(member, int(year), int(month))
            target_msg = None
            for m in msgs:
                if str(m.get("id", "")) == msg_id:
                    target_msg = m
                    break
            if not target_msg:
                self._send_json({"ok": False, "errors": [f"消息 {msg_id} 不存在"]}, 404)
                return

            file_url = target_msg.get("file") or target_msg.get("thumbnail") or ""
            if not file_url:
                self._send_json({"ok": False, "errors": ["该消息无媒体下载链接"]}, 400)
                return

            import urllib.request
            ts_str = target_msg.get("published_at") or target_msg.get("updated_at", "")
            try:
                dt = _archive.parse_jst_datetime(ts_str)
            except Exception:
                dt = datetime.now()

            dest_dir = _archive._month_dir(member, dt) / _archive._media_subdir(target_msg.get("type", ""))
            dest_dir.mkdir(parents=True, exist_ok=True)
            ts = dt.strftime("%Y%m%d_%H%M%S")
            tmp_path = dest_dir / f"{ts}_{msg_id}.tmp"

            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            candidate_urls = [u for u in [target_msg.get("file"), target_msg.get("thumbnail")] if u]
            ok = False
            used_url = file_url
            for u in candidate_urls:
                if ok:
                    break
                try:
                    req = urllib.request.Request(u, headers=headers)
                    with urllib.request.urlopen(req, timeout=30) as resp, open(tmp_path, "wb") as f:
                        if resp.status == 200:
                            f.write(resp.read())
                            if tmp_path.exists() and tmp_path.stat().st_size > 0:
                                ok = True
                                used_url = u
                                break
                except Exception:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

            if not ok:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._send_json({"ok": False, "errors": ["下载失败，该媒体资源链接可能已过期，可使用 backfill_archive.py 工具带最新 Token 回填重试"]}, 400)
                return

            ext = _archive._guess_extension(used_url, _archive._sniff_content_type(tmp_path))
            final_path = dest_dir / f"{ts}_{msg_id}{ext}"
            os.replace(tmp_path, final_path)
            rel = final_path.relative_to(_archive._member_root(member)).as_posix()

            target_msg["_local_file"] = rel
            target_msg.pop("_download_failed", None)

            json_path = (_archive.archive_root() / member / f"{int(year):04d}" / f"{int(month):02d}" / "messages.json")
            tmp = json_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(msgs, f, ensure_ascii=False, indent=2)
            os.replace(tmp, json_path)
            _archive._save_msgs_to_sqlite(member, int(year), int(month), [target_msg])

            self._send_json({"ok": True, "id": int(msg_id), "local_file": rel, "media_url": f"/api/archive/media/{member}/{rel}"})
            return

        if sub == "letters":
            if not self._guard(need_admin=True):
                return
            raw_m = qp("member")
            member = _archive.member_dir_name(raw_m) if raw_m else ""
            if not member:
                self._send_json({"ok": False, "errors": ["缺少成员参数 member"]}, 400)
                return
            letters = _archive.get_archive_letters(member)
            grp = _archive.infer_member_group(member)
            slim = []
            for item in letters:
                loc = item.get("local_file") or ""
                if loc:
                    if loc.startswith(member + "/"):
                        rel_path = loc[len(member) + 1:]
                    else:
                        rel_path = loc
                    media_url = f"/api/archive/media/{member}/{rel_path}"
                else:
                    media_url = None

                slim.append({
                    "id": item.get("id"),
                    "group_id": item.get("group_id"),
                    "member_name": item.get("member_name"),
                    "member_dir": item.get("member_dir"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "text": item.get("text", ""),
                    "file_url": item.get("file_url"),
                    "media_url": media_url,
                    "thumbnail_url": item.get("thumbnail_url"),
                    "is_favorite": bool(item.get("is_favorite")),
                })
            self._send_json({
                "ok": True,
                "member": member,
                "group": grp,
                "total": len(slim),
                "letters": slim
            })
            return

        if sub == "letters_sync":
            if not self._guard(need_admin=True):
                return
            raw_m = qp("member")
            if not raw_m:
                content_len = int(self.headers.get("Content-Length") or 0)
                if content_len > 0:
                    body = self._read_body_json()
                    if body and isinstance(body, dict):
                        raw_m = body.get("member")
            if not raw_m:
                self._send_json({"ok": False, "errors": ["缺少成员参数 member"]}, 400)
                return
            member = _archive.member_dir_name(raw_m)
            target_mem = None
            norm_raw = raw_m.replace(" ", "").replace("　", "").replace("_", "").lower()
            for m in getattr(cfg, "MONITOR_LIST", []):
                m_name = m.get("m_name") or m.get("name", "")
                norm_m = m_name.replace(" ", "").replace("　", "").replace("_", "").lower()
                if norm_m == norm_raw or _archive.member_dir_name(m_name) == member:
                    target_mem = m
                    break
            if not target_mem:
                target_mem = {"name": raw_m, "m_name": raw_m}

            import tools.archive_letters as _al
            import httpx

            async def _do_sync():
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    _archive.initialize(client)
                    return await _al.sync_letters_for_member(target_mem, client)

            try:
                import asyncio
                import concurrent.futures
                try:
                    _loop = asyncio.get_running_loop()
                except RuntimeError:
                    _loop = None

                if _loop and _loop.is_running():
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                        tot, nw = _pool.submit(asyncio.run, _do_sync()).result(timeout=60)
                else:
                    tot, nw = asyncio.run(_do_sync())
                self._send_json({"ok": True, "member": member, "total": tot, "new": nw, "count": tot})
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"同步信件异常: {e}"]}, 500)
            return

        if sub == "home":
            # ── 缓存：基于 archive.db 与 blogs.db 的 mtime + 日期，跨天自动失效 ──
            global _home_cache, _home_cache_key
            try:
                db_mtime = _archive.get_db_path().stat().st_mtime
            except OSError:
                db_mtime = 0
            try:
                blog_mtime = Path("data/archive/blogs.db").stat().st_mtime
            except OSError:
                blog_mtime = 0
            today_str = datetime.now().strftime("%Y-%m-%d")
            cache_key = (db_mtime, blog_mtime, today_str)
            if _home_cache is not None and _home_cache_key == cache_key:
                self._send_json(_home_cache)
                return

            random.seed(today_str)

            monitor_names = {}
            for m in getattr(cfg, "MONITOR_LIST", []):
                norm = m.get("m_name", "").replace(" ", "").replace("　", "").replace("_", "")
                monitor_names[norm] = m.get("m_name", "")

            db = _archive.init_db()

            from src import avatar_manager
            avatar_map = avatar_manager.get_member_avatar_map()

            # ── 1. Message 归档成员统计（纯 SQL 极速查询，不解析多余 JSON）──
            members = []
            today_msg_cnt = 0
            this_week_msgs = 0
            last_week_msgs = 0

            if db:
                try:
                    r_td = db.execute("SELECT COUNT(*) FROM messages WHERE published_at LIKE ? OR updated_at LIKE ?", (f"{today_str}%", f"{today_str}%")).fetchone()
                    today_msg_cnt = r_td[0] if r_td else 0

                    now_dt = datetime.now()
                    w0 = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")
                    w1 = (now_dt - timedelta(days=14)).strftime("%Y-%m-%d")
                    r_this = db.execute("SELECT COUNT(*) FROM messages WHERE published_at >= ?", (w0,)).fetchone()
                    this_week_msgs = r_this[0] if r_this else 0
                    r_last = db.execute("SELECT COUNT(*) FROM messages WHERE published_at >= ? AND published_at < ?", (w1, w0)).fetchone()
                    last_week_msgs = r_last[0] if r_last else 0
                except Exception:
                    pass

            for name in _archive.list_members():
                months = _archive.list_months(name)
                total = sum(m["count"] for m in months)
                latest_msgs: list[dict] = []
                type_counts: dict[str, int] = {}

                if db:
                    try:
                        # 极速 SQL 聚合分类计数 (0.1ms)
                        for r_tc in db.execute("SELECT type, COUNT(*) FROM messages WHERE member_dir = ? GROUP BY type", (name,)).fetchall():
                            mtype = r_tc[0] or "text"
                            type_counts[mtype] = r_tc[1]
                        
                        # 极速 SQL 获取最新 8 条文本消息 (0.1ms)
                        for lm in db.execute("""
                            SELECT id, text, translation, published_at, updated_at
                            FROM messages
                            WHERE member_dir = ? AND type = 'text' AND text IS NOT NULL AND trim(text) != ''
                            ORDER BY published_at DESC LIMIT 8
                        """, (name,)).fetchall():
                            latest_msgs.append({
                                "id": lm[0],
                                "text": lm[1] or "",
                                "translation": lm[2] or "",
                                "published_at": lm[3] or lm[4] or "",
                            })
                    except Exception:
                        pass

                monthly = [{"year": mo["year"], "month": mo["month"], "count": mo["count"]} for mo in months[:24]]

                first_date, last_date = "", ""
                if months:
                    last = months[0]
                    first = months[-1]
                    first_date = f"{first['year']:04d}/{first['month']:02d}"
                    last_date = f"{last['year']:04d}/{last['month']:02d}"

                stats = {
                    "total": total,
                    "months": len(months),
                    "pictures": type_counts.get("picture", 0) + type_counts.get("image", 0),
                    "videos": type_counts.get("video", 0),
                    "voices": type_counts.get("voice", 0),
                    "texts": type_counts.get("text", 0),
                    "first_date": first_date,
                    "last_date": last_date,
                }
                if months:
                    stats["this_month"] = months[0]["count"]

                norm = name.replace(" ", "").replace("　", "").replace("_", "")
                display = monitor_names.get(norm) or name.replace("_", " ")
                group = _archive.infer_member_group(name)
                avatar = avatar_map.get(f"{group}:{norm}") or avatar_map.get(norm) or ""
                members.append({
                    "name": name,
                    "display": display,
                    "group": group,
                    "avatar": avatar,
                    "stats": stats,
                    "monthly": monthly,
                    "days": {},
                    "latest_msgs": latest_msgs,
                })

            # ── 2. Blog 博客全量统计 ──
            GROUP_INFO = {
                "nogizaka": {"name": "乃木坂46", "icon": "💜", "color": "#8b5cf6"},
                "sakurazaka": {"name": "樱坂46", "icon": "🌸", "color": "#ec4899"},
                "hinatazaka": {"name": "日向坂46", "icon": "🩵", "color": "#06b6d4"},
            }
            blog_groups = []
            total_blogs = 0
            total_blog_authors = 0
            recent_blogs = []
            blog_pics = []
            rand_blog_msgs = []
            today_blog_cnt = 0
            blog_this_week = 0

            def _encode_blog_media_url(rel_path: str) -> str:
                if not rel_path:
                    return ""
                from urllib.parse import quote
                parts = rel_path.replace("\\", "/").strip("/").split("/")
                encoded_parts = [quote(p) for p in parts]
                return "/api/archive/blog_media/" + "/".join(encoded_parts)

            blog_db = _get_blog_db()
            if blog_db:
                try:
                    total_blogs = blog_db.execute("SELECT COUNT(*) FROM blog_posts").fetchone()[0]
                    total_blog_authors = blog_db.execute("SELECT COUNT(DISTINCT author) FROM blog_posts").fetchone()[0]

                    for gkey, gmeta in GROUP_INFO.items():
                        row = blog_db.execute("""
                            SELECT COUNT(*), COUNT(DISTINCT author), MIN(date), MAX(date)
                            FROM blog_posts WHERE group_key=?
                        """, (gkey,)).fetchone()
                        count = row[0] if row else 0
                        if count > 0:
                            lp_row = blog_db.execute("""
                                SELECT id, author, title, date, body_text, images_json, image_paths_json
                                FROM blog_posts WHERE group_key=?
                                ORDER BY date DESC LIMIT 1
                            """, (gkey,)).fetchone()
                            latest_post = None
                            if lp_row:
                                lp = dict(lp_row)
                                imgs = json.loads(lp.get("image_paths_json") or "[]")
                                first_img = imgs[0].replace("\\", "/") if imgs and imgs[0] else ""
                                cover = _encode_blog_media_url(first_img) if first_img else ""
                                latest_post = {
                                    "id": lp["id"],
                                    "author": lp["author"],
                                    "title": lp["title"],
                                    "date": lp["date"],
                                    "cover": cover,
                                }
                            blog_groups.append({
                                "key": gkey,
                                "name": gmeta["name"],
                                "icon": gmeta["icon"],
                                "color": gmeta["color"],
                                "total": count,
                                "author_count": row[1],
                                "first_date": (row[2] or "")[:7].replace("-", "/"),
                                "last_date": (row[3] or "")[:7].replace("-", "/"),
                                "latest_post": latest_post,
                            })

                    # 最近博客列表
                    for r in blog_db.execute("""
                        SELECT id, group_key, author, title, date, body_text, images_json, image_paths_json
                        FROM blog_posts
                        ORDER BY date DESC LIMIT 6
                    """).fetchall():
                        bp = dict(r)
                        imgs = json.loads(bp.get("image_paths_json") or "[]")
                        first_img = imgs[0].replace("\\", "/") if imgs and imgs[0] else ""
                        cover = _encode_blog_media_url(first_img) if first_img else ""
                        gname = GROUP_INFO.get(bp["group_key"], {}).get("name", bp["group_key"])
                        gicon = GROUP_INFO.get(bp["group_key"], {}).get("icon", "📝")
                        recent_blogs.append({
                            "type": "blog",
                            "id": bp["id"],
                            "group_key": bp["group_key"],
                            "group_name": gname,
                            "group_icon": gicon,
                            "author": bp["author"],
                            "title": bp["title"],
                            "date": bp["date"],
                            "cover": cover,
                            "has_images": len(imgs) > 0,
                        })
                        if cover:
                            blog_pics.append({
                                "type": "blog",
                                "id": bp["id"],
                                "group_key": bp["group_key"],
                                "member": bp["author"],
                                "member_display": f"{gicon} {gname} · {bp['author']}",
                                "text": bp["title"],
                                "url": cover,
                                "published_at": bp["date"],
                                "year": int(bp["date"][:4]) if len(bp["date"]) >= 4 and bp["date"][:4].isdigit() else 2026,
                                "month": int(bp["date"][5:7]) if len(bp["date"]) >= 7 and bp["date"][5:7].isdigit() else 8,
                            })

                    # 今日与本周博客统计（极速 SQL 范围查询）
                    r_b_td = blog_db.execute("SELECT COUNT(*) FROM blog_posts WHERE date LIKE ?", (f"{today_str}%",)).fetchone()
                    today_blog_cnt = r_b_td[0] if r_b_td else 0
                    week_ago_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                    r_b_wk = blog_db.execute("SELECT COUNT(*) FROM blog_posts WHERE date >= ?", (week_ago_str,)).fetchone()
                    blog_this_week = r_b_wk[0] if r_b_wk else 0

                    # 博客时光隧道：随机抽取 3 篇经典博文
                    rand_blog_rows = blog_db.execute("""
                        SELECT id, group_key, author, title, date, body_text
                        FROM blog_posts
                        ORDER BY RANDOM() LIMIT 3
                    """).fetchall()
                    for r in rand_blog_rows:
                        bp = dict(r)
                        gname = GROUP_INFO.get(bp["group_key"], {}).get("name", bp["group_key"])
                        gicon = GROUP_INFO.get(bp["group_key"], {}).get("icon", "📝")
                        raw_body = bp.get("body_text") or ""
                        clean_body = re.sub(r'[\r\n\s]+', ' ', raw_body).strip()
                        preview = clean_body[:120] + ("..." if len(clean_body) > 120 else "")
                        rand_blog_msgs.append({
                            "type": "blog",
                            "id": bp["id"],
                            "group_key": bp["group_key"],
                            "member_display": f"{gicon} {gname} · {bp['author']}",
                            "text": bp["title"],
                            "translation": preview,
                            "published_at": bp["date"],
                        })
                except Exception:
                    pass

            # ── 3. 聚合写真画廊（Message 写真 + 博客精选图）──
            def _ym(utc_str: str) -> tuple[int, int]:
                try:
                    return (int(utc_str[:4]), int(utc_str[5:7]))
                except (ValueError, IndexError):
                    return (2026, 1)

            msg_pics = []
            if db:
                try:
                    recent_pic_rows = db.execute("""
                        SELECT id, member_name, text, local_file, published_at, updated_at, raw_json
                        FROM messages
                        WHERE type IN ('picture','image') AND local_file IS NOT NULL AND local_file != ''
                        ORDER BY published_at DESC LIMIT 8
                    """).fetchall()
                    rand_pic_rows = db.execute("""
                        SELECT id, member_name, text, local_file, published_at, updated_at, raw_json
                        FROM messages
                        WHERE type IN ('picture','image') AND local_file IS NOT NULL AND local_file != ''
                        ORDER BY RANDOM() LIMIT 4
                    """).fetchall()
                    seen_p_ids = set()
                    for row in (recent_pic_rows + rand_pic_rows):
                        if row[0] in seen_p_ids:
                            continue
                        seen_p_ids.add(row[0])
                        rj = json.loads(row[6]) if row[6] else {}
                        pub = row[4] or row[5] or ""
                        norm_m = row[1].replace(" ", "").replace("　", "").replace("_", "")
                        disp = monitor_names.get(norm_m) or row[1].replace("_", " ")
                        canonical_m = _archive.member_dir_name(row[1])
                        msg_pics.append({
                            "type": "msg",
                            "member": canonical_m, "member_display": disp,
                            "id": row[0], "text": row[2] or "",
                            "url": f"/api/archive/media/{canonical_m}/{row[3]}",
                            "w": rj.get("thumbnail_width"), "h": rj.get("thumbnail_height"),
                            "published_at": pub,
                            "year": _ym(pub)[0], "month": _ym(pub)[1],
                        })
                except Exception:
                    pass

            # 综合写真画廊：Message 精选图 + Blog 插图混合
            agg_pics = sorted(msg_pics + blog_pics[:6], key=lambda x: x.get("published_at", ""), reverse=True)

            # ── 4. 全站最新动态流（Message 消息 + Blog 博文混合）──
            agg_msgs = []
            for m in members:
                for msg in m["latest_msgs"][:4]:
                    agg_msgs.append({
                        "type": "msg",
                        "member": m["name"],
                        "member_display": m["display"],
                        "id": msg["id"],
                        "text": msg["text"],
                        "translation": msg.get("translation", ""),
                        "published_at": msg.get("published_at", ""),
                        "year": _ym(msg.get("published_at", ""))[0],
                        "month": _ym(msg.get("published_at", ""))[1],
                    })

            for b in recent_blogs[:4]:
                agg_msgs.append({
                    "type": "blog",
                    "group_key": b["group_key"],
                    "group_name": b["group_name"],
                    "group_icon": b["group_icon"],
                    "author": b["author"],
                    "member_display": f"{b['group_icon']} {b['group_name']} · {b['author']}",
                    "id": b["id"],
                    "text": b["title"],
                    "translation": "",
                    "cover": b.get("cover", ""),
                    "published_at": b["date"],
                    "year": int(b["date"][:4]) if len(b["date"]) >= 4 and b["date"][:4].isdigit() else 2026,
                    "month": int(b["date"][5:7]) if len(b["date"]) >= 7 and b["date"][5:7].isdigit() else 8,
                })

            recent_feed = sorted(agg_msgs, key=lambda x: x.get("published_at", ""), reverse=True)[:8]

            # ── 5. 全站时光隧道（随机 Message 经典记录 + 随机 Blog 经典）──
            rand_msgs = []
            if db:
                try:
                    rand_txt_rows = db.execute("""
                        SELECT id, member_name, text, translation, published_at, updated_at
                        FROM messages
                        WHERE type='text' AND text IS NOT NULL AND trim(text)!=''
                        ORDER BY RANDOM() LIMIT 4
                    """).fetchall()
                    for r in rand_txt_rows:
                        pub = r[4] or r[5] or ""
                        norm_r = r[1].replace(" ", "").replace("　", "").replace("_", "")
                        disp_r = monitor_names.get(norm_r) or r[1].replace("_", " ")
                        canonical_r = _archive.member_dir_name(r[1])
                        rand_msgs.append({
                            "type": "msg",
                            "member": canonical_r, "member_display": disp_r,
                            "id": r[0], "text": r[2] or "", "translation": r[3] or "",
                            "published_at": pub,
                            "year": _ym(pub)[0], "month": _ym(pub)[1],
                        })
                except Exception:
                    pass

            time_tunnel = sorted(rand_msgs + rand_blog_msgs, key=lambda x: x.get("published_at", ""), reverse=True)[:6]

            # ── 6. 综合统计概览 ──
            total_messages = sum(m["stats"]["total"] for m in members)
            first_dates = [m["stats"]["first_date"] for m in members if m["stats"]["first_date"]] + [g["first_date"] for g in blog_groups if g["first_date"]]
            last_dates = [m["stats"]["last_date"] for m in members if m["stats"]["last_date"]] + [g["last_date"] for g in blog_groups if g["last_date"]]

            agg_first = min(first_dates) if first_dates else ""
            agg_last = max(last_dates) if last_dates else ""

            last_pub = max((p.get("published_at", "") for p in agg_pics), default="")
            last_feed = max((f.get("published_at", "") for f in recent_feed), default="")
            last_updated = max(last_pub, last_feed)

            summary = {
                "total_messages": total_messages,
                "total_blogs": total_blogs,
                "total_all": total_messages + total_blogs,
                "member_count": len(members),
                "blog_group_count": len(blog_groups),
                "blog_author_count": total_blog_authors,
                "first_date": agg_first,
                "last_date": agg_last,
                "last_updated": last_updated,
                "today_stats": {
                    "messages": today_msg_cnt,
                    "blogs": today_blog_cnt,
                    "total": today_msg_cnt + today_blog_cnt,
                },
                "week_stats": {
                    "this_week": this_week_msgs + blog_this_week,
                    "last_week": last_week_msgs,
                    "messages_week": this_week_msgs,
                    "blogs_week": blog_this_week,
                },
            }

            result = {
                "ok": True,
                "summary": summary,
                "members": members,
                "blog_groups": blog_groups,
                "recent_pics": agg_pics,
                "recent_feed": recent_feed,
                "time_tunnel": time_tunnel,
            }
            _home_cache = result
            _home_cache_key = cache_key
            self._send_json(result)
            return

        # ── 博客归档 API ──
        if sub == "blog_groups":
            groups = []
            try:
                db = _get_blog_db()
                for r in db.execute("""
                    SELECT group_key, COUNT(*), MIN(date), MAX(date)
                    FROM blog_posts GROUP BY group_key ORDER BY group_key
                """).fetchall():
                    groups.append({
                        "key": r[0], "total": r[1],
                        "first_date": r[2] or "", "last_date": r[3] or "",
                    })
            except Exception:
                pass
            self._send_json({"ok": True, "groups": groups})
            return

        if sub == "blog_calendar":
            qs = self._query_params()
            group = qs.get("group", "hinatazaka")
            author = qs.get("author", "")
            days = {}
            try:
                db = _get_blog_db()
                where = "WHERE group_key=?"
                params = [group]
                if author:
                    norm_author = author.replace(" ", "").replace("　", "").replace("_", "")
                    where += " AND REPLACE(REPLACE(REPLACE(author, ' ', ''), '　', ''), '_', '') = ?"
                    params.append(norm_author)
                for r in db.execute(f"""
                    SELECT substr(date,1,10) as d, COUNT(*)
                    FROM blog_posts {where}
                    GROUP BY d
                """, params).fetchall():
                    if r[0]:
                        days[r[0]] = r[1]
            except Exception:
                pass
            self._send_json({"ok": True, "group": group, "days": days})
            return

        if sub == "blog_authors":
            qs = self._query_params()
            group = qs.get("group", "hinatazaka")
            authors = []
            try:
                db = _get_blog_db()
                from src import avatar_manager
                from src.sakamichi_roster import get_author_sort_tuple
                avatar_map = avatar_manager.get_member_avatar_map()
                raw_authors = db.execute("""
                    SELECT author, COUNT(*)
                    FROM blog_posts WHERE group_key=? AND author != '' AND author IS NOT NULL
                    GROUP BY author
                """, (group,)).fetchall()
                for r in raw_authors:
                    if r[0] and str(r[0]).strip():
                        a_name = str(r[0]).strip()
                        norm_a = a_name.replace(" ", "").replace("　", "").replace("_", "")
                        avatar = avatar_map.get(f"{group}:{norm_a}") or avatar_map.get(norm_a) or ""
                        sort_key = get_author_sort_tuple(group, a_name)
                        authors.append({
                            "name": a_name,
                            "total": r[1],
                            "avatar": avatar,
                            "_sort": sort_key,
                        })
                # 按 期别 - 期别整体账号 - staff（整体按五十音）精准排序
                authors.sort(key=lambda x: x["_sort"])
                for a_item in authors:
                    a_item.pop("_sort", None)
            except Exception:
                pass
            self._send_json({"ok": True, "group": group, "authors": authors})
            return

        if sub == "blogs":
            qs = self._query_params()
            blog_id = qs.get("id")
            if blog_id:
                try:
                    db = _get_blog_db()
                    r = db.execute("SELECT * FROM blog_posts WHERE id=?", (blog_id,)).fetchone()
                    if r:
                        d = dict(r)
                        d["images_json"] = d.get("images_json") or "[]"
                        d["image_paths_json"] = d.get("image_paths_json") or "[]"
                        self._send_json({"ok": True, "post": d})
                        return
                    else:
                        self._send_json({"ok": False, "errors": ["博客不存在"]}, 404)
                        return
                except Exception as e:
                    self._send_json({"ok": False, "errors": [str(e)]}, 500)
                    return

            group = qs.get("group", "hinatazaka")
            author = qs.get("author", "")
            date_filter = qs.get("date", "")
            year = int(qs.get("year", "0") or "0")
            month = int(qs.get("month", "0") or "0")
            page = max(1, int(qs.get("page", "1") or "1"))
            per_page = min(100, max(1, int(qs.get("per_page", "30") or "30")))
            q = qs.get("q", "")
            posts = []
            total = 0
            try:
                db = _get_blog_db()
                where = "WHERE group_key=?"
                params: list = [group]
                if author:
                    norm_author = author.replace(" ", "").replace("　", "").replace("_", "")
                    where += " AND REPLACE(REPLACE(REPLACE(author, ' ', ''), '　', ''), '_', '') = ?"
                    params.append(norm_author)
                if date_filter:
                    where += " AND substr(date,1,10)=?"
                    params.append(date_filter)
                elif year and month:
                    where += " AND substr(date,1,7)=?"
                    params.append(f"{year:04d}-{month:02d}")
                if q:
                    where += " AND (title LIKE ? OR body_text LIKE ? OR translation LIKE ?)"
                    q_like = f"%{q}%"
                    params.extend([q_like, q_like, q_like])
                total = db.execute(
                    f"SELECT COUNT(*) FROM blog_posts {where}", params).fetchone()[0]

                # 计算分页与偏移：
                # 默认首页展示模式（无关键词搜索且无日期筛选）：第1页包含 1 张 Hero 顶置大卡片 + 24 张完整网格 (共 25 篇，满 6 行 × 4 列无缺口)
                # 第 2 页及之后为标准的 24 篇网格 (6 行 × 4 列)
                has_hero_mode = (not q and not date_filter and not (year and month))
                if has_hero_mode:
                    if page == 1:
                        limit = 25
                        offset = 0
                    else:
                        limit = 24
                        offset = 25 + (page - 2) * 24
                    
                    if total <= 25:
                        total_pages = 1
                    else:
                        total_pages = 1 + (total - 25 + 24 - 1) // 24
                else:
                    limit = per_page
                    offset = (page - 1) * per_page
                    total_pages = max(1, (total + per_page - 1) // per_page)

                rows = db.execute(
                    f"SELECT * FROM blog_posts {where} ORDER BY date DESC LIMIT ? OFFSET ?",
                    params + [limit, offset],
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    d["images_json"] = d.get("images_json") or "[]"
                    paths_str = d.get("image_paths_json") or "[]"
                    try:
                        images = json.loads(d["images_json"])
                        paths = json.loads(paths_str)
                        if images:
                            while len(paths) < len(images):
                                paths.append("")
                            dirty = False
                            img_root = Path("data/blog_images")
                            for i, img_url in enumerate(images):
                                if not paths[i] or not (img_root / paths[i]).exists():
                                    safe_title = re.sub(r'[\\/:*?"<>|]', '', d.get("title", ""))[:50].strip()
                                    safe_author = re.sub(r'[\\/:*?"<>|]', '', d.get("author", ""))[:20].strip()
                                    ts = (d.get("date") or "").replace("/", "").replace(" ", "_").replace(":", "")
                                    safe_ts = re.sub(r'[^0-9_]', '', ts)[:15]
                                    ext = img_url.rsplit(".", 1)[-1].split("?")[0].lower()
                                    if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
                                        ext = "jpg"
                                    fname = f"{i+1:02d}.{ext}"
                                    cand = img_root / d.get("group_key", "") / safe_author / f"{safe_title}-{safe_ts}" / fname
                                    if cand.exists():
                                        paths[i] = str(cand.relative_to(img_root))
                                        dirty = True
                            if dirty:
                                paths_str = json.dumps(paths, ensure_ascii=False)
                                try:
                                    db.execute("UPDATE blog_posts SET image_paths_json = ? WHERE id = ?", (paths_str, d["id"]))
                                    db.commit()
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    d["image_paths_json"] = paths_str
                    d["content_json"] = d.get("content_json") or "[]"
                    d["translation_model"] = d.get("translation_model") or ""
                    posts.append(d)
            except Exception:
                pass
            self._send_json({
                "ok": True, "group": group, "posts": posts,
                "total": total, "page": page, "total_pages": total_pages,
            })
            return

        if sub == "blogs/translate":
            if getattr(cfg, "AUTH_ENABLED", False):
                user = self._current_user()
                if not user or user.get("role") != "admin":
                    self._send_json({"ok": False, "msg": "需要管理员权限方可使用翻译功能"}, 401)
                    return

            if self.command != "POST":
                self._send_json({"ok": False, "msg": "Method not allowed"}, 405)
                return
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._send_json({"ok": False, "msg": "Missing body"}, 400)
                return
            body_data = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body_data)
                blog_id = int(data.get("id", 0))
            except json.JSONDecodeError:
                self._send_json({"ok": False, "msg": "Invalid JSON"}, 400)
                return
                
            if not blog_id:
                self._send_json({"ok": False, "msg": "无效参数"})
                return

            try:
                db = _get_blog_db()
                row = db.execute("SELECT * FROM blog_posts WHERE id = ?", (blog_id,)).fetchone()
                if not row:
                    self._send_json({"ok": False, "msg": "未找到该博客"})
                    return
                row = dict(row)

                if row.get("content_json") and row["content_json"] != "[]":
                    self._send_json({"ok": True, "html": row.get("translation", ""), "content_json": row["content_json"], "translation_model": row.get("translation_model") or ""})
                    return

                import asyncio
                import httpx
                from src import translator
                from src.logger import log_all

                log_all(f"🔄 网页端请求手动翻译博客: {row['author']} - {row.get('title', '')}")

                async def _do_translate():
                    async with httpx.AsyncClient(timeout=120) as temp_client:
                        return await translator.translate_blog_structured(
                            row["body_html"], row["author"], row["group_key"], custom_client=temp_client
                        )

                structured, model_name = asyncio.run(_do_translate())
                if structured:
                    translated = translator.blocks_to_html(structured)
                    content_json = json.dumps(structured, ensure_ascii=False)
                    translation_model = model_name or ""
                    log_all(f"✅ 网页端手动翻译完成: {row['author']} - {row.get('title', '')}（模型: {translation_model}）")
                    db.execute("UPDATE blog_posts SET translation = ?, content_json = ?, translation_model = ? WHERE id = ?", (translated, content_json, translation_model, blog_id))
                    db.commit()
                    self._send_json({"ok": True, "html": translated, "content_json": content_json, "translation_model": translation_model})
                else:
                    log_all(f"⚠️ 网页端手动翻译失败: {row['author']} - {row.get('title', '')}", is_error=True)
                    self._send_json({"ok": False, "msg": "翻译失败，请稍后重试"})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as e:
                import traceback
                with open("logs/ui_error.txt", "w", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
                self._send_json({"ok": False, "msg": f"异常: {e}"})
            return

        if sub == "blogs/archive_member":
            if getattr(cfg, "AUTH_ENABLED", False):
                user = self._current_user()
                if not user or user.get("role") != "admin":
                    self._send_json({"ok": False, "msg": "需要管理员权限方可操作"}, 401)
                    return

            if self.command != "POST":
                self._send_json({"ok": False, "msg": "Method not allowed"}, 405)
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}

            target_url = payload.get("url", "").strip()
            translate = bool(payload.get("translate", False))
            
            if not target_url:
                self._send_json({"ok": False, "msg": "请输入有效的成员博客 URL"})
                return

            import subprocess
            cmd = [sys.executable, str(_BASE_DIR / "tools" / "archive_member.py"), target_url]
            if translate:
                cmd.append("--translate")

            try:
                subprocess.Popen(cmd, cwd=str(_BASE_DIR))
                self._send_json({"ok": True, "msg": "已成功启动后台博客归档任务！可在终端或日志中查看进度。"})
            except Exception as e:
                self._send_json({"ok": False, "msg": f"启动归档任务失败: {e}"})
            return

        if sub == "messages/backfill":
            if getattr(cfg, "AUTH_ENABLED", False):
                user = self._current_user()
                if not user or user.get("role") != "admin":
                    self._send_json({"ok": False, "msg": "需要管理员权限方可操作"}, 401)
                    return

            if self.command != "POST":
                self._send_json({"ok": False, "msg": "Method not allowed"}, 405)
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = {}

            member_name = payload.get("member", "").strip()
            
            import subprocess
            cmd = [sys.executable, str(_BASE_DIR / "tools" / "backfill_archive.py"), "--force"]
            if member_name:
                cmd.append(member_name)

            try:
                subprocess.Popen(cmd, cwd=str(_BASE_DIR))
                msg_target = f"【{member_name}】" if member_name else "【全部监控成员】"
                self._send_json({"ok": True, "msg": f"已成功启动 {msg_target} 的历史消息回填任务！"})
            except Exception as e:
                self._send_json({"ok": False, "msg": f"启动消息回填失败: {e}"})
            return

        if sub == "blogs/delete_translation":
            if getattr(cfg, "AUTH_ENABLED", False):
                user = self._current_user()
                if not user or user.get("role") != "admin":
                    self._send_json({"ok": False, "msg": "需要管理员权限方可操作"}, 401)
                    return

            if self.command != "POST":
                self._send_json({"ok": False, "msg": "Method not allowed"}, 405)
                return

            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._send_json({"ok": False, "msg": "Missing body"}, 400)
                return

            body_data = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body_data)
                blog_id = int(data.get("id", 0))
            except json.JSONDecodeError:
                self._send_json({"ok": False, "msg": "Invalid JSON"}, 400)
                return

            if not blog_id:
                self._send_json({"ok": False, "msg": "无效参数"})
                return

            try:
                db = _get_blog_db()
                db.execute("UPDATE blog_posts SET translation = NULL, content_json = NULL, translation_model = NULL WHERE id = ?", (blog_id,))
                db.commit()
                from src.logger import log_all
                log_all(f"🗑️ 管理员删除了博客 (ID: {blog_id}) 的翻译缓存")
                self._send_json({"ok": True, "msg": "已清除该博客的翻译"})
            except Exception as e:
                self._send_json({"ok": False, "msg": f"异常: {e}"})
            return

        if sub.startswith("blog_media/"):
            rel_str = unquote(sub[len("blog_media/"):].replace("\\", "/"))
            rel = Path(rel_str)
            full = (BLOG_IMAGE_DIR / rel).resolve()
            if BLOG_IMAGE_DIR.resolve() not in full.parents and full != BLOG_IMAGE_DIR.resolve():
                self._send_json({"ok": False, "errors": ["非法路径"]}, 403)
                return
            if not full.is_file():
                self._send_json({"ok": False, "errors": ["媒体不存在"]}, 404)
                return
            self._serve_file_range(full)
            return

        if sub.startswith("media/"):
            rest = unquote(sub[len("media/"):])
            raw_member, _, rel = rest.partition("/")
            member = _archive.member_dir_name(raw_member) if raw_member else ""
            if not member or member not in _archive.list_members() or not rel:
                self._send_json({"ok": False, "errors": ["媒体不存在"]}, 404)
                return
            member_root = (_archive.archive_root() / member).resolve()
            full = (member_root / rel).resolve()
            if member_root not in full.parents:
                self._send_json({"ok": False, "errors": ["非法路径"]}, 403)
                return
            if not full.is_file():
                self._send_json({"ok": False, "errors": ["媒体不存在"]}, 404)
                return
            self._serve_file_range(full)
            return

        self._send_json({"ok": False, "errors": ["未知路径"]}, 404)

    def _serve_file_range(self, path: Path) -> None:
        """媒体文件服务，支持 HTTP Range（视频/音频拖进度条必需）。

        缓存策略用 private + no-cache：浏览器可以存副本，但每次使用前必须回源
        验证（带 ETag 命中则 304，几乎零流量）。绝不能用 max-age —— 那会让
        登出后的浏览器直接从本地缓存渲染私密图片，绕过鉴权。
        """
        import mimetypes
        from email.utils import formatdate, parsedate_to_datetime
        st = path.stat()
        size = st.st_size
        etag = f'"{int(st.st_mtime)}-{size:x}"'
        last_modified = formatdate(st.st_mtime, usegmt=True)
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

        # 条件请求（无 Range 时才处理 304）
        if not self.headers.get("Range"):
            fresh = False
            inm = self.headers.get("If-None-Match", "")
            if inm:
                fresh = any(t.strip().lstrip("W/") == etag for t in inm.split(","))
            elif self.headers.get("If-Modified-Since"):
                try:
                    since = parsedate_to_datetime(self.headers["If-Modified-Since"]).timestamp()
                    fresh = int(st.st_mtime) <= int(since)
                except (TypeError, ValueError):
                    fresh = False
            if fresh:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "private, no-cache")
                self.end_headers()
                return

        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        # private: 禁止中间代理缓存；no-cache: 每次使用前必须回源鉴权
        self.send_header("Cache-Control", "private, no-cache")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionError, OSError):
            pass   # 播放器中断连接是常态

    def _handle_status(self) -> None:
        """运行状态快照：健康追踪数据 + 各账号实时 Token 剩余时间。"""
        from src.health import get_tracker
        snap = get_tracker().snapshot()

        # Token 实时剩余：health 里的值只在续期时点更新，这里现算最新值。
        # 仅在 credentials 模块已加载时计算（独立模式不引入副作用）。
        creds_mod = sys.modules.get("config.credentials")
        if creds_mod is not None:
            import config.config as cfg
            live = {}
            for acc_id in cfg.ACCOUNTS:
                remaining = creds_mod.get_token_remaining_seconds(acc_id)
                if remaining is not None:
                    live[acc_id] = {"remaining": max(0.0, remaining), "healthy": remaining > 0}
            if live:
                snap["tokens"] = live

        snap["ok"] = True
        snap["now_epoch"] = _time.time()
        snap["embedded"] = _on_poll_cb is not None
        self._send_json(snap)

    def _handle_logs(self) -> None:
        """查看日志。source=live（内存环，增量）| error | response（文件尾部）。
           所有日志在写入时已经过 redact_sensitive 脱敏。"""
        from urllib.parse import parse_qs
        qs = parse_qs(self.path.partition("?")[2])

        def qs_int(key: str, default: int) -> int:
            try:
                return int((qs.get(key) or [str(default)])[0])
            except ValueError:
                return default

        source = (qs.get("source") or ["live"])[0]
        if source == "live":
            from src.logger import get_recent
            entries, seq = get_recent(qs_int("after", 0))
            self._send_json({"ok": True, "source": "live", "entries": entries, "seq": seq})
            return
        if source in ("error", "response", "system"):
            import config.config as cfg
            if source == "error":
                fp = Path(cfg.ERROR_LOG_FILE)
            elif source == "system":
                fp = Path(getattr(cfg, "SYSTEM_LOG_FILE", "logs/system_info.log"))
            else:
                fp = Path(cfg.RESPONSE_LOG_FILE)
            
            tail = max(1, min(qs_int("tail", 200), 1000))
            try:
                lines = _tail_file(fp, tail)
            except OSError as e:
                self._send_json({"ok": False, "errors": [f"读取日志文件失败: {e}"]}, 500)
                return
            self._send_json({"ok": True, "source": source, "lines": lines, "file": str(fp)})
            return
        self._send_json({"ok": False, "errors": [f"未知日志源: {source!r}"]}, 400)

    def do_PUT(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        if not self._check_origin():
            return
        if self.path.split("?", 1)[0] != "/api/config":
            self._send_json({"ok": False, "errors": ["未知路径"]}, 404)
            return
        if not self._check_auth():
            return
        raw = self._read_body_json()
        if raw is None:
            return
        errors = validate_config(raw)
        if errors:
            self._send_json({"ok": False, "errors": errors}, 400)
            return
        with _mutation_lock:
            try:
                save_config(raw)
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"写入 config.json 失败: {e}"]}, 500)
                return
            reloaded = _trigger_reload()
            from src.logger import log_all
            log_all("⚙️ 网页端更新 config.json 并成功触发热重载")
        self._send_json({
            "ok": True, "reloaded": reloaded,
            "cred_status": _cred_status(raw), "qq_bot_status": _qq_bot_status(raw),
        })

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        if not self._check_origin():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/auth/login":
            self._handle_login()
            return
        if path == "/api/auth/logout":
            self._handle_logout()
            return
        if path == "/api/users":
            if not self._check_auth():
                return
            self._handle_users_write()
            return
        if path in ("/api/qq_openid/start", "/api/qq_openid/stop"):
            if not self._check_auth():
                return
            self._handle_openid(path.rsplit("/", 1)[1])
            return
        if path == "/api/reload":
            if not self._check_auth():
                return
            reloaded = _trigger_reload()
            from src.logger import log_all
            log_all("⟳ 网页端请求系统配置热重载")
            self._send_json({"ok": True, "reloaded": reloaded})
            return
        if path == "/api/secrets":
            self._handle_secrets()
            return
        if path == "/api/accounts/rename":
            if not self._check_auth():
                return
            body = self._read_body_json()
            if body is None:
                return
            old_id = str(body.get("old_id", "")).strip()
            new_id = str(body.get("new_id", "")).strip()
            if not old_id or not new_id:
                self._send_json({"ok": False, "errors": ["缺少 old_id 或 new_id 参数"]}, 400)
                return
            try:
                from config import credentials
                credentials.rename_account(old_id, new_id)
                from src.logger import log_all
                log_all(f"🔄 账号凭证与状态已同步重命名: {old_id} -> {new_id}")
                self._send_json({"ok": True, "old_id": old_id, "new_id": new_id})
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"重命名账号失败: {e}"]}, 500)
            return
        if path == "/api/accounts/verify":
            if not self._check_auth():
                return
            body = self._read_body_json()
            if body is None:
                return
            account = str(body.get("account", "")).strip()
            if not account:
                self._send_json({"ok": False, "errors": ["缺少 account 参数"]}, 400)
                return
            try:
                from config.credentials import verify_and_handshake_account
                import asyncio
                h_ok, h_msg, h_details = asyncio.run(verify_and_handshake_account(account))
                self._send_json({"ok": h_ok, "msg": h_msg, "details": h_details})
            except Exception as e:
                self._send_json({"ok": False, "msg": f"验证异常: {e}"}, 500)
            return
        if path == "/api/accounts/smart_parse":
            if not self._check_auth():
                return
            body = self._read_body_json()
            if body is None:
                return
            raw_text = str(body.get("raw", "")).strip()
            account = str(body.get("account", "")).strip()
            if not raw_text:
                self._send_json({"ok": False, "errors": ["缺少 raw 文本"]}, 400)
                return
            try:
                import asyncio
                res = asyncio.run(self._smart_parse_credentials_text(raw_text, account))
                self._send_json({"ok": True, **res})
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"智能解析异常: {e}"]}, 500)
            return
        if path == "/api/subscriptions/sync":
            if not self._check_auth():
                return
            try:
                import asyncio
                from src.member_directory import sync_all_accounts_subscriptions, get_all_subscriptions
                stats = asyncio.run(sync_all_accounts_subscriptions())
                subs = get_all_subscriptions()
                self._send_json({"ok": True, "stats": stats, "subscriptions": subs})
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"同步订阅状态失败: {e}"]}, 500)
            return
        if path.startswith("/api/archive/"):
            if not self._guard(need_admin=False):
                return
            self._handle_archive(path[len("/api/archive/"):])
            return
        if path == "/api/social/parse_post":
            if not self._check_auth():
                return
            body = self._read_body_json()
            if body is None:
                return
            url = str(body.get("url", "")).strip()
            if not url:
                self._send_json({"ok": False, "errors": ["缺少 url 参数"]}, 400)
                return
            try:
                from src.social.single_fetcher import SocialUrlParser
                raw_cfg = _load_raw_config()
                parser = SocialUrlParser(raw_cfg)
                post = parser.parse(url)

                tr = None
                if body.get("translate", True) and post.text:
                    try:
                        from src import translator
                        import asyncio
                        tr = asyncio.run(translator.translate_text(post.text, "社媒", "偶像"))
                    except Exception:
                        pass

                media_list = [{"type": m.type, "url": m.url, "alt": m.alt_text} for m in post.media]
                self._send_json({
                    "ok": True,
                    "platform": post.platform,
                    "post_id": post.post_id,
                    "author": post.author,
                    "text": post.text,
                    "translation": tr,
                    "timestamp": post.timestamp,
                    "media": media_list,
                    "extra": post.extra,
                })
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"解析失败: {e}"]}, 500)
            return
        if path == "/api/social/manual_push":
            if not self._check_auth():
                return
            body = self._read_body_json()
            if body is None:
                return
            url = str(body.get("url", "")).strip()
            if not url:
                self._send_json({"ok": False, "errors": ["缺少 url 参数"]}, 400)
                return
            try:
                from src.social.single_fetcher import manual_push_social_url
                raw_cfg = _load_raw_config()
                translate = bool(body.get("translate", True))
                archive = bool(body.get("archive", True))
                channels = body.get("channels")
                if channels is not None and not isinstance(channels, list):
                    channels = [str(channels)]
                res = manual_push_social_url(url, raw_cfg, target_channels=channels, translate=translate, archive=archive)
                self._send_json(res)
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"推送失败: {e}"]}, 500)
            return
        if path == "/api/test_push":
            if not self._check_auth():
                return
            if _on_test_push_cb is None:
                self._send_json({"ok": False, "errors": ["独立模式下无法测试推送（主程序未运行在本进程）"]}, 400)
                return
            body = self._read_body_json()
            if body is None:
                return
            channel = body.get("channel", "tg")
            target = str(body.get("target", "")).strip()
            text = str(body.get("text", "")).strip() or (
                f"🧪 坂道监控 · 测试推送\n发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "（来自网页管理端，收到即代表该通道配置正确）"
            )
            if channel not in ("tg", "napcat", "official"):
                self._send_json({"ok": False, "errors": [f"不支持的通道: {channel!r}"]}, 400)
                return
            if not target:
                self._send_json({"ok": False, "errors": ["缺少推送目标 target"]}, 400)
                return
            if channel == "napcat" and not target.lstrip("-").isdigit():
                self._send_json({"ok": False, "errors": ["NapCat 目标必须是 QQ 群号"]}, 400)
                return
            from src.logger import log_all
            log_all(f"📨 网页端发起测试推送 [通道: {channel} | 目标: {target}]")
            ok, err = _on_test_push_cb(channel, target, text[:1000])
            if ok:
                self._send_json({"ok": True, "channel": channel, "target": target})
            else:
                self._send_json({"ok": False, "errors": [err or "发送失败"]}, 502)
            return
        if path == "/api/poll":
            if not self._check_auth():
                return
            if _on_poll_cb is None:
                self._send_json({"ok": False, "errors": ["独立模式下无法触发巡查（主程序未运行在本进程）"]}, 400)
                return
            try:
                _on_poll_cb()
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"触发巡查失败: {e}"]}, 500)
                return
            self._send_json({"ok": True})
            return
        if path == "/api/config/restore":
            if not self._check_auth():
                return
            body = self._read_body_json()
            if body is None:
                return
            name = body.get("name", "")
            if not isinstance(name, str) or not _HISTORY_NAME_RE.match(name):
                self._send_json({"ok": False, "errors": [f"非法历史版本名: {name!r}"]}, 400)
                return
            src = _history_dir() / name
            if not src.exists():
                self._send_json({"ok": False, "errors": [f"历史版本不存在: {name}"]}, 404)
                return
            try:
                import json5
                with open(src, "r", encoding="utf-8") as f:
                    raw = json5.load(f)
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"历史版本解析失败: {e}"]}, 500)
                return
            errors = validate_config(raw)
            if errors:
                self._send_json({"ok": False, "errors": ["历史版本未通过当前校验:"] + errors}, 400)
                return
            with _mutation_lock:
                try:
                    save_config(raw)   # 会先把当前版本快照进 history，恢复也可再撤销
                except Exception as e:
                    self._send_json({"ok": False, "errors": [f"写入失败: {e}"]}, 500)
                    return
                reloaded = _trigger_reload()
            self._send_json({"ok": True, "reloaded": reloaded, "restored": name})
            return
        if path == "/api/restart":
            if not self._check_auth():
                return
            if _on_restart_cb is None:
                self._send_json(
                    {"ok": False, "errors": ["独立模式下无法重启主程序（主程序未运行在本进程）"]}, 400)
                return
            from src.logger import log_all
            log_all("⟳ 网页端发起主程序进程重启")
            try:
                _on_restart_cb()
            except Exception as e:
                log_all(f"🚨 重启回调异常: {e}", is_error=True)
            self._send_json({"ok": True, "restarting": True})
            return
        if path == "/api/system/proxy/test":
            if not self._check_auth():
                return
            self._handle_proxy_test()
            return
        if path == "/api/system/storage/clean":
            if not self._check_auth():
                return
            if not self._guard(need_admin=True):
                return
            body = self._read_body_json()
            if body is None:
                return
            category = str(body.get("category", "")).strip()
            from src.utils import clean_storage_category
            ok, msg, freed = clean_storage_category(category)
            if ok:
                from src.logger import log_all
                log_all(f"🧹 网页端清理存储分类 [{category}]: {msg}")
                self._send_json({"ok": True, "msg": msg, "freed_bytes": freed})
            else:
                self._send_json({"ok": False, "errors": [msg]}, 400)
            return
        self._send_json({"ok": False, "errors": ["未知路径"]}, 404)

    def _handle_proxy_test(self) -> None:
        """测试指定代理服务器的连通性与关键节点响应延迟。"""
        body = self._read_body_json()
        if body is None:
            return
        proxy = str(body.get("proxy", "")).strip() or None

        import time
        import httpx

        targets = [
            {"name": "Google Gemini (AI 翻译)", "url": "https://generativelanguage.googleapis.com"},
            {"name": "Telegram Bot API", "url": "https://api.telegram.org"},
            {"name": "Instagram 官方", "url": "https://www.instagram.com"},
            {"name": "乃木坂46 Message", "url": "https://api.message.nogizaka46.com"},
        ]

        async def _probe(target):
            t0 = time.monotonic()
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=10.0, follow_redirects=True) as client:
                    resp = await client.get(target["url"])
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    return {
                        "name": target["name"],
                        "url": target["url"],
                        "ok": resp.status_code < 500,
                        "status_code": resp.status_code,
                        "latency_ms": latency_ms,
                        "error": None,
                    }
            except Exception as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                return {
                    "name": target["name"],
                    "url": target["url"],
                    "ok": False,
                    "status_code": 0,
                    "latency_ms": latency_ms,
                    "error": f"{type(e).__name__}: {str(e)[:80]}",
                }

        async def _run_all():
            return await asyncio.gather(*[_probe(t) for t in targets])

        try:
            results = asyncio.run(_run_all())
            all_ok = all(r["ok"] for r in results)
            any_ok = any(r["ok"] for r in results)
            self._send_json({"ok": True, "proxy": proxy, "all_ok": all_ok, "any_ok": any_ok, "results": results})
        except Exception as e:
            self._send_json({"ok": False, "errors": [f"代理测试执行失败: {e}"]}, 500)

    async def _smart_parse_credentials_text(self, raw: str, account: str = "") -> dict:
        """智能解析用户粘贴的 cURL / Headers / Signin Payload 文本。"""
        import re
        import json
        import httpx

        # 彻底清洗 Windows cmd 特有的所有转义模式 (如 ^\^", ^", ^{, ^}, ^&, ^|, ^$)
        cleaned = (
            raw.replace(r'^\^"', '"')
            .replace(r'\^"', '"')
            .replace(r'^\^', '')
            .replace('^^', '^')
            .replace('^"', '"')
            .replace('^{', '{')
            .replace('^}', '}')
            .replace('^&', '&')
            .replace('^|', '|')
            .replace('^$', '$')
        )
        result = {"token": "", "cookie": "", "refresh_token": "", "extracted": []}

        group_type = "nogizaka"
        acc_api_base = ""
        acc_web_origin = ""
        acc_app_tag = ""
        if account:
            try:
                raw_cfg = _load_raw_config()
                acc_data = raw_cfg.get("accounts", {}).get(account, {})
                group_type = acc_data.get("group_type") or acc_data.get("group") or "nogizaka"
                acc_api_base = acc_data.get("api_base") or ""
                acc_web_origin = acc_data.get("web_origin") or ""
                acc_app_tag = acc_data.get("app_tag") or ""
            except Exception:
                pass

        # 1. 检查是否包含 signin 请求体（用户在登录瞬间复制的 cURL）
        signin_match = re.search(r'--data-raw\s+["\']?(\{.+?\})["\']?(?:\s+&|\s*$|\s+-)', cleaned, re.DOTALL) or \
                       re.search(r'-d\s+["\']?(\{.+?\})["\']?(?:\s+&|\s*$|\s+-)', cleaned, re.DOTALL) or \
                       re.search(r'--data-raw\s+["\'](\{.+?\})["\']', cleaned) or \
                       re.search(r'-d\s+["\'](\{.+?\})["\']', cleaned)
        if "signin" in cleaned and signin_match:
            try:
                json_str = signin_match.group(1).strip()
                try:
                    body_json = json.loads(json_str)
                except Exception:
                    body_json = json.loads(json_str.replace(r'\"', '"'))

                # 从 cURL 中动态提取目标 URL
                url = ""
                url_m = re.search(r'(?:--url\s+["\']?|curl\s+["\']?)(https?://[^\s"\'>]+)', cleaned)
                if url_m and "signin" in url_m.group(1).lower():
                    url = url_m.group(1).strip().strip('"').strip("'")
                if not url:
                    if acc_api_base:
                        url = f"{acc_api_base.rstrip('/')}/v2/signin"
                    elif group_type.lower() == "yodel" or "yodel" in cleaned.lower():
                        url = "https://api.service.yodel-app.com/v2/signin"
                    else:
                        domain_part = group_type if group_type.endswith("46") else f"{group_type}46"
                        url = f"https://api.message.{domain_part}.com/v2/signin"

                # 提取 app-id
                app_id = ""
                app_id_m = re.search(r'x-talk-app-id:\s*([^\r\n"\']+)', cleaned, re.IGNORECASE)
                if app_id_m:
                    app_id = app_id_m.group(1).strip()
                if not app_id:
                    if acc_app_tag:
                        app_id = f"jp.co.sonymusic.communication.{acc_app_tag} 2.5"
                    elif group_type.lower() == "yodel" or "yodel" in url:
                        app_id = "jp.co.sonymusic.communication.yodel 2.5"
                    else:
                        app_id = f"jp.co.sonymusic.communication.{group_type} 2.5"

                # 提取 origin 与 referer
                origin = ""
                origin_m = re.search(r'origin:\s*([^\r\n"\']+)', cleaned, re.IGNORECASE)
                if origin_m:
                    origin = origin_m.group(1).strip()
                if not origin:
                    if acc_web_origin:
                        origin = acc_web_origin.rstrip("/")
                    elif group_type.lower() == "yodel" or "yodel" in url:
                        origin = "https://service.yodel-app.com"
                    else:
                        domain_part = group_type if group_type.endswith("46") else f"{group_type}46"
                        origin = f"https://message.{domain_part}.com"

                headers = {
                    "accept": "application/json",
                    "content-type": "application/json",
                    "origin": origin,
                    "referer": origin + "/",
                    "x-talk-app-id": app_id,
                    "x-talk-app-platform": "web"
                }

                # 附带 cURL 中可能包含的前置 Cookie
                req_cookie_m = re.search(r'(?:-b|--cookie)\s+["\']([^"\']+)["\']', cleaned, re.IGNORECASE)
                if req_cookie_m:
                    headers["cookie"] = req_cookie_m.group(1).strip()

                async with httpx.AsyncClient(timeout=12) as client:
                    r = await client.post(url, headers=headers, json=body_json)
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("access_token"):
                            result["token"] = data["access_token"]
                            result["extracted"].append("access_token (由登录接口自动换取)")
                        cookies = []
                        for sc in r.headers.get_list("set-cookie"):
                            sc_part = sc.split(";")[0].strip()
                            if sc_part and "=" in sc_part:
                                cookies.append(sc_part)
                        # 如果请求中带有前置 cookie（如 S5SI 等），一并合并
                        if req_cookie_m:
                            for part in req_cookie_m.group(1).split(";"):
                                p_trim = part.strip()
                                if p_trim and "=" in p_trim:
                                    cookies.append(p_trim)
                        if cookies:
                            from config.credentials import _clean_cookie_string
                            merged = {}
                            for c_item in cookies:
                                merged.update(_clean_cookie_string(c_item))
                            if merged:
                                result["cookie"] = "; ".join(f"{k}={v}" for k, v in merged.items())
                                result["extracted"].append("session Cookie (由登录响应下发，可长期自动续期)")
            except Exception as e:
                from src.logger import log_all
                log_all(f"⚠️ smart_parse 模拟登录请求失败: {e}")

        # 2. 提取所有 Authorization Bearer 或 access_token，并自动优选最新未过期的 Token
        if not result["token"]:
            token_candidates = []
            for m in re.finditer(r'(?:authorization|bearer)\s*[:=]?\s*(?:bearer\s+)?([a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-+/=]+)', cleaned, re.IGNORECASE):
                token_candidates.append(m.group(1).strip())
            for m in re.finditer(r'["\']?access_token["\']?\s*[:=]\s*["\']([a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-+/=]+)["\']', cleaned, re.IGNORECASE):
                token_candidates.append(m.group(1).strip())

            if token_candidates:
                from config.credentials import _decode_token_exp
                token_candidates = list(dict.fromkeys(token_candidates))
                token_candidates.sort(key=lambda t: _decode_token_exp(t) or 0, reverse=True)
                result["token"] = token_candidates[0]
                result["extracted"].append("Token (JWT)")

        # 3. 提取并智能合并所有 Cookie (-b, --cookie, -H "cookie: ...", cookie: ..., Set-Cookie)
        if not result["cookie"]:
            cookie_candidates = []
            for m in re.finditer(r'(?:-b|--cookie)\s+["\']([^"\']+)["\']', cleaned, re.IGNORECASE):
                cookie_candidates.append(m.group(1).strip())
            for m in re.finditer(r'(?:-H|--header)\s+["\']cookie:\s*([^"\']+)["\']', cleaned, re.IGNORECASE):
                cookie_candidates.append(m.group(1).strip())
            for m in re.finditer(r'^cookie:\s*(.+)$', cleaned, re.IGNORECASE | re.MULTILINE):
                cookie_candidates.append(m.group(1).strip())
            for m in re.finditer(r'set-cookie:\s*([^;\r\n]+)', cleaned, re.IGNORECASE):
                cookie_candidates.append(m.group(1).strip())

            if cookie_candidates:
                from config.credentials import _clean_cookie_string
                merged_cookies = {}
                cookie_candidates.sort(key=lambda c: ("session=" in c.lower(), len(c)))
                for cand in cookie_candidates:
                    parsed = _clean_cookie_string(cand)
                    merged_cookies.update(parsed)

                if merged_cookies:
                    result["cookie"] = "; ".join(f"{k}={v}" for k, v in merged_cookies.items())
                    result["extracted"].append("Cookie")
                    if "session" in merged_cookies:
                        result["extracted"].append("session (长期会话)")

        # 4. 提取 refresh_token
        if not result["refresh_token"]:
            m = re.search(r'["\']?refresh_token["\']?\s*[:=]\s*["\']([a-f0-9\-]{32,36})["\']', cleaned, re.IGNORECASE)
            if m:
                result["refresh_token"] = m.group(1).strip()
                result["extracted"].append("Refresh Token")

        return result

    def _handle_secrets(self) -> None:
        """写入凭证到 .env（值只进不出）。body:
           { "values": {"HINATA_SHARED_TOKEN": "...", ...}, "account": "hinata_shared"? }
           带 account 时执行凭证轮换（删除磁盘旧凭证，热重载后用新值重建）。"""
        if not self._check_auth():
            return
        body = self._read_body_json()
        if body is None:
            return
        remove = body.get("remove")
        if isinstance(remove, list) and remove and not body.get("values"):
            # 删除模式：仅接受白名单内的键（与写入同一套校验）
            bad = [k for k in remove
                   if not isinstance(k, str) or k in _FORBIDDEN_ENV_KEYS
                   or not _SECRET_KEY_RE.match(k)]
            if bad:
                self._send_json({"ok": False, "errors": [f"不允许删除的变量: {bad}"]}, 400)
                return
            with _mutation_lock:
                try:
                    update_env_file({}, remove=remove)
                except Exception as e:
                    self._send_json({"ok": False, "errors": [f"写入 .env 失败: {e}"]}, 500)
                    return
                for key in remove:
                    os.environ.pop(key, None)
                reloaded = _trigger_reload()
            try:
                raw = _load_raw_config()
                status = {"cred_status": _cred_status(raw), "qq_bot_status": _qq_bot_status(raw)}
            except Exception:
                status = {}
            self._send_json({"ok": True, "reloaded": reloaded, "removed": sorted(remove),
                             "env_status": _env_status(), **status})
            return

        values = body.get("values")
        if not isinstance(values, dict):
            self._send_json({"ok": False, "errors": ["缺少 values 对象"]}, 400)
            return
        errors = validate_secret_values(values)
        account = body.get("account")
        if account is not None:
            try:
                raw = _load_raw_config()
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"读取 config.json 失败: {e}"]}, 500)
                return
            if account not in raw.get("accounts", {}):
                errors.append(f"未知账号: {account!r}")
        if errors:
            self._send_json({"ok": False, "errors": errors}, 400)
            return

        with _mutation_lock:
            try:
                update_env_file(values)
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"写入 .env 失败: {e}"]}, 500)
                return
            for key, val in values.items():
                os.environ[key] = val
            if account is not None:
                _rotate_account_creds(account)
            reloaded = _trigger_reload()

        handshake_info = None
        if account is not None:
            try:
                from config.credentials import verify_and_handshake_account
                import asyncio
                h_ok, h_msg, h_details = asyncio.run(verify_and_handshake_account(account))
                handshake_info = {"ok": h_ok, "msg": h_msg, "details": h_details}
            except Exception as e:
                handshake_info = {"ok": False, "msg": f"握手过程异常: {e}", "details": {}}

        try:
            raw = _load_raw_config()
            status = {"cred_status": _cred_status(raw), "qq_bot_status": _qq_bot_status(raw)}
        except Exception:
            status = {}
        self._send_json({
            "ok": True, "reloaded": reloaded, "updated": sorted(values),
            "env_status": _env_status(),
            "handshake": handshake_info,
            **status,
        })

    def log_message(self, fmt: str, *args) -> None:
        # 静默常规访问日志，避免刷屏主程序输出；错误仍由异常路径打印
        pass


class _ThreadingHTTPServer(ThreadingHTTPServer):
    """静默处理客户端主动断开连接等无害网络异常。"""
    def handle_error(self, request, client_address):
        ex_type, _, _ = sys.exc_info()
        if ex_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


def start_webui(host: str | None = None, port: int | None = None,
                on_reload=None, on_restart=None, on_poll=None, on_test_push=None,
                on_openid=None):
    """启动网页管理端（后台守护线程）。

    参数缺省时从 config.config 读取 WEB_ADMIN_HOST / WEB_ADMIN_PORT。
    on_restart:   主程序注入的重启回调（触发优雅停机 + 进程自替换）。
    on_poll:      主程序注入的立即巡查回调（唤醒主循环）。
    on_test_push: 主程序注入的测试推送回调（(channel, target, text) -> (ok, err)）。
    不传则网页上不显示对应按钮（独立模式）。
    返回 ThreadingHTTPServer 实例（用于 shutdown() 清理），失败时返回 None。
    """
    global _on_reload_cb, _on_restart_cb, _on_poll_cb, _on_test_push_cb, _on_openid_cb, \
        _enforce_host_check
    _on_reload_cb = on_reload
    _on_restart_cb = on_restart
    _on_poll_cb = on_poll
    _on_test_push_cb = on_test_push
    _on_openid_cb = on_openid

    if host is None or port is None:
        import config.config as cfg
        host = host or getattr(cfg, "WEB_ADMIN_HOST", "127.0.0.1")
        port = port if port is not None else getattr(cfg, "WEB_ADMIN_PORT", 8787)

    _enforce_host_check = host in _LOOPBACK_HOSTS

    try:
        _ThreadingHTTPServer.request_queue_size = 128
        server = _ThreadingHTTPServer((host, port), _Handler)
        server.daemon_threads = True
    except OSError as e:
        from src.logger import log_all
        log_all(f"🚨 网页管理端启动失败（{host}:{port}）: {e}", is_error=True)
        return None

    thread = threading.Thread(target=server.serve_forever, name="webui", daemon=True)
    thread.start()

    import config.config as cfg
    if getattr(cfg, "AUTH_ENABLED", False):
        from src import auth as _auth
        if _auth.has_users():
            users = _auth.load_users()
            admins = sum(1 for u in users.values() if u.get("role") == "admin")
            hint = f"账号登录（{len(users)} 个用户 / {admins} 个管理员）"
        else:
            hint = "⚠️ 账号系统已启用但无用户，请执行 python tools/manage_users.py add <用户名>"
    elif os.getenv("WEB_ADMIN_TOKEN"):
        hint = "已启用 token 鉴权"
    else:
        hint = "无鉴权（仅限本机访问时可接受）"
    from src.logger import log_all
    try:
        log_all(f"🌐 网页管理端已启动: http://{host}:{server.server_address[1]}/ （{hint}）")
    except Exception:
        log_all(f"WebUI started: http://{host}:{server.server_address[1]}/")
    return server


# ================================================================
# 独立运行：python -m src.webui
# （主程序未运行时也可编辑配置；主程序若装了 watchdog 会自动热重载）
# ================================================================

if __name__ == "__main__":
    import config.config as _cfg  # noqa: F401 - 触发 .env 加载与配置校验
    from src.logger import log_all

    server = start_webui()
    if server is None:
        raise SystemExit(1)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
        log_all("✅ 网页管理端已停止")
