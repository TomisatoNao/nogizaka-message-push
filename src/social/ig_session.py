"""
social/ig_session.py — Instagram 登录态（cookies）管理与健康检测

**为什么仍然需要 cookies**：Instagram 的账号 Feed、Story 及受限内容接口要求
会话态；公开单帖另有匿名 Embed 回退。用账号密码做程序化登录几乎必然触发风控
（新设备 + 代理出口 IP → checkpoint）。
复用浏览器里已经建立好的会话则不产生「登录」事件，是风险最低的方式。
因此本项目**没有任何 Instagram 密码登录路径**，只吃 cookies。

本模块负责：
  * 解析用户粘贴的各种格式（cookies.txt / 请求头字符串 / JSON 导出 / 裸 sessionid）
  * 写成 yt-dlp 能用的 Netscape 格式文件
  * 检测会话是否仍然有效（登录态专属接口）
  * 记录健康状态，失效时只告警一次，避免刷屏
"""

import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from collections.abc import Iterator

import requests

from src.social import sqlite_utils

log = logging.getLogger("collink")

DB_PATH = os.path.join("data", "social.db")
# 保留变量别名以防外部旧代码导入，但内部不再创建文件
COOKIE_FILE = os.path.join("data", "instagram_cookies.txt")
STATE_FILE = os.path.join("data", "instagram_session.json")

# 用来判断会话是否还活着的接口。
CHECK_URL = "https://www.instagram.com/api/v1/accounts/edit/web_form_data/"
IG_APP_ID = "936619743392459"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 真正决定登录态的关键 cookie
REQUIRED = ("sessionid",)
USEFUL = ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur", "datr")


# ── SQLite 数据库操作 ───────────────────────────────────────

def _get_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite_utils.connect(DB_PATH)
    conn.row_factory = sqlite_utils.sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ig_session (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ig_session_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        state_json TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    """)
    conn.commit()
    return conn


def _cleanup_legacy_files() -> None:
    """清理历史旧版磁盘明文文件，实现零明文落盘。"""
    for legacy_p in (
        os.path.join("data", "instagram_cookies.txt"),
        os.path.join("data", "instagram_cookies.txt.tmp"),
        os.path.join("data", "instagram_session.json"),
        os.path.join("data", "instagram_session.json.tmp"),
    ):
        try:
            if os.path.exists(legacy_p):
                os.remove(legacy_p)
        except OSError:
            pass


# ── 解析 ──────────────────────────────────────────────────

def parse_cookies(raw: str) -> dict:
    """把用户粘贴的内容解析成 {name: value}。"""
    raw = (raw or "").strip()
    if not raw:
        return {}

    # 3) JSON 数组
    if raw.startswith("["):
        try:
            out = {}
            for c in json.loads(raw):
                if isinstance(c, dict) and c.get("name"):
                    out[c["name"]] = str(c.get("value", ""))
            if out:
                return out
        except ValueError:
            pass

    # 1) Netscape 格式（含 TAB 分隔的行）
    if "\t" in raw or "#HttpOnly_" in raw or raw.lstrip().startswith("# Netscape"):
        out = {}
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out[parts[5]] = parts[6]
        if out:
            return out

    # 2) 请求头字符串
    if "=" in raw:
        out = {}
        for piece in re.split(r"[;\n]", raw):
            piece = piece.strip()
            if piece.startswith("#HttpOnly_"):
                piece = piece[len("#HttpOnly_"):]
            elif not piece or piece.startswith("#"):
                continue
            if "\t" in piece:
                parts = piece.split("\t")
                if len(parts) >= 7:
                    out[parts[5]] = parts[6]
                    continue
            if "=" not in piece:
                continue
            k, v = piece.split("=", 1)
            k, v = k.strip(), v.strip().strip('"')
            if k and " " not in k and v:
                out[k] = v
        if out:
            return out

    # 4) 裸 sessionid
    if len(raw) > 20 and " " not in raw:
        return {"sessionid": raw}
    return {}


def missing_required(cookies: dict) -> list:
    return [k for k in REQUIRED if not cookies.get(k)]


COOKIE_FIELDS = [
    {"key": "sessionid", "label": "sessionid", "required": True,
     "level": "必需", "desc": "登录凭证。整个登录态就是它，等同于密码，切勿外传。",
     "example": "7712…%3AAbCd…%3A17%3AAY…"},
    {"key": "csrftoken", "label": "csrftoken", "required": True,
     "level": "必需", "desc": "CSRF 令牌。缺失会导致大量接口直接拒绝请求。",
     "example": "aBcDeFgHiJkLmNoP"},
    {"key": "ds_user_id", "label": "ds_user_id", "required": False,
     "level": "强烈建议", "desc": "你的数字用户 ID，部分接口据此校验。",
     "example": "1234567890"},
    {"key": "mid", "label": "mid", "required": False,
     "level": "强烈建议", "desc": "设备标识。风控关键 —— 缺失会让请求看起来来自陌生设备。",
     "example": "aBcDeF…"},
    {"key": "ig_did", "label": "ig_did", "required": False,
     "level": "强烈建议", "desc": "设备指纹。风控关键，作用同 mid。",
     "example": "12345678-ABCD-…"},
    {"key": "rur", "label": "rur", "required": False,
     "level": "可选", "desc": "路由标识，缺失影响很小。",
     "example": "PRN,1234…"},
    {"key": "datr", "label": "datr", "required": False,
     "level": "可选", "desc": "浏览器标识，缺失影响很小。",
     "example": "aBcDeF…"},
]

COOKIE_ROLE = {
    "sessionid": "登录凭证（必需）",
    "csrftoken": "CSRF 令牌 —— 缺失会导致大量接口直接拒绝",
    "ds_user_id": "用户 ID —— 部分接口据此校验",
    "mid": "设备标识 —— 缺失会让请求看起来来自陌生设备",
    "ig_did": "设备指纹 —— 同上，是风控的关键信号",
    "rur": "路由标识 —— 缺失属轻微异常",
    "datr": "浏览器标识 —— 缺失属轻微异常",
}
CRITICAL_FOR_RISK = ("csrftoken", "mid", "ig_did")


def assess(cookies: dict) -> dict:
    """评估 cookie 完整度，并给出风控层面的判断。"""
    present = [k for k in USEFUL if cookies.get(k)]
    missing = [k for k in USEFUL if not cookies.get(k)]
    missing_critical = [k for k in CRITICAL_FOR_RISK if not cookies.get(k)]

    if missing_critical:
        level = "minimal" if len(missing_critical) >= 2 else "partial"
    elif missing:
        level = "good"
    else:
        level = "full"

    advice = {
        "full": "cookie 完整，风控层面最接近真实浏览器。",
        "good": "关键 cookie 齐全，可正常使用。",
        "partial": f"缺少 {', '.join(missing_critical)}，建议补齐后再用，否则风控概率上升。",
        "minimal": "⚠️ 只有登录凭证、缺少设备标识（mid / ig_did）与 CSRF 令牌。建议导出完整 cookies。",
    }[level]

    return {
        "level": level,
        "present": present,
        "missing": missing,
        "missing_critical": missing_critical,
        "roles": {k: COOKIE_ROLE.get(k, "") for k in missing},
        "advice": advice,
        "safe_enough": level in ("full", "good"),
    }


# ── SQLite 数据库持久化 ────────────────────────────────────

def write_cookie_file(cookies: dict, path: str = "") -> str:
    """将 cookies 写入 SQLite 数据库持久化，并自动清除磁盘残留明文文件。"""
    conn = _get_db()
    now = time.time()
    with conn:
        conn.execute("DELETE FROM ig_session")
        for k, v in (cookies or {}).items():
            if k and v is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO ig_session (key, value, updated_at) VALUES (?, ?, ?)",
                    (str(k), str(v), now),
                )
    _cleanup_legacy_files()
    return f"sqlite:{DB_PATH}#ig_session"


def read_cookie_file(path: str = "") -> dict:
    """读取 cookies：显式文件优先，其次 SQLite，并支持旧文件静默迁移。"""
    # A configured cookies_file is an explicit source and must be honoured.
    # Older versions ignored this argument and silently read the SQLite
    # session instead, which made a newly exported file appear ineffective.
    if path:
        configured = os.path.expanduser(str(path).strip())
        if os.path.exists(configured):
            try:
                with open(configured, encoding="utf-8", errors="replace") as f:
                    parsed = parse_cookies(f.read())
                if parsed:
                    return parsed
            except OSError:
                pass

    conn = _get_db()
    rows = conn.execute("SELECT key, value FROM ig_session").fetchall()
    cookies = {row["key"]: row["value"] for row in rows}
    if cookies:
        return cookies

    # 启动时静默自愈：若 SQLite 为空，检查是否有旧版 txt 文件或环境变量可迁移
    legacy_file = os.path.join("data", "instagram_cookies.txt")
    if os.path.exists(legacy_file):
        try:
            with open(legacy_file, encoding="utf-8") as f:
                parsed = parse_cookies(f.read())
                if parsed:
                    write_cookie_file(parsed)
                    return parsed
        except OSError:
            pass

    env_sid = os.getenv("INSTAGRAM_SESSIONID", "").strip()
    if env_sid:
        cookies = {"sessionid": env_sid}
        if "%3A" in env_sid:
            uid = env_sid.split("%3A")[0]
            if uid.isdigit():
                cookies["ds_user_id"] = uid
        elif ":" in env_sid:
            uid = env_sid.split(":")[0]
            if uid.isdigit():
                cookies["ds_user_id"] = uid
        env_uid = os.getenv("INSTAGRAM_DS_USER_ID", "").strip()
        if env_uid:
            cookies["ds_user_id"] = env_uid
        write_cookie_file(cookies)
        return cookies

    return {}


def read_browser_cookies(browser: str) -> dict:
    """从已登录浏览器读取 Instagram cookies（不接触账号密码）。"""
    browser = (browser or "").strip().lower()
    if not browser:
        return {}
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = extract_cookies_from_browser(browser)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        log.debug("[instagram] 无法读取 %s 浏览器 cookies: %s", browser, str(exc)[:160])
        return {}
    out = {}
    for cookie in jar:
        domain = (cookie.domain or "").lower().lstrip(".")
        if domain == "instagram.com" or domain.endswith(".instagram.com"):
            out[cookie.name] = cookie.value
    return out


def resolve_cookies(path: str = "", browser: str = "") -> dict:
    """按显式文件 → SQLite → 浏览器的顺序取得一套 cookies。"""
    cookies = read_cookie_file(path)
    if cookies:
        return cookies
    return read_browser_cookies(browser)


def create_temp_cookie_file(cookies: dict | None = None) -> str:
    """创建供 yt-dlp 使用的短生命周期 Netscape cookies 文件。

    登录态长期存放在 SQLite；此文件只在一次 yt-dlp 调用期间存在，权限
    设为当前用户可读写，调用方必须在 finally 中删除（见下方上下文管理器）。
    """
    cookies = cookies or read_cookie_file()
    if not cookies:
        return ""
    fd, path = tempfile.mkstemp(prefix="collink-instagram-", suffix=".cookies.txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# Netscape HTTP Cookie File\n")
            for name, value in cookies.items():
                if not name or value is None:
                    continue
                handle.write(
                    "\t".join((
                        ".instagram.com", "TRUE", "/", "TRUE", "0",
                        str(name), str(value),
                    ))
                    + "\n"
                )
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def remove_temp_cookie_file(path: str) -> None:
    """Best-effort removal of a temporary yt-dlp cookie file."""
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("[instagram] 临时 cookies 文件清理失败: %s", str(exc)[:120])


@contextmanager
def temporary_cookie_file(cookies: dict | None = None) -> Iterator[str]:
    """在 ``with`` 块内提供临时 Netscape cookies 文件。"""
    path = create_temp_cookie_file(cookies)
    try:
        yield path
    finally:
        remove_temp_cookie_file(path)


def get_cookie_header() -> str:
    """获取拼接好的 Cookie 请求头字符串（如 'sessionid=xxx; csrftoken=yyy'）。"""
    cookies = read_cookie_file()
    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# ── 健康检测 ──────────────────────────────────────────────

def check_session(cookies: dict | None = None, *, proxy: str = "",
                  user_agent: str = "") -> dict:
    """检测登录态是否仍然有效。"""
    cookies = cookies or read_cookie_file()
    if not cookies:
        return {"valid": False, "status": 0, "detail": "尚未配置 cookies",
                "username": "", "user_id": ""}
    miss = missing_required(cookies)
    if miss:
        return {"valid": False, "status": 0,
                "detail": f"缺少关键 cookie：{', '.join(miss)}",
                "username": "", "user_id": cookies.get("ds_user_id", "")}

    ua = user_agent or DEFAULT_UA
    proxies_dict = None
    if proxy:
        p_str = str(proxy).strip()
        if p_str:
            proxies_dict = {"http": p_str, "https": p_str, "all": p_str}

    try:
        from curl_cffi import requests as curl_requests
        r = curl_requests.get(
            CHECK_URL,
            headers={
                "User-Agent": ua,
                "X-IG-App-ID": IG_APP_ID,
                "Accept": "*/*",
                "Referer": "https://www.instagram.com/",
                "X-CSRFToken": cookies.get("csrftoken", ""),
            },
            cookies=cookies,
            proxies=proxies_dict,
            timeout=25,
            impersonate="chrome",
        )
    except ImportError:
        headers = {
            "User-Agent": ua,
            "X-IG-App-ID": IG_APP_ID,
            "Accept": "*/*",
            "Referer": "https://www.instagram.com/",
        }
        if cookies.get("csrftoken"):
            headers["X-CSRFToken"] = cookies["csrftoken"]
        try:
            r = requests.get(
                CHECK_URL,
                headers=headers,
                cookies=cookies,
                proxies=proxies_dict,
                timeout=25,
            )
        except Exception as e:
            return {
                "valid": False,
                "status": -1,
                "detail": f"网络错误：{str(e)[:120]}",
                "username": "",
                "user_id": cookies.get("ds_user_id", ""),
            }
    except Exception as e:
        return {
            "valid": None,
            "status": -1,
            "detail": f"检测请求失败：{str(e)[:120]}",
            "username": "",
            "user_id": cookies.get("ds_user_id", ""),
        }

    uid = cookies.get("ds_user_id", "")
    if r.status_code == 200:
        who = ""
        try:
            form = (r.json() or {}).get("form_data") or {}
            who = form.get("username", "")
        except ValueError:
            pass
        return {"valid": True, "status": 200,
                "detail": (f"登录态有效（当前账号 @{who}）" if who
                           else "登录态有效"),
                "username": who, "user_id": uid}
    if r.status_code in (401, 403):
        return {"valid": False, "status": r.status_code,
                "detail": "登录态已失效（被拒绝）—— 需要重新导出 cookies",
                "username": "", "user_id": uid}
    if r.status_code == 429:
        return {"valid": None, "status": 429,
                "detail": "接口限流（429），无法判定登录态。这通常是访问过于频繁"
                          "或该接口对当前 IP 限流，稍后再测即可",
                "username": "", "user_id": uid}
    return {"valid": None, "status": r.status_code,
            "detail": f"无法判定（HTTP {r.status_code}）",
            "username": "", "user_id": uid}


# ── 状态持久化（SQLite） ────────────────────────────────────

def load_state() -> dict:
    conn = _get_db()
    row = conn.execute("SELECT state_json FROM ig_session_state WHERE id = 1").fetchone()
    if row and row["state_json"]:
        try:
            return json.loads(row["state_json"])
        except (json.JSONDecodeError, ValueError):
            pass

    # 静默迁移旧版 session.json
    legacy_state = os.path.join("data", "instagram_session.json")
    if os.path.exists(legacy_state):
        try:
            with open(legacy_state, encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    now = time.time()
                    d["updated_at"] = now
                    with conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO ig_session_state (id, state_json, updated_at) VALUES (1, ?, ?)",
                            (json.dumps(d, ensure_ascii=False), now),
                        )
                    try:
                        os.remove(legacy_state)
                    except OSError:
                        pass
                    return d
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_state(**fields) -> dict:
    conn = _get_db()
    st = {}
    row = conn.execute("SELECT state_json FROM ig_session_state WHERE id = 1").fetchone()
    if row and row["state_json"]:
        try:
            st = json.loads(row["state_json"])
        except (json.JSONDecodeError, ValueError):
            st = {}
    st.update(fields)
    st["updated_at"] = time.time()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO ig_session_state (id, state_json, updated_at) VALUES (1, ?, ?)",
            (json.dumps(st, ensure_ascii=False), st["updated_at"]),
        )
    legacy_state = os.path.join("data", "instagram_session.json")
    if os.path.exists(legacy_state):
        try:
            os.remove(legacy_state)
        except OSError:
            pass
    return st


def mark_invalid(reason: str) -> bool:
    """标记登录态失效。返回 True 表示这是「刚刚失效」（需要告警）。"""
    st = load_state()
    was_valid = st.get("valid", True)
    save_state(valid=False, reason=reason, failed_at=time.time())
    return bool(was_valid)


def mark_valid() -> None:
    st = load_state()
    if not st.get("valid"):
        save_state(valid=True, reason="", recovered_at=time.time(),
                   notified=False)
    else:
        save_state(valid=True, last_ok=time.time())


def status() -> dict:
    """给后台展示的完整状态。"""
    cookies = read_cookie_file()
    st = load_state()
    return {
        "configured": bool(cookies),
        "storage": "sqlite:data/social.db#ig_session",
        "cookie_names": sorted(cookies.keys()),
        "has_sessionid": bool(cookies.get("sessionid")),
        "user_id": cookies.get("ds_user_id", ""),
        "valid": st.get("valid"),
        "reason": st.get("reason", ""),
        "last_check": st.get("last_check", 0),
        "failed_at": st.get("failed_at", 0),
        "updated_at": st.get("updated_at", 0),
        "saved_at": st.get("saved_at", 0),
        "assessment": assess(cookies) if cookies else None,
        "state": st,
    }


def clear() -> None:
    conn = _get_db()
    with conn:
        conn.execute("DELETE FROM ig_session")
        conn.execute("DELETE FROM ig_session_state")
    _cleanup_legacy_files()

