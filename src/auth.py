# ============================================================
# auth.py — 账号系统：密码哈希、用户库、会话、登录限流（基于 data/auth.db）
# ============================================================
# 角色:
#   admin  —— 管理端全部功能（配置 / 凭证 / 日志 / 重启 / 归档）
#   viewer —— 只能访问归档查看器
#
# 安全与架构设计:
#   - 独立安全数据库 data/auth.db（与业务消息归档库 archive.db 彻底物理隔离）
#   - 密码用 scrypt 加盐哈希（stdlib hashlib），永不存明文、不可逆
#   - 校验用 hmac.compare_digest 常时比较，免疫时序攻击
#   - 会话采用内存高速缓存 + SQLite 持久化，重启无感保持登录态
#   - 会话 Token 配 HttpOnly + SameSite=Strict cookie 与滑动续期
#   - 改密、删号与角色变更联动销毁/更新全端活跃会话
#   - 登录失败按 IP 限流锁定，抵抗暴力破解
# ============================================================
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
AUTH_DB_PATH = _BASE_DIR / "data" / "auth.db"
LEGACY_USERS_JSON = _BASE_DIR / "data" / "users.json"
USERS_PATH = AUTH_DB_PATH  # 兼容测试与旧引用别名

ROLES = ("admin", "viewer")
MIN_PASSWORD_LEN = 8

# scrypt 参数（n=2**14 约需 16MB 内存、现代机器几十毫秒，符合 OWASP 下限建议）
# maxmem 显式给到 128MB —— OpenSSL 默认上限 32MB，不显式声明时调大 n 会直接报错
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 128 * 1024 * 1024
_SALT_BYTES = 16

# 登录限流：LOCK_WINDOW 内失败 MAX_FAILURES 次 → 锁定 LOCK_SECONDS
MAX_FAILURES = 5
LOCK_WINDOW = 600
LOCK_SECONDS = 900

_lock = threading.Lock()
_auth_conn: sqlite3.Connection | None = None
_sessions: dict[str, dict] = {}          # token -> {username, role, expires_at}
_sessions_loaded_from_db: bool = False   # 是否已从 DB 加载过活跃会话
_failures: dict[str, list[float]] = {}   # ip -> [失败时间戳]
_locked_until: dict[str, float] = {}     # ip -> 解锁时间


# ================================================================
# 数据库连接与初始化
# ================================================================

def get_auth_db() -> sqlite3.Connection:
    """获取或初始化 data/auth.db 数据库连接。"""
    global _auth_conn
    if _auth_conn is not None:
        return _auth_conn

    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUTH_DB_PATH), timeout=10.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            password_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            expires_at REAL NOT NULL,
            created_at REAL NOT NULL DEFAULT 0
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_username ON sessions(username);")
    conn.commit()

    # 自动无缝平滑迁移旧版 data/users.json（若存在且 users 表为空）
    if AUTH_DB_PATH == _BASE_DIR / "data" / "auth.db" and LEGACY_USERS_JSON.exists():
        try:
            cur = conn.execute("SELECT COUNT(*) FROM users;")
            if cur.fetchone()[0] == 0:
                with open(LEGACY_USERS_JSON, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    with conn:
                        for uname, info in data.items():
                            conn.execute(
                                "INSERT OR REPLACE INTO users (username, role, password_json, created_at) VALUES (?, ?, ?, ?);",
                                (uname, info.get("role", "viewer"), json.dumps(info.get("password", {})), float(info.get("created_at", 0)))
                            )
        except Exception:
            pass

    _auth_conn = conn
    return _auth_conn


# ================================================================
# 密码哈希
# ================================================================

def hash_password(password: str) -> dict:
    """生成密码记录（含随机盐与 scrypt 参数）。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            maxmem=_SCRYPT_MAXMEM)
    return {
        "algo": "scrypt",
        "salt": salt.hex(),
        "hash": digest.hex(),
        "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
    }


def verify_password(password: str, record: dict) -> bool:
    """常时比较校验密码。记录损坏或算法不认识时返回 False。"""
    try:
        if record.get("algo") != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(record["salt"]),
            n=int(record["n"]), r=int(record["r"]), p=int(record["p"]),
            maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(digest.hex(), record["hash"])
    except (KeyError, ValueError, TypeError):
        return False


# ================================================================
# 用户库 CRUD
# ================================================================

def load_users() -> dict:
    """读取用户库 {username: {role, password: {...}, created_at}}。"""
    conn = get_auth_db()
    users = {}
    try:
        cur = conn.execute("SELECT username, role, password_json, created_at FROM users;")
        for row in cur.fetchall():
            try:
                pw = json.loads(row[2])
            except Exception:
                pw = {}
            users[row[0]] = {
                "role": row[1],
                "password": pw,
                "created_at": float(row[3]),
            }
    except Exception:
        pass
    return users


def save_users(users: dict) -> None:
    """写入用户库（持久化到 data/auth.db）。"""
    conn = get_auth_db()
    with _lock:
        with conn:
            conn.execute("DELETE FROM users;")
            for uname, info in users.items():
                conn.execute(
                    "INSERT OR REPLACE INTO users (username, role, password_json, created_at) VALUES (?, ?, ?, ?);",
                    (uname, info.get("role", "viewer"), json.dumps(info.get("password", {})), float(info.get("created_at", 0)))
                )


def has_users() -> bool:
    """检查是否存在任何用户。"""
    conn = get_auth_db()
    try:
        cur = conn.execute("SELECT COUNT(*) FROM users;")
        return cur.fetchone()[0] > 0
    except Exception:
        return bool(load_users())


def ensure_initial_admin() -> tuple[bool, str, str]:
    """系统启动时确保存在至少一个管理员账号。

    如果用户库为空，自动创建用户名为 admin 的初始账号并生成强随机密码。
    返回 (created, username, password)。
    若已存在用户，返回 (False, "", "")。
    """
    if has_users():
        return False, "", ""

    # 生成 12 位可读性优良的强随机密码（排除易混淆字符 0, O, 1, I, l）
    alphabet = "23456789abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
    init_pw = "".join(secrets.choice(alphabet) for _ in range(12))

    ok, _ = add_user("admin", init_pw, "admin")
    if ok:
        return True, "admin", init_pw
    return False, "", ""


def add_user(username: str, password: str, role: str) -> tuple[bool, str]:
    """新增用户。返回 (成功, 说明)。"""
    username = (username or "").strip()
    if not username or not username.replace("_", "").replace("-", "").isalnum():
        return False, "用户名只能包含字母、数字、下划线和连字符"
    if role not in ROLES:
        return False, f"角色必须是 {' / '.join(ROLES)}"
    if len(password or "") < MIN_PASSWORD_LEN:
        return False, f"密码至少 {MIN_PASSWORD_LEN} 位"
    users = load_users()
    if username in users:
        return False, f"用户已存在: {username}"
    users[username] = {
        "role": role,
        "password": hash_password(password),
        "created_at": int(time.time()),
    }
    save_users(users)
    return True, f"已创建 {role} 用户: {username}"


def set_password(username: str, password: str) -> tuple[bool, str]:
    """重置/修改密码，并销毁该用户所有活跃会话（强制全端下线）。"""
    if len(password or "") < MIN_PASSWORD_LEN:
        return False, f"密码至少 {MIN_PASSWORD_LEN} 位"
    users = load_users()
    if username not in users:
        return False, f"用户不存在: {username}"
    users[username]["password"] = hash_password(password)
    save_users(users)
    destroy_user_sessions(username)
    return True, f"已重置密码: {username}"


def delete_user(username: str) -> tuple[bool, str]:
    """删除用户，并销毁其所有活跃会话。"""
    users = load_users()
    if username not in users:
        return False, f"用户不存在: {username}"
    if users[username].get("role") == "admin" and \
            sum(1 for u in users.values() if u.get("role") == "admin") <= 1:
        return False, "不能删除最后一个 admin 用户（否则无人能进管理端）"
    del users[username]
    save_users(users)
    destroy_user_sessions(username)
    return True, f"已删除用户: {username}"


def set_role(username: str, role: str) -> tuple[bool, str]:
    """修改用户角色，并同步更新该用户所有活跃会话的角色权限。"""
    if role not in ROLES:
        return False, f"角色必须是 {' / '.join(ROLES)}"
    users = load_users()
    if username not in users:
        return False, f"用户不存在: {username}"
    if users[username].get("role") == "admin" and role != "admin" and \
            sum(1 for u in users.values() if u.get("role") == "admin") <= 1:
        return False, "不能降级最后一个 admin 用户"
    users[username]["role"] = role
    save_users(users)
    with _lock:
        _load_sessions_from_db()
        for s in _sessions.values():
            if s.get("username") == username:
                s["role"] = role
        conn = get_auth_db()
        try:
            with conn:
                conn.execute("UPDATE sessions SET role = ? WHERE username = ?;", (role, username))
        except Exception:
            pass
    return True, f"{username} 角色已改为 {role}"


# ================================================================
# 登录限流
# ================================================================

def is_locked_out(ip: str) -> float:
    """返回剩余锁定秒数，0 表示未锁定。"""
    with _lock:
        until = _locked_until.get(ip, 0)
    remaining = until - time.time()
    return max(0.0, remaining)


def record_failure(ip: str) -> None:
    now = time.time()
    with _lock:
        hits = [t for t in _failures.get(ip, []) if now - t < LOCK_WINDOW]
        hits.append(now)
        _failures[ip] = hits
        if len(hits) >= MAX_FAILURES:
            _locked_until[ip] = now + LOCK_SECONDS
            _failures[ip] = []


def clear_failures(ip: str) -> None:
    with _lock:
        _failures.pop(ip, None)
        _locked_until.pop(ip, None)


# ================================================================
# 会话管理（内存高速缓存 + data/auth.db 持久化）
# ================================================================

def _load_sessions_from_db() -> None:
    """系统启动或首次使用时从 data/auth.db 加载未过期的活跃会话到内存中。"""
    global _sessions_loaded_from_db
    if _sessions_loaded_from_db:
        return
    conn = get_auth_db()
    now = time.time()
    try:
        with conn:
            # 清理数据库中已过期的失效会话
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?;", (now,))
            cur = conn.execute("SELECT token, username, role, expires_at FROM sessions WHERE expires_at > ?;", (now,))
            for row in cur.fetchall():
                _sessions[row[0]] = {
                    "username": row[1],
                    "role": row[2],
                    "expires_at": float(row[3]),
                }
    except Exception:
        pass
    _sessions_loaded_from_db = True


def authenticate(username: str, password: str) -> dict | None:
    """校验用户名密码，成功返回 {username, role}。"""
    users = load_users()
    record = users.get(username or "")
    if record is None:
        # 用户不存在也做一次哈希，避免通过响应时间区分用户是否存在
        hash_password(password or "x")
        return None
    if not verify_password(password or "", record.get("password") or {}):
        return None
    return {"username": username, "role": record.get("role", "viewer")}


def create_session(username: str, role: str, ttl_seconds: int) -> str:
    """创建并持久化会话。"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires_at = now + ttl_seconds
    with _lock:
        _load_sessions_from_db()
        _sessions[token] = {
            "username": username,
            "role": role,
            "expires_at": expires_at,
        }
        conn = get_auth_db()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sessions (token, username, role, expires_at, created_at) VALUES (?, ?, ?, ?, ?);",
                    (token, username, role, expires_at, now),
                )
        except Exception:
            pass
    return token


def get_session(token: str, ttl_seconds: int = 0) -> dict | None:
    """取会话（顺带清理过期项）。ttl_seconds > 0 时滑动续期。"""
    if not token:
        return None
    now = time.time()
    with _lock:
        _load_sessions_from_db()
        for t in [t for t, s in _sessions.items() if s["expires_at"] <= now]:
            _sessions.pop(t, None)
        sess = _sessions.get(token)
        if sess is None:
            # 内存未命中，尝试从 auth.db 读取
            conn = get_auth_db()
            try:
                cur = conn.execute(
                    "SELECT username, role, expires_at FROM sessions WHERE token = ? AND expires_at > ?;",
                    (token, now),
                )
                row = cur.fetchone()
                if row:
                    sess = {
                        "username": row[0],
                        "role": row[1],
                        "expires_at": float(row[2]),
                    }
                    _sessions[token] = sess
            except Exception:
                pass
        if sess is None:
            return None
        if ttl_seconds > 0:
            new_exp = now + ttl_seconds
            sess["expires_at"] = new_exp
            conn = get_auth_db()
            try:
                with conn:
                    conn.execute("UPDATE sessions SET expires_at = ? WHERE token = ?;", (new_exp, token))
            except Exception:
                pass
        return dict(sess)


def destroy_session(token: str) -> None:
    """注销并持久化删除单条会话。"""
    if not token:
        return
    with _lock:
        _load_sessions_from_db()
        _sessions.pop(token, None)
        conn = get_auth_db()
        try:
            with conn:
                conn.execute("DELETE FROM sessions WHERE token = ?;", (token,))
        except Exception:
            pass


def destroy_user_sessions(username: str) -> None:
    """销毁指定用户的所有活跃会话（改密/删号/强制下线时触发）。"""
    with _lock:
        _load_sessions_from_db()
        for token in [t for t, s in _sessions.items() if s.get("username") == username]:
            _sessions.pop(token, None)
        conn = get_auth_db()
        try:
            with conn:
                conn.execute("DELETE FROM sessions WHERE username = ?;", (username,))
        except Exception:
            pass


def session_count() -> int:
    """返回当前活跃会话数量。"""
    with _lock:
        _load_sessions_from_db()
        now = time.time()
        for t in [t for t, s in _sessions.items() if s["expires_at"] <= now]:
            _sessions.pop(t, None)
        return len(_sessions)
