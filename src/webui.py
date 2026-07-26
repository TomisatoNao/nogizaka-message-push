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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = _BASE_DIR / "config" / "config.json"
SCHEMA_PATH = _BASE_DIR / "config" / "config.schema.json"
_STATIC_PATH = Path(__file__).resolve().parent / "webui_static" / "index.html"

# 热重载成功后的补偿回调（由 start_webui 注入，签名 on_reload(success: bool)）
_on_reload_cb = None


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
        if m.get("account") not in accounts:
            errors.append(f"成员 {label} 引用了未定义的账号: {m.get('account')!r}")
        key = (str(m.get("id")), str(m.get("account")))
        if key in seen:
            errors.append(f"成员 {label} 重复：同一账号下 id={m.get('id')} 出现多次")
        seen.add(key)

    return errors


# ================================================================
# 序列化：dict → 带分区注释的 JSONC 文本
# ================================================================

_SECTIONS: list[tuple[str, list[str]]] = [
    ("── 推送通道 ──",  ["channels", "napcat_api"]),
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
    if key in ("monitor", "gemini_models") and isinstance(val, list) and val:
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


def _qq_bot_status() -> list[dict]:
    """QQ 官方 Bot 凭证状态（.env 的 QQ_OFFICIAL_BOT{1,2}_*，只报有/无）。"""
    bots = []
    for i in (1, 2):
        entry = {
            "name": f"BOT{i}",
            "app_id": bool(os.getenv(f"QQ_OFFICIAL_BOT{i}_APP_ID")),
            "client_secret": bool(os.getenv(f"QQ_OFFICIAL_BOT{i}_CLIENT_SECRET")),
            "target_openid": bool(os.getenv(f"QQ_OFFICIAL_BOT{i}_TARGET_OPENID")),
        }
        entry["ok"] = entry["app_id"] and entry["client_secret"] and entry["target_openid"]
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
                "qq_bot_status": _qq_bot_status(),
                "config_path": str(CONFIG_PATH),
                "auth_required": bool(os.getenv("WEB_ADMIN_TOKEN", "")),
            })
            return

        self._send_json({"ok": False, "errors": ["未知路径"]}, 404)

    def do_PUT(self) -> None:  # noqa: N802
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
        try:
            save_config(raw)
        except Exception as e:
            self._send_json({"ok": False, "errors": [f"写入 config.json 失败: {e}"]}, 500)
            return
        reloaded = _trigger_reload()
        self._send_json({
            "ok": True, "reloaded": reloaded,
            "cred_status": _cred_status(raw), "qq_bot_status": _qq_bot_status(),
        })

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/reload":
            self._send_json({"ok": False, "errors": ["未知路径"]}, 404)
            return
        if not self._check_auth():
            return
        self._send_json({"ok": True, "reloaded": _trigger_reload()})

    def log_message(self, fmt: str, *args) -> None:
        # 静默常规访问日志，避免刷屏主程序输出；错误仍由异常路径打印
        pass


def start_webui(host: str | None = None, port: int | None = None, on_reload=None):
    """启动网页管理端（后台守护线程）。

    参数缺省时从 config.config 读取 WEB_ADMIN_HOST / WEB_ADMIN_PORT。
    返回 ThreadingHTTPServer 实例（用于 shutdown() 清理），失败时返回 None。
    """
    global _on_reload_cb
    _on_reload_cb = on_reload

    if host is None or port is None:
        import config.config as cfg
        host = host or getattr(cfg, "WEB_ADMIN_HOST", "127.0.0.1")
        port = port if port is not None else getattr(cfg, "WEB_ADMIN_PORT", 8787)

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
        print("✅ 网页管理端已停止")
