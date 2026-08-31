# ============================================================
# webui.py — 网页管理端：在浏览器里编辑 config.json 并热重载
# ============================================================
# 零新增依赖：用 stdlib http.server 跑在后台守护线程（与 watcher 同模式）。
# ============================================================
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from src.webui_modules.config_service import (
    _FORBIDDEN_ENV_KEYS,
    _HISTORY_KEEP,
    _HISTORY_NAME_RE,
    _OPTIONAL_COMMENT,
    _OPTIONAL_ORDER,
    _SECRET_KEY_RE,
    _SECTIONS,
    _cred_status,
    _history_dir,
    _qq_bot_status,
    _quote_env,
    _rotate_account_creds,
    _snapshot_config,
    _trigger_reload,
    list_config_history,
    save_config,
    serialize_config,
    set_on_reload_callback,
    update_env_file,
    validate_config,
    validate_secret_values,
)
from src.webui_modules.media_service import serve_file_range
from src.webui_modules.static_handler import (
    ARCHIVE_HTML_PATH,
    INDEX_HTML_PATH,
    LOGIN_HTML_PATH,
    NOT_FOUND_HTML_PATH,  # noqa: F401
    compress_if_supported,
    send_404,
    send_html,
    send_html_text,
    send_json,
    send_static,
)
from src.webui_modules.auth_handlers import (
    LOOPBACK_HOSTS,
    REFRESH_COOKIE,
    SESSION_COOKIE,
    api_token_ok,
    check_host,
    check_origin,
    cookie_refresh_token,
    cookie_token,
    current_user,
    guard,
    handle_auth_me,
    handle_api_token_session,
    handle_login,
    handle_logout,
    handle_refresh,
    handle_users_get,
    handle_users_write,
)
from src.webui_modules.system_handlers import (
    env_status as _env_status,
    handle_logs as _sys_handle_logs,
    handle_members as _sys_handle_members,
    handle_openid_action as _sys_handle_openid_action,
    handle_openid_status as _sys_handle_openid_status,
    handle_proxy_test as _sys_handle_proxy_test,
    handle_status as _sys_handle_status,
    handle_storage as _sys_handle_storage,
    handle_storage_clean as _sys_handle_storage_clean,
    handle_test_push as _sys_handle_test_push,
    smart_parse_credentials_text as _sys_smart_parse,
    tail_file as _tail_file,  # noqa: F401
)
from src.webui_modules.archive_handlers import (
    ARCHIVE_TYPES,
    BLOG_IMAGE_DIR,  # noqa: F401
    get_blog_db as _get_blog_db,  # noqa: F401
    handle_archive as _mod_handle_archive,
)

__all__ = [
    "CONFIG_PATH",
    "ENV_PATH",
    "PROJECT_ROOT",
    "SCHEMA_PATH",
    "_FORBIDDEN_ENV_KEYS",
    "_HISTORY_KEEP",
    "_HISTORY_NAME_RE",
    "_OPTIONAL_COMMENT",
    "_OPTIONAL_ORDER",
    "_SECRET_KEY_RE",
    "_SECTIONS",
    "_cred_status",
    "_history_dir",
    "_qq_bot_status",
    "_quote_env",
    "_rotate_account_creds",
    "_snapshot_config",
    "_trigger_reload",
    "list_config_history",
    "save_config",
    "serialize_config",
    "set_on_reload_callback",
    "start_webui",
    "update_env_file",
    "validate_config",
    "validate_secret_values",
]

_BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = _BASE_DIR
CONFIG_PATH = _BASE_DIR / "config" / "config.json"
SCHEMA_PATH = _BASE_DIR / "config" / "config.schema.json"
ENV_PATH = _BASE_DIR / ".env"

_SESSION_COOKIE = SESSION_COOKIE
_REFRESH_COOKIE = REFRESH_COOKIE
_LOOPBACK_HOSTS = LOOPBACK_HOSTS

_on_reload_cb = None
_on_restart_cb = None
_on_poll_cb = None
_on_test_push_cb = None
_on_openid_cb = None
_mutation_lock = threading.Lock()
_enforce_host_check = False


def _load_raw_config() -> dict:
    """从磁盘读取 config.json（JSONC），返回新格式对象。"""
    import json5
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json5.load(f)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SakamichiWebUI/1.0"
    timeout = 30
    _ARCHIVE_TYPES = ARCHIVE_TYPES

    def handle(self) -> None:
        try:
            super().handle()
        except (socket.timeout, TimeoutError):
            pass
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        except Exception as exc:
            from src.logger import log_all
            log_all(f"🚨 WebUI 请求未处理异常: {type(exc).__name__}: {exc}", is_error=True)

    # ── 兼容性代理方法 ─────────────────────────────────────
    def _query_params(self) -> dict[str, str]:
        qs = parse_qs(self.path.partition("?")[2])
        return {k: v[0] for k, v in qs.items() if v}

    def _compress_if_supported(self, data: bytes) -> tuple[bytes, dict[str, str]]:
        return compress_if_supported(self, data)

    def _send_json(self, obj: dict, code: int = 200) -> None:
        send_json(self, obj, code)

    def _check_host(self) -> bool:
        return check_host(self, _enforce_host_check)

    def _check_origin(self) -> bool:
        return check_origin(self)

    def _api_token_ok(self) -> bool:
        return api_token_ok(self)

    def _cookie_token(self) -> str:
        return cookie_token(self)

    def _cookie_refresh_token(self) -> str:
        return cookie_refresh_token(self)

    def _current_user(self) -> dict | None:
        return current_user(self)

    def _guard(self, need_admin: bool = True, is_page: bool = False) -> bool:
        return guard(self, need_admin, is_page)

    def _check_auth(self) -> bool:
        return guard(self, need_admin=True)

    def _send_static(self, name: str) -> None:
        send_static(self, name)

    def _send_html(self, file_path: Path, code: int = 200) -> None:
        send_html(self, file_path, code)

    def _send_404(self, message: str = "未知路径") -> None:
        send_404(self, message)

    def _send_html_text(self, title: str, message: str, code: int = 200, link: tuple[str, str] | None = None) -> None:
        send_html_text(self, title, message, code)

    def _handle_archive(self, sub: str) -> None:
        _mod_handle_archive(self, sub, self._guard, self._read_body_json)

    async def _smart_parse_credentials_text(self, raw: str, account: str = "") -> dict:
        return await _sys_smart_parse(raw, account)

    def _serve_file_range(self, path: Path) -> None:
        serve_file_range(self, path)

    def _read_body_json(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._send_json({"ok": False, "errors": ["Content-Length 必须是非负整数"]}, 400)
            return None
        if length < 0:
            self._send_json({"ok": False, "errors": ["Content-Length 必须是非负整数"]}, 400)
            return None
        if length == 0:
            return {}
        if length > 5 * 1024 * 1024:
            self._send_json({"ok": False, "errors": ["请求体过大（上限 5MB）"]}, 413)
            return None
        try:
            body = self.rfile.read(length)
        except (socket.timeout, TimeoutError):
            self._send_json({"ok": False, "errors": ["读取请求体超时"]}, 408)
            return None
        except (ConnectionResetError, BrokenPipeError):
            return None
        import json5
        try:
            return json5.loads(body.decode("utf-8"))
        except Exception as e:
            self._send_json({"ok": False, "errors": [f"JSON 解析失败: {e}"]}, 400)
            return None

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── 路由派发 ──────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        path = self.path.split("?", 1)[0]

        # 0. 健康探针（免鉴权放行）
        if path in ("/api/health", "/api/health/status"):
            self._send_json({"ok": True, "status": "healthy"})
            return

        # 1. 静态页面与资产
        if path in ("", "/", "/index.html"):
            import config.config as cfg
            user = self._current_user()
            if user is None and getattr(cfg, "AUTH_ENABLED", False) and getattr(cfg, "AUTH_ARCHIVE_PUBLIC", False):
                self._redirect("/archive")
                return
            if not self._guard(need_admin=True, is_page=True):
                return
            self._send_html(INDEX_HTML_PATH)
            return

        if path in ("/archive", "/archive.html"):
            if not self._guard(need_admin=False, is_page=True):
                return
            self._send_html(ARCHIVE_HTML_PATH)
            return

        if path in ("/login", "/login.html"):
            self._send_html(LOGIN_HTML_PATH)
            return

        if path.startswith("/static/"):
            self._send_static(path[len("/static/"):])
            return

        # 2. 鉴权与用户
        if path == "/api/auth/me":
            handle_auth_me(self)
            return
        if path in ("/api/auth/users", "/api/users"):
            handle_users_get(self)
            return

        # 3. 系统监控与运维
        if path in ("/api/status",):
            if not self._guard(need_admin=True):
                return
            _sys_handle_status(self, _on_poll_cb)
            return

        if path == "/api/logs":
            if not self._guard(need_admin=True):
                return
            _sys_handle_logs(self)
            return

        if path in ("/api/storage", "/api/system/storage"):
            if not self._guard(need_admin=True):
                return
            _sys_handle_storage(self)
            return

        if path == "/api/members":
            if not self._guard(need_admin=True):
                return
            _sys_handle_members(self, _load_raw_config)
            return

        if path == "/api/qq_openid/status":
            if not self._guard(need_admin=True):
                return
            _sys_handle_openid_status(self, _on_openid_cb)
            return

        # 4. 配置中心
        if path == "/api/config":
            if not self._guard(need_admin=True):
                return
            try:
                raw = _load_raw_config()
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"读取 config.json 失败: {e}"]}, 500)
                return
            self._send_json({
                "ok": True, "config": raw,
                "cred_status": _cred_status(raw), "qq_bot_status": _qq_bot_status(raw),
                "env_status": _env_status(),
                "can_restart": _on_restart_cb is not None,
            })
            return

        if path == "/api/config/schema":
            if not self._guard(need_admin=True):
                return
            try:
                with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
                    self._send_json({"ok": True, "schema": json.load(f)})
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"读取 schema 失败: {e}"]}, 500)
            return

        if path == "/api/config/history":
            if not self._guard(need_admin=True):
                return
            name = self._query_params().get("name")
            if name:
                if not _HISTORY_NAME_RE.match(name):
                    self._send_json({"ok": False, "errors": [f"非法快照文件名: {name!r}"]}, 400)
                    return
                src = _history_dir() / name
                if not src.is_file():
                    self._send_json({"ok": False, "errors": [f"快照文件不存在: {name!r}"]}, 404)
                    return
                try:
                    content_str = src.read_text(encoding="utf-8")
                    self._send_json({"ok": True, "name": name, "content": content_str})
                except Exception as e:
                    self._send_json({"ok": False, "errors": [f"读取快照失败: {e}"]}, 500)
                return
            self._send_json({"ok": True, "history": list_config_history()})
            return

        # 5. 归档与媒体
        if path.startswith("/api/archive/"):
            if not self._guard(need_admin=False):
                return
            sub = path[len("/api/archive/"):]
            _mod_handle_archive(self, sub, self._guard, self._read_body_json)
            return

        if path.startswith("/avatar/"):
            sub = "avatar"
            _mod_handle_archive(self, sub, self._guard, self._read_body_json)
            return

        if path.startswith("/media/"):
            if not self._guard(need_admin=False):
                return
            sub = "media/" + path[len("/media/"):]
            _mod_handle_archive(self, sub, self._guard, self._read_body_json)
            return

        if path.startswith("/blog_media/"):
            if not self._guard(need_admin=False):
                return
            sub = "blog_media/" + path[len("/blog_media/"):]
            _mod_handle_archive(self, sub, self._guard, self._read_body_json)
            return

        self._send_404()

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        if not self._check_origin():
            return
        path = self.path.split("?", 1)[0]

        # 1. 登录认证
        if path == "/api/auth/login":
            body = self._read_body_json()
            if body is not None:
                handle_login(self, body)
            return
        if path == "/api/auth/refresh":
            handle_refresh(self)
            return
        if path == "/api/auth/token-session":
            handle_api_token_session(self)
            return
        if path == "/api/auth/logout":
            handle_logout(self)
            return
        if path in ("/api/users", "/api/auth/users"):
            body = self._read_body_json()
            if body is not None:
                handle_users_write(self, body, "POST")
            return

        # 2. 配置与密钥管理
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

        if path in ("/api/config/rollback", "/api/config/restore"):
            if not self._check_auth():
                return
            body = self._read_body_json()
            if body is None:
                return
            filename = str(body.get("name") or body.get("filename", "")).strip()
            if not _HISTORY_NAME_RE.match(filename):
                self._send_json({"ok": False, "errors": [f"非法快照文件名: {filename!r}"]}, 400)
                return
            src = _history_dir() / filename
            if not src.is_file():
                self._send_json({"ok": False, "errors": [f"快照文件不存在: {filename!r}"]}, 404)
                return
            import json5
            try:
                with open(src, "r", encoding="utf-8") as f:
                    raw = json5.load(f)
            except Exception as e:
                self._send_json({"ok": False, "errors": [f"解析快照失败: {e}"]}, 500)
                return
            with _mutation_lock:
                try:
                    save_config(raw)
                except Exception as e:
                    self._send_json({"ok": False, "errors": [f"写入 config.json 失败: {e}"]}, 500)
                    return
                reloaded = _trigger_reload()
            self._send_json({
                "ok": True, "reloaded": reloaded, "restored": filename, "restored_from": filename,
                "cred_status": _cred_status(raw), "qq_bot_status": _qq_bot_status(raw),
            })
            return

        # 3. 运维诊断与工具
        if path in ("/api/proxy/test", "/api/system/proxy/test"):
            if not self._check_auth():
                return
            body = self._read_body_json() or {}
            _sys_handle_proxy_test(self, body)
            return

        if path in ("/api/storage/clean", "/api/system/storage/clean"):
            if not self._check_auth():
                return
            body = self._read_body_json() or {}
            _sys_handle_storage_clean(self, body)
            return

        if path in ("/api/restart", "/api/system/restart"):
            if not self._check_auth():
                return
            if _on_restart_cb is not None:
                threading.Thread(target=_on_restart_cb, name="restart_trigger", daemon=True).start()
                self._send_json({"ok": True, "message": "已触发系统优雅重启"})
            else:
                self._send_json({"ok": False, "errors": ["独立运行模式不支持通过网页重启"]}, 400)
            return

        if path in ("/api/poll", "/api/system/poll"):
            if not self._check_auth():
                return
            if _on_poll_cb is not None:
                _on_poll_cb()
                self._send_json({"ok": True, "message": "已触发立即巡查"})
            else:
                self._send_json({"ok": False, "errors": ["独立运行模式不支持立即巡查"]}, 400)
            return

        if path in ("/api/test_push", "/api/system/test_push"):
            if not self._check_auth():
                return
            body = self._read_body_json() or {}
            _sys_handle_test_push(self, body, _on_test_push_cb)
            return

        if path in ("/api/qq_openid/start", "/api/qq_openid/stop"):
            if not self._check_auth():
                return
            action = path.rsplit("/", 1)[1]
            body = self._read_body_json() or {}
            _sys_handle_openid_action(self, action, body, _on_openid_cb)
            return

        # 4. 归档子路由 POST
        if path.startswith("/api/archive/"):
            sub = path[len("/api/archive/"):]
            _mod_handle_archive(self, sub, self._guard, self._read_body_json)
            return

        self._send_404()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        if not self._check_origin():
            return
        path = self.path.split("?", 1)[0]

        if path == "/api/config":
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
            return

        if path in ("/api/users", "/api/auth/users"):
            body = self._read_body_json()
            if body is not None:
                handle_users_write(self, body, "PUT")
            return

        if path.startswith("/api/archive/"):
            sub = path[len("/api/archive/"):]
            _mod_handle_archive(self, sub, self._guard, self._read_body_json)
            return

        self._send_404()

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._check_host():
            return
        if not self._check_origin():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/api/users", "/api/auth/users"):
            body = self._read_body_json() or {}
            handle_users_write(self, body, "POST")
            return
        self._send_404()

    def _handle_secrets(self) -> None:
        """写入凭证到 .env。"""
        if not self._check_auth():
            return
        body = self._read_body_json()
        if body is None:
            return
        remove = body.get("remove")
        if isinstance(remove, list) and remove and not body.get("values"):
            bad = [k for k in remove if not isinstance(k, str) or k in _FORBIDDEN_ENV_KEYS or not _SECRET_KEY_RE.match(k)]
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
            self._send_json({"ok": True, "reloaded": reloaded, "removed": sorted(remove), "env_status": _env_status(), **status})
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
        self._send_json({"ok": True, "reloaded": reloaded, "updated": sorted(values), "env_status": _env_status(), **status})

    def log_message(self, fmt: str, *args) -> None:
        pass


class _ThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    allow_reuse_port = getattr(socket, "SO_REUSEPORT", None) is not None

    def handle_error(self, request, client_address):
        ex_type, _, _ = sys.exc_info()
        if ex_type in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


def start_webui(
    host: str | None = None,
    port: int | None = None,
    on_reload=None,
    on_restart=None,
    on_poll=None,
    on_test_push=None,
    on_openid=None,
):
    """启动网页管理端（后台守护线程）。"""
    global _on_reload_cb, _on_restart_cb, _on_poll_cb, _on_test_push_cb, _on_openid_cb, _enforce_host_check
    _on_reload_cb = on_reload
    _on_restart_cb = on_restart
    _on_poll_cb = on_poll
    _on_test_push_cb = on_test_push
    _on_openid_cb = on_openid

    if host is None or port is None:
        import config.config as cfg
        host = host or getattr(cfg, "WEB_ADMIN_HOST", "127.0.0.1")
        port = port if port is not None else getattr(cfg, "WEB_ADMIN_PORT", 46046)

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
    has_auth = False
    if getattr(cfg, "AUTH_ENABLED", False):
        from src import auth as _auth
        if _auth.has_users():
            users = _auth.load_users()
            admins = sum(1 for u in users.values() if u.get("role") == "admin")
            hint = f"账号登录（{len(users)} 个用户 / {admins} 个管理员）"
            has_auth = True
        else:
            hint = "⚠️ 账号系统已启用但无用户，请执行 python tools/manage_users.py add <用户名>"
    elif os.getenv("WEB_ADMIN_TOKEN"):
        hint = "已启用 token 鉴权"
        has_auth = True
    else:
        hint = "无鉴权（仅限本机访问时可接受）"

    from src.logger import log_all
    if host not in _LOOPBACK_HOSTS and not has_auth:
        log_all(f"⚠️ [安全警告] 网页管理端监听非回环地址 ({host}) 且未启用密码或 Token 鉴权！", is_error=True)

    try:
        log_all(f"🌐 网页管理端已启动: http://{host}:{server.server_address[1]}/ （{hint}）")
    except Exception:
        log_all(f"WebUI started: http://{host}:{server.server_address[1]}/")
    return server


if __name__ == "__main__":
    import config.config as _cfg  # noqa: F401
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
