"""
social/ig_session.py — Instagram 登录态（cookies）管理与健康检测

**为什么只要 cookies**：Instagram 已对匿名接口全面 429，必须带会话态；而用
账号密码做程序化登录几乎必然触发风控（新设备 + 代理出口 IP → checkpoint）。
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
import time

import requests

log = logging.getLogger("collink")

COOKIE_FILE = os.path.join("data", "instagram_cookies.txt")
STATE_FILE = os.path.join("data", "instagram_session.json")

# 用来判断会话是否还活着的接口。
#
# ⚠️ 不能用 web_profile_info —— 实测它**无论登录态是否有效都返回 429**
# （接口/IP 级限流），拿它做检测会把好端端的会话误判成「已失效」。
# accounts/edit/web_form_data 是纯登录态接口：有会话返回 200 + form_data，
# 没有会话返回 401/403，判定干净。
CHECK_URL = "https://www.instagram.com/api/v1/accounts/edit/web_form_data/"
IG_APP_ID = "936619743392459"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 真正决定登录态的关键 cookie
REQUIRED = ("sessionid",)
USEFUL = ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur", "datr")


# ── 解析 ──────────────────────────────────────────────────

def parse_cookies(raw: str) -> dict:
    """把用户粘贴的内容解析成 {name: value}。

    支持四种常见形态，用户不用关心自己复制的是哪种：
      1. Netscape cookies.txt（浏览器扩展导出）
      2. 请求头字符串：`sessionid=xxx; csrftoken=yyy`
      3. JSON 数组（EditThisCookie 等扩展的导出格式）
      4. 只有一串 sessionid 的值
    """
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
            # 过滤掉明显不是 cookie 的行（比如 curl 命令里的 header）
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


# 后台「逐项填写」表单的字段定义：顺序 / 是否必填 / 说明 / 示例
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
     "level": "强烈建议", "desc": "设备标识。**风控关键** —— 缺失会让请求"
                                  "看起来来自一台陌生设备。",
     "example": "aBcDeF…"},
    {"key": "ig_did", "label": "ig_did", "required": False,
     "level": "强烈建议", "desc": "设备指纹。**风控关键**，作用同 mid。",
     "example": "12345678-ABCD-…"},
    {"key": "rur", "label": "rur", "required": False,
     "level": "可选", "desc": "路由标识，缺失影响很小。",
     "example": "PRN,1234…"},
    {"key": "datr", "label": "datr", "required": False,
     "level": "可选", "desc": "浏览器标识，缺失影响很小。",
     "example": "aBcDeF…"},
]

# 各 cookie 的作用，用于向用户解释「为什么不能只给 sessionid」
COOKIE_ROLE = {
    "sessionid": "登录凭证（必需）",
    "csrftoken": "CSRF 令牌 —— 缺失会导致大量接口直接拒绝",
    "ds_user_id": "用户 ID —— 部分接口据此校验",
    "mid": "设备标识 —— 缺失会让请求看起来来自陌生设备",
    "ig_did": "设备指纹 —— 同上，是风控的关键信号",
    "rur": "路由标识 —— 缺失属轻微异常",
    "datr": "浏览器标识 —— 缺失属轻微异常",
}
# 缺了会显著抬高风控概率的（不只是功能问题）
CRITICAL_FOR_RISK = ("csrftoken", "mid", "ig_did")


def assess(cookies: dict) -> dict:
    """评估 cookie 完整度，并给出风控层面的判断。

    **只给 sessionid 反而更危险**：mid / ig_did 是设备标识，
    带着 sessionid 却不带设备标识，在 Instagram 看来就是
    「这个会话突然出现在一台从没见过的设备上」—— 这正是风控要抓的异常信号。
    """
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
        "partial": f"缺少 {', '.join(missing_critical)}，"
                   f"建议补齐后再用，否则风控概率上升。",
        "minimal": "⚠️ 只有登录凭证、缺少设备标识（mid / ig_did）与 CSRF 令牌。"
                   "带着 sessionid 却不带设备标识，在 Instagram 看来等于"
                   "「会话突然出现在陌生设备上」，**这恰恰是风控最敏感的信号**，"
                   "比完整导出更容易触发验证。强烈建议导出完整 cookies。",
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


# ── 落盘 ──────────────────────────────────────────────────

def write_cookie_file(cookies: dict, path: str = COOKIE_FILE) -> str:
    """写成 Netscape 格式（yt-dlp 的 cookiefile 要求这个格式）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    expiry = int(time.time()) + 400 * 86400   # 与 IG 自身下发的 400 天一致
    lines = ["# Netscape HTTP Cookie File",
             "# 由 collink 后台生成，请勿手工编辑"]
    for name, value in cookies.items():
        if not name or value is None:
            continue
        lines.append("\t".join([".instagram.com", "TRUE", "/", "TRUE",
                                str(expiry), str(name), str(value)]))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)      # 尽量收紧权限：sessionid 等同于登录态
    except OSError:
        pass
    return path


def read_cookie_file(path: str = COOKIE_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            cookies = parse_cookies(f.read())
            if cookies:
                return cookies
    except OSError:
        pass

    # 尝试从环境变量 INSTAGRAM_SESSIONID 回退读取
    env_sid = os.getenv("INSTAGRAM_SESSIONID", "").strip()
    if env_sid:
        cookies = {"sessionid": env_sid}
        # 自动提取 ds_user_id (sessionid 开头以 %3A 或 : 分割的部分)
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

        # 自动同步写入 Netscape 格式文件，确保 yt-dlp 与其他组件可直接调用
        try:
            write_cookie_file(cookies, path)
        except Exception:
            pass
        return cookies

    return {}


# ── 健康检测 ──────────────────────────────────────────────

def check_session(cookies: dict | None = None, *, proxy: str = "",
                  user_agent: str = "") -> dict:
    """检测登录态是否仍然有效。

    注意：Instagram 对普通 requests 库（缺浏览器指纹）直接返回 403，
    所以这里用 curl_cffi（和 InstagramFetcher 同款）来检测，
    避免误报。

    :return: {"valid", "status", "detail", "username", "user_id"}
    """
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
    try:
        from curl_cffi import requests as curl_requests
        r = curl_requests.get(CHECK_URL, headers={
            "User-Agent": ua,
            "X-IG-App-ID": IG_APP_ID,
            "Accept": "*/*",
            "Referer": "https://www.instagram.com/",
            "X-CSRFToken": cookies.get("csrftoken", ""),
        }, cookies=cookies, proxies=proxy, timeout=25, impersonate="chrome")
    except ImportError:
        # 没有 curl_cffi 时 fallback 到 requests
        headers = {
            "User-Agent": ua,
            "X-IG-App-ID": IG_APP_ID,
            "Accept": "*/*",
            "Referer": "https://www.instagram.com/",
        }
        if cookies.get("csrftoken"):
            headers["X-CSRFToken"] = cookies["csrftoken"]
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            r = requests.get(CHECK_URL, headers=headers, cookies=cookies,
                             proxies=proxies, timeout=25)
        except Exception as e:
            return {"valid": False, "status": -1,
                    "detail": f"网络错误：{str(e)[:120]}",
                    "username": "", "user_id": cookies.get("ds_user_id", "")}
    except Exception as e:
        return {"valid": None, "status": -1,
                "detail": f"检测请求失败：{str(e)[:120]}",
                "username": "", "user_id": cookies.get("ds_user_id", "")}

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
        # 429 是**限流**，不代表登录态失效 —— 不能据此判死
        return {"valid": None, "status": 429,
                "detail": "接口限流（429），无法判定登录态。这通常是访问过于频繁"
                          "或该接口对当前 IP 限流，稍后再测即可",
                "username": "", "user_id": uid}
    return {"valid": None, "status": r.status_code,
            "detail": f"无法判定（HTTP {r.status_code}）",
            "username": "", "user_id": uid}


# ── 状态持久化（用于失效告警只发一次）──────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(**fields) -> dict:
    st = load_state()
    st.update(fields)
    st["updated_at"] = time.time()
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)
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
        "cookie_file": COOKIE_FILE if os.path.exists(COOKIE_FILE) else "",
        "cookie_names": sorted(cookies.keys()),
        "has_sessionid": bool(cookies.get("sessionid")),
        "user_id": cookies.get("ds_user_id", ""),
        "valid": st.get("valid"),
        "reason": st.get("reason", ""),
        "last_check": st.get("last_check", 0),
        "failed_at": st.get("failed_at", 0),
        "updated_at": st.get("updated_at", 0),
        "saved_at": st.get("saved_at", 0),
    }


def clear() -> None:
    for p in (COOKIE_FILE, STATE_FILE):
        try:
            os.unlink(p)
        except OSError:
            pass
