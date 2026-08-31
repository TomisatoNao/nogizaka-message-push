"""
src/webui_modules/auth_handlers.py — WebUI 身份认证、会话与用户管理处理器

提供：
  1. Host 头校验（防 DNS rebinding）与 Origin 校验（防 CSRF）
  2. 会话 Cookie（短效 Session + 长效 RTR Refresh Token）与 API Token 鉴权
  3. 路由守卫与角色权限验证（_guard, _current_user）
  4. 登录、刷新、退出、当前用户信息与用户 CRUD 接口处理
"""

from __future__ import annotations

import hmac
import json
import os
from html import escape as html_escape
from urllib.parse import parse_qs, quote, urlparse

from src import auth as _auth
import config.config as cfg
from src.webui_modules.static_handler import send_json

SESSION_COOKIE = "sakamichi_session"
REFRESH_COOKIE = "sakamichi_refresh_token"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def check_host(handler, enforce_host_check: bool = False) -> bool:
    """绑定回环地址时校验 Host 头（防 DNS rebinding）。"""
    if not enforce_host_check:
        return True
    host = handler.headers.get("Host", "")
    if host.startswith("["):  # IPv6: [::1]:46046
        hostname = host[1:].split("]", 1)[0]
    else:
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
    if hostname in LOOPBACK_HOSTS:
        return True
    send_json(handler, {"ok": False, "errors": [f"拒绝非本机 Host: {host!r}"]}, 403)
    return False


def api_token_ok(handler) -> bool:
    """校验静态管理 Token（WEB_ADMIN_TOKEN）。"""
    token = os.getenv("WEB_ADMIN_TOKEN", "").strip()
    if not token:
        return False
    supplied = handler.headers.get("X-Auth-Token", "").strip()
    if not supplied:
        authz = handler.headers.get("Authorization", "").strip()
        if authz.startswith("Bearer "):
            supplied = authz[7:].strip()
    if not supplied:
        supplied = (parse_qs(handler.path.partition("?")[2]).get("token") or [""])[0]
    return bool(supplied) and hmac.compare_digest(supplied, token)


def cookie_token(handler) -> str:
    """提取当前请求的 Session Token。"""
    raw = handler.headers.get("Cookie", "")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            return value
    return ""


def cookie_refresh_token(handler) -> str:
    """提取当前请求的 Refresh Token。"""
    raw = handler.headers.get("Cookie", "")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == REFRESH_COOKIE:
            return value
    return ""


def current_user(handler) -> dict | None:
    """解析当前身份：会话 cookie 优先，其次 API token（视为 admin）。"""
    ttl = max(1, int(getattr(cfg, "AUTH_SESSION_HOURS", 2))) * 3600
    sess = _auth.get_session(cookie_token(handler), ttl_seconds=ttl)
    if sess:
        return {"username": sess["username"], "role": sess["role"], "via": "session"}
    if api_token_ok(handler):
        return {"username": "(api-token)", "role": "admin", "via": "token"}
    return None


def check_origin(handler) -> bool:
    """非 GET 请求校验 Origin（配合 SameSite=Strict cookie 防 CSRF）。"""
    origin = handler.headers.get("Origin", "")
    if not origin:
        return True
    host = (urlparse(origin).hostname or "").lower()
    if host in LOOPBACK_HOSTS:
        return True
    raw_host = handler.headers.get("Host", "")
    req_host = (urlparse(f"//{raw_host}").hostname or "").lower()
    if host == req_host:
        return True
    send_json(handler, {"ok": False, "errors": [f"拒绝跨站请求 Origin: {origin!r}"]}, 403)
    return False


def send_html_prompt(handler, title: str, message: str, code: int = 200, link: tuple[str, str] | None = None) -> None:
    """极简提示页（未登录 / 权限不足 / 未初始化）。"""
    extra = f'<p><a href="{link[0]}">{html_escape(link[1])}</a></p>' if link else ""
    body = (
        "<style>body{font-family:system-ui,sans-serif;background:#f5f6f8;color:#1c2333;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        "div{background:#fff;padding:32px 38px;border-radius:14px;max-width:520px;"
        "box-shadow:0 2px 12px rgba(0,0,0,.06);line-height:1.8}"
        "code{background:#f0f1f4;padding:2px 6px;border-radius:4px}"
        "a{color:#6d5bd0}</style>"
        f"<div><h2>{html_escape(title)}</h2><p>{html_escape(message)}</p>{extra}</div>"
    ).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def redirect(handler, location: str) -> None:
    """发送 302 重定向。"""
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def guard(handler, need_admin: bool = True, is_page: bool = False) -> bool:
    """路由守卫。通过返回 True；否则已发送响应（页面 302 / API 401·403）返回 False。"""
    if not getattr(cfg, "AUTH_ENABLED", False):
        if not os.getenv("WEB_ADMIN_TOKEN", ""):
            return True
        if api_token_ok(handler):
            return True
        send_json(handler, {"ok": False, "errors": ["未授权：X-Auth-Token 缺失或错误"]}, 401)
        return False

    if not need_admin and getattr(cfg, "AUTH_ARCHIVE_PUBLIC", False):
        return True

    user = current_user(handler)
    if user is None and is_page:
        refresh_tk = cookie_refresh_token(handler)
        if refresh_tk:
            access_ttl = max(1, int(getattr(cfg, "AUTH_SESSION_HOURS", 2))) * 3600
            refresh_days = max(1, int(getattr(cfg, "AUTH_REFRESH_DAYS", 30)))
            rot_user, new_access, new_refresh = _auth.verify_and_rotate_refresh_token(
                refresh_tk,
                access_ttl_seconds=access_ttl,
                refresh_ttl_days=refresh_days,
            )
            if rot_user:
                user = {"username": rot_user["username"], "role": rot_user["role"], "via": "refresh"}
                handler._pending_set_cookies = [
                    f"{SESSION_COOKIE}={new_access}; Path=/; Max-Age={access_ttl}; HttpOnly; SameSite=Strict",
                    f"{REFRESH_COOKIE}={new_refresh}; Path=/; Max-Age={refresh_days * 86400}; HttpOnly; SameSite=Strict",
                ]

    if user is not None:
        if need_admin and user["role"] != "admin":
            if is_page:
                send_html_prompt(
                    handler,
                    "权限不足",
                    f"当前账号 {user['username']}（{user['role']}）只能访问归档。",
                    403,
                    link=("/archive", "前往消息归档"),
                )
            else:
                send_json(handler, {"ok": False, "errors": ["需要管理员权限"]}, 403)
            return False
        return True

    if not _auth.has_users():
        msg = "账号系统已启用但还没有任何用户。请在服务器上执行：python tools/manage_users.py add <用户名>"
        if is_page:
            send_html_prompt(handler, "尚未创建账号", msg, 503)
        else:
            send_json(handler, {"ok": False, "errors": [msg]}, 503)
        return False

    if is_page:
        redirect(handler, "/login?next=" + quote(handler.path, safe=""))
    else:
        send_json(handler, {"ok": False, "errors": ["未登录"]}, 401)
    return False


# ================================================================
# 认证路由处理
# ================================================================

def handle_auth_me(handler) -> None:
    """GET /api/auth/me 返回当前登录用户信息。"""
    user = current_user(handler)
    auth_enabled = bool(getattr(cfg, "AUTH_ENABLED", False))
    if not user:
        send_json(handler, {"ok": True, "auth_enabled": auth_enabled, "user": None, "authenticated": False}, 200)
        return
    send_json(handler, {
        "ok": True,
        "auth_enabled": auth_enabled,
        "authenticated": True,
        "user": user,
        "username": user["username"],
        "role": user["role"],
        "via": user.get("via", "session"),
    }, 200)



def handle_login(handler, body: dict) -> None:
    """POST /api/auth/login 校验用户名密码并派发会话 Cookie。"""
    if not getattr(cfg, "AUTH_ENABLED", False):
        send_json(handler, {"ok": False, "errors": ["账号系统未启用"]}, 400)
        return
    ip = handler.client_address[0] if handler.client_address else "?"
    locked = _auth.is_locked_out(ip)
    if locked > 0:
        send_json(handler, {"ok": False, "errors": [f"登录失败次数过多，请 {int(locked // 60) + 1} 分钟后再试"]}, 429)
        return

    username = str(body.get("username", ""))[:64]
    password = str(body.get("password", ""))[:256]
    user = _auth.authenticate(username, password)
    if user is None:
        _auth.record_failure(ip)
        from src.logger import log_all
        log_all(f"🔒 网页登录失败: {username!r} 来自 {ip}", is_error=True)
        send_json(handler, {"ok": False, "errors": ["用户名或密码错误"]}, 401)
        return

    _auth.clear_failures(ip)
    remember = bool(body.get("remember", True))
    access_ttl = max(1, int(getattr(cfg, "AUTH_SESSION_HOURS", 2))) * 3600
    refresh_days = max(1, int(getattr(cfg, "AUTH_REFRESH_DAYS", 30))) if remember else 1
    token = _auth.create_session(user["username"], user["role"], access_ttl)
    refresh_token = _auth.create_refresh_token(user["username"], user["role"], ttl_days=refresh_days)
    from src.logger import log_all
    log_all(f"🔓 网页登录成功: {user['username']}（{user['role']}）来自 {ip}")

    handler._pending_set_cookies = [
        f"{SESSION_COOKIE}={token}; Path=/; Max-Age={access_ttl}; HttpOnly; SameSite=Strict",
        f"{REFRESH_COOKIE}={refresh_token}; Path=/; Max-Age={refresh_days * 86400}; HttpOnly; SameSite=Strict",
    ]
    send_json(handler, {
        "ok": True,
        "user": user,
        "username": user["username"],
        "role": user["role"],
        "token": token,
        "refresh_token": refresh_token,
        "session_ttl_seconds": access_ttl,
    }, 200)


def handle_refresh(handler) -> None:
    """POST /api/auth/refresh 刷新 Session Token。"""
    body = handler._read_body_json() if hasattr(handler, "_read_body_json") else {}
    body = body or {}
    r_token = cookie_refresh_token(handler) or str(body.get("refresh_token", "")).strip()
    if not r_token:
        send_json(handler, {"ok": False, "errors": ["缺少 Refresh Token"]}, 401)
        return

    access_ttl = max(1, int(getattr(cfg, "AUTH_SESSION_HOURS", 2))) * 3600
    refresh_days = max(1, int(getattr(cfg, "AUTH_REFRESH_DAYS", 30)))

    user, new_access, new_refresh = _auth.verify_and_rotate_refresh_token(
        r_token,
        access_ttl_seconds=access_ttl,
        refresh_ttl_days=refresh_days,
    )
    if not user:
        handler._pending_set_cookies = [
            f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
            f"{REFRESH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
        ]
        send_json(handler, {"ok": False, "errors": ["刷新令牌已失效，请重新登录"]}, 401)
        return

    handler._pending_set_cookies = [
        f"{SESSION_COOKIE}={new_access}; Path=/; Max-Age={access_ttl}; HttpOnly; SameSite=Strict",
        f"{REFRESH_COOKIE}={new_refresh}; Path=/; Max-Age={refresh_days * 86400}; HttpOnly; SameSite=Strict",
    ]
    send_json(handler, {
        "ok": True,
        "user": user,
        "username": user["username"],
        "role": user["role"],
        "token": new_access,
        "refresh_token": new_refresh,
        "session_ttl_seconds": access_ttl,
    }, 200)


def handle_logout(handler) -> None:
    """POST /api/auth/logout 注销当前会话。"""
    s_token = cookie_token(handler)
    if s_token:
        _auth.destroy_session(s_token)
    r_token = cookie_refresh_token(handler)
    if r_token:
        _auth.destroy_refresh_token(r_token)

    handler._pending_set_cookies = [
        f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
        f"{REFRESH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict",
    ]
    handler._pending_headers = [
        ("Clear-Site-Data", '"cache", "cookies"'),
    ]
    send_json(handler, {"ok": True, "message": "已成功退出登录"}, 200)



def handle_users_get(handler) -> None:
    """GET /api/auth/users 返回所有用户列表（仅 admin）。"""
    if not guard(handler, need_admin=True):
        return
    me = current_user(handler) or {}
    users = [{
        "username": name,
        "role": u.get("role", "viewer"),
        "created_at": u.get("created_at", 0),
        "is_me": name == me.get("username"),
    } for name, u in sorted(_auth.load_users().items())]
    send_json(handler, {"ok": True, "users": users, "min_password_len": _auth.MIN_PASSWORD_LEN}, 200)


def handle_users_write(handler, body: dict, method: str = "POST") -> None:
    """用户管理（仅 admin）：add / passwd / role / delete。"""
    if not guard(handler, need_admin=True):
        return

    action = str(body.get("action", ""))
    username = str(body.get("username", ""))[:64]
    password = str(body.get("password", ""))[:256]
    role = str(body.get("role", "viewer"))
    me = (current_user(handler) or {}).get("username", "?")

    if action == "add":
        ok, msg = _auth.add_user(username, password, role)
    elif action in ("passwd", "set_password"):
        ok, msg = _auth.set_password(username, password)
    elif action in ("role", "set_role"):
        ok, msg = _auth.set_role(username, role)
    elif action == "delete":
        if username == me:
            ok, msg = False, "不能删除当前登录的账号"
        else:
            ok, msg = _auth.delete_user(username)
    else:
        send_json(handler, {"ok": False, "errors": [f"未知操作: {action!r}"]}, 400)
        return

    if ok:
        from src.logger import log_all
        log_all(f"👤 用户管理[{me}]: {action} {username} — {msg}")
        send_json(handler, {"ok": True, "message": msg}, 200)
    else:
        send_json(handler, {"ok": False, "errors": [msg]}, 400)
