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

import hmac
import json
import os
import re
import shutil
import sys
import threading
import time as _time
from datetime import datetime
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

_BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = _BASE_DIR / "config" / "config.json"
SCHEMA_PATH = _BASE_DIR / "config" / "config.schema.json"
ENV_PATH = _BASE_DIR / ".env"
_STATIC_PATH = Path(__file__).resolve().parent / "webui_static" / "index.html"
_ARCHIVE_HTML_PATH = Path(__file__).resolve().parent / "webui_static" / "archive.html"
_LOGIN_HTML_PATH = Path(__file__).resolve().parent / "webui_static" / "login.html"

_SESSION_COOKIE = "sakamichi_session"

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
_OPTIONAL_ORDER = ["qq_send_interval", "translate", "gemini_models", "gemini_min_interval", "translate_timeout"]
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
    path = path or CONFIG_PATH
    try:
        _snapshot_config(path)
    except OSError as e:
        print(f"⚠️ 配置历史快照失败（继续保存）: {e}")
    text = serialize_config(raw)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _trigger_reload() -> bool:
    """写回后触发进程内热重载（测试中可 monkeypatch 掉）。"""
    from config.config import reload as _reload
    ok = _reload()
    if _on_reload_cb is not None:
        try:
            _on_reload_cb(ok)
        except Exception as e:
            print(f"🚨 网页管理端 on_reload 回调异常: {e}")
    return ok


# ================================================================
# 凭证写入：网页填写的密钥落到 .env（与手动编辑同一存放处）
# ================================================================

# 允许通过网页写入的 .env 变量名（白名单）。
# WEB_ADMIN_TOKEN 故意排除：管理端令牌只能手动设置，防止误操作把自己锁在门外。
_SECRET_KEY_RE = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*_(?:TOKEN|COOKIE|REFRESH_TOKEN|CLIENT_SECRET|APP_ID|TARGET_OPENID)"
    r"|GEMINI_API_KEY)$"
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
    """轮换账号凭证：删除磁盘持久化凭证 + 清除内存态。

    磁盘凭证优先级高于 .env（Token 续期后磁盘才是最新值），所以换新凭证时必须
    删掉旧文件，热重载后 load_all_accounts 才会用 .env 的新值重新初始化。
    （测试中可 monkeypatch 掉。）
    """
    import config.config as cfg
    cred_file = Path(cfg.CRED_DIR) / f"{account_id}.json"
    try:
        cred_file.unlink(missing_ok=True)
    except OSError as e:
        print(f"⚠️ 删除旧凭证文件失败 {cred_file}: {e}")
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
                "declared": True,
                "app_id": bool(b.get("app_id") or os.getenv(f"{prefix}_APP_ID")),
                "client_secret": bool(os.getenv(f"{prefix}_CLIENT_SECRET")),
                "target_openid": bool(b.get("target_openid") or os.getenv(f"{prefix}_TARGET_OPENID")),
                "secret_env": f"{prefix}_CLIENT_SECRET",
            }
            entry["ok"] = entry["app_id"] and entry["client_secret"] and entry["target_openid"]
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
        entry["ok"] = entry["app_id"] and entry["client_secret"] and entry["target_openid"]
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
        "TG_BOT_TOKEN": bool(os.getenv("TG_BOT_TOKEN")),
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


class _Handler(BaseHTTPRequestHandler):
    server_version = "SakamichiWebUI/1.0"

    # ── 工具 ─────────────────────────────────────────────
    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
        """WEB_ADMIN_TOKEN 校验（脚本 / 无会话访问用，等价 admin 身份）。"""
        token = os.getenv("WEB_ADMIN_TOKEN", "")
        if not token:
            return False
        supplied = self.headers.get("X-Auth-Token", "")
        if not supplied:
            authz = self.headers.get("Authorization", "")
            if authz.startswith("Bearer "):
                supplied = authz[7:]
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

        if not _auth.has_users():
            msg = ("账号系统已启用但还没有任何用户。请在服务器上执行："
                   "python tools/manage_users.py add <用户名>")
            if is_page:
                self._send_html_text("尚未创建账号", msg, 503)
            else:
                self._send_json({"ok": False, "errors": [msg]}, 503)
            return False

        user = self._current_user()
        if user is None:
            if is_page:
                self._redirect("/login?next=" + quote(self.path, safe=""))
            else:
                self._send_json({"ok": False, "errors": ["未登录"]}, 401)
            return False
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
        """主题 CSS / JS —— 白名单文件名，不接受任意路径。"""
        allowed = {"theme.css": "text/css", "theme.js": "application/javascript"}
        ctype = allowed.get(name)
        if ctype is None:
            self._send_json({"ok": False, "errors": ["未知静态资源"]}, 404)
            return
        try:
            body = (_STATIC_PATH.parent / name).read_bytes()
        except OSError:
            self._send_json({"ok": False, "errors": ["静态资源缺失"]}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")   # 改版即时生效
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
        # 共享静态资源（主题 token / 切换脚本）：登录页也要用，故不设鉴权
        if path in ("/static/theme.css", "/static/theme.js"):
            self._send_static(path.rsplit("/", 1)[1])
            return
        if path == "/login":
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

        import asyncio

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

        slim = [{
            "id": str(m.get("id", "")),
            "name": m.get("name") or "(无名)",
            "state": m.get("state", "?"),
            "tags": [str(t) for t in (m.get("tags") or [])],
            "subscription": (m.get("subscription") or {}).get("type", ""),
        } for m in members]
        self._send_json({"ok": True, "account": account, "members": slim})

    # ── 消息归档查看器 ─────────────────────────────
    _ARCHIVE_TYPES = ("text", "picture", "image", "video", "voice")

    # ── 登录 / 登出 / 身份 ─────────────────────────
    def _handle_auth_me(self) -> None:
        import config.config as cfg
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
        secret 只用于本次连接，不落盘、不回显。"""
        if _on_openid_cb is None:
            self._send_json({"ok": False, "errors": [
                "独立模式下不可用（需要主程序的事件循环来跑 WebSocket 监听）"]}, 400)
            return

        if action == "stop":
            ok, msg = _on_openid_cb("stop", "", "")
            self._send_json({"ok": ok, "message": msg})
            return

        body = self._read_body_json()
        if body is None:
            return
        app_id = str(body.get("app_id", "")).strip()
        secret = str(body.get("client_secret", "")).strip()
        # secret 允许留空 → 用 .env 里该 Bot 已配置的值
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

        ok, msg = _on_openid_cb("start", app_id, secret)
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

        from src import archive as _archive
        qs = parse_qs(self.path.partition("?")[2])

        def qp(key: str, default: str = "") -> str:
            return (qs.get(key) or [default])[0]

        if sub == "members":
            members = []
            for name in _archive.list_members():
                months = _archive.list_months(name)
                members.append({
                    "name": name,
                    "display": name.replace("_", " "),
                    "months": len(months),
                    "total": sum(m["count"] for m in months),
                })
            self._send_json({"ok": True, "members": members})
            return

        if sub == "months":
            member = qp("member")
            if member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {member!r}"]}, 404)
                return
            self._send_json({"ok": True, "member": member, "months": _archive.list_months(member)})
            return

        if sub == "messages":
            member = qp("member")
            if member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {member!r}"]}, 404)
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
            total = len(msgs)
            start = (page - 1) * per_page
            slim = [{
                "id": m.get("id"),
                "type": m.get("type"),
                "text": m.get("text", ""),
                "translation": m.get("_translation", ""),
                "published_at": m.get("published_at") or m.get("updated_at", ""),
                "media_url": (f"/api/archive/media/{member}/{m['_local_file']}"
                              if m.get("_local_file") else None),
                "download_failed": bool(m.get("_download_failed")),
                "w": m.get("thumbnail_width"),
                "h": m.get("thumbnail_height"),
            } for m in msgs[start:start + per_page]]
            self._send_json({
                "ok": True, "member": member, "year": year, "month": month,
                "total": total, "page": page,
                "total_pages": max(1, -(-total // per_page)), "messages": slim,
            })
            return

        if sub == "calendar":
            member = qp("member")
            if member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {member!r}"]}, 404)
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
            member = qp("member")
            if member not in _archive.list_members():
                self._send_json({"ok": False, "errors": [f"未归档的成员: {member!r}"]}, 404)
                return
            query = qp("q").strip()
            if not query:
                self._send_json({"ok": False, "errors": ["缺少搜索关键词 q"]}, 400)
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
            total = len(hits)
            start = (page - 1) * per_page
            slim = [{
                "id": m.get("id"),
                "type": m.get("type"),
                "text": m.get("text", ""),
                "translation": m.get("_translation", ""),
                "published_at": m.get("published_at") or m.get("updated_at", ""),
                "media_url": (f"/api/archive/media/{member}/{m['_local_file']}"
                              if m.get("_local_file") else None),
                "download_failed": bool(m.get("_download_failed")),
                "w": m.get("thumbnail_width"),
                "h": m.get("thumbnail_height"),
                "year": m.get("_year"),
                "month": m.get("_month"),
            } for m in hits[start:start + per_page]]
            self._send_json({
                "ok": True, "member": member, "q": query, "total": total,
                "page": page, "total_pages": max(1, -(-total // per_page)),
                "capped": total >= 500, "messages": slim,
            })
            return

        if sub.startswith("media/"):
            rest = unquote(sub[len("media/"):])
            member, _, rel = rest.partition("/")
            if member not in _archive.list_members() or not rel:
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
        if source in ("error", "response"):
            import config.config as cfg
            fp = Path(cfg.ERROR_LOG_FILE if source == "error" else cfg.RESPONSE_LOG_FILE)
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
            self._send_json({"ok": True, "reloaded": _trigger_reload()})
            return
        if path == "/api/secrets":
            self._handle_secrets()
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
            if channel not in ("tg", "napcat"):
                self._send_json({"ok": False, "errors": [f"不支持的通道: {channel!r}"]}, 400)
                return
            if not target:
                self._send_json({"ok": False, "errors": ["缺少推送目标 target"]}, 400)
                return
            if channel == "napcat" and not target.lstrip("-").isdigit():
                self._send_json({"ok": False, "errors": ["NapCat 目标必须是 QQ 群号"]}, 400)
                return
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
            # 先把响应发出去再触发停机，客户端才能收到确认
            self._send_json({"ok": True, "restarting": True})
            try:
                _on_restart_cb()
            except Exception as e:
                print(f"🚨 重启回调异常: {e}")
            return
        self._send_json({"ok": False, "errors": ["未知路径"]}, 404)

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

        try:
            raw = _load_raw_config()
            status = {"cred_status": _cred_status(raw), "qq_bot_status": _qq_bot_status(raw)}
        except Exception:
            status = {}
        self._send_json({
            "ok": True, "reloaded": reloaded, "updated": sorted(values),
            "env_status": _env_status(),
            **status,
        })

    def log_message(self, fmt: str, *args) -> None:
        # 静默常规访问日志，避免刷屏主程序输出；错误仍由异常路径打印
        pass


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
        server = ThreadingHTTPServer((host, port), _Handler)
    except OSError as e:
        print(f"🚨 网页管理端启动失败（{host}:{port}）: {e}")
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
    print(f"🌐 网页管理端已启动: http://{host}:{server.server_address[1]}/ （{hint}）")
    return server


# ================================================================
# 独立运行：python -m src.webui
# （主程序未运行时也可编辑配置；主程序若装了 watchdog 会自动热重载）
# ================================================================

if __name__ == "__main__":
    import config.config as _cfg  # noqa: F401 - 触发 .env 加载与配置校验

    server = start_webui()
    if server is None:
        raise SystemExit(1)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
        print("✅ 网页管理端已停止")
