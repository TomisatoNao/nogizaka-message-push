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
import sys
import threading
import time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = _BASE_DIR / "config" / "config.json"
SCHEMA_PATH = _BASE_DIR / "config" / "config.schema.json"
ENV_PATH = _BASE_DIR / ".env"
_STATIC_PATH = Path(__file__).resolve().parent / "webui_static" / "index.html"

# 热重载成功后的补偿回调（由 start_webui 注入，签名 on_reload(success: bool)）
_on_reload_cb = None

# 重启回调（由主程序注入；触发优雅停机 + 进程自替换。独立模式下为 None）
_on_restart_cb = None

# 立即巡查回调（由主程序注入；唤醒主循环跳过等待。独立模式下为 None）
_on_poll_cb = None

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
    if key in ("channels", "web_admin") and isinstance(val, dict) and val:
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


def save_config(raw: dict, path: Path | None = None) -> None:
    """序列化并原子写回 config.json（临时文件 + os.replace）。"""
    path = path or CONFIG_PATH
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


def update_env_file(values: dict[str, str], path: Path | None = None) -> None:
    """更新 .env：已有的键原地替换，新键追加到末尾；其余行（注释等）原样保留。"""
    path = path or ENV_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else [
        "# .env — 密钥和凭证（由网页管理端创建，参考 .env.example）",
    ]
    remaining = dict(values)
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            lines[i] = f"{key}={_quote_env(remaining.pop(key))}"
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
        # 只展示前两个槽位 + 任何已配置的更高槽位，避免表格里挂 20 行空行
        if i <= 2 or entry["app_id"] or entry["client_secret"] or entry["target_openid"]:
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

    def _check_auth(self) -> bool:
        token = os.getenv("WEB_ADMIN_TOKEN", "")
        if not token:
            return True
        supplied = self.headers.get("X-Auth-Token", "")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[7:]
        if hmac.compare_digest(supplied, token):
            return True
        self._send_json({"ok": False, "errors": ["未授权：X-Auth-Token 缺失或错误"]}, 401)
        return False

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
    def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
        if not self._check_host():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                body = _STATIC_PATH.read_bytes()
            except OSError:
                self._send_json({"ok": False, "errors": ["管理页面文件缺失"]}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
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
        path = self.path.split("?", 1)[0]
        if path == "/api/reload":
            if not self._check_auth():
                return
            self._send_json({"ok": True, "reloaded": _trigger_reload()})
            return
        if path == "/api/secrets":
            self._handle_secrets()
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
                on_reload=None, on_restart=None, on_poll=None):
    """启动网页管理端（后台守护线程）。

    参数缺省时从 config.config 读取 WEB_ADMIN_HOST / WEB_ADMIN_PORT。
    on_restart: 主程序注入的重启回调（触发优雅停机 + 进程自替换）。
    on_poll:    主程序注入的立即巡查回调（唤醒主循环）。
    二者不传则网页上不显示对应按钮（独立模式）。
    返回 ThreadingHTTPServer 实例（用于 shutdown() 清理），失败时返回 None。
    """
    global _on_reload_cb, _on_restart_cb, _on_poll_cb, _enforce_host_check
    _on_reload_cb = on_reload
    _on_restart_cb = on_restart
    _on_poll_cb = on_poll

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

    token_hint = "已启用 token 鉴权" if os.getenv("WEB_ADMIN_TOKEN") else "未设置 WEB_ADMIN_TOKEN（仅限本机访问时可接受）"
    print(f"🌐 网页管理端已启动: http://{host}:{server.server_address[1]}/ （{token_hint}）")
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
