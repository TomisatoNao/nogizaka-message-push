# ============================================================
# credentials.py — 账号凭证管理、Token 刷新、文件锁工具
# ============================================================
import asyncio
import base64
import json
import os
from datetime import datetime, timezone

import httpx

from config.config import ACCOUNTS, ALERT_COOLDOWN_SECONDS, CRED_DIR, TOKEN_REFRESH_BEFORE_SECONDS
from src.logger import log_all, log_response

# ---- 运行时状态 ----
ACCOUNT_CREDS:        dict[str, dict]          = {}
_file_locks:          dict[str, asyncio.Lock]  = {}
_token_refresh_locks: dict[str, asyncio.Lock]  = {}
_alert_last_sent:     dict[str, float]         = {}


# ──────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────
def _clean_cookie_string(raw: str) -> dict[str, str]:
    """将 Set-Cookie 格式字符串解析为键值字典，忽略属性字段。"""
    ignore = {"path", "domain", "expires", "samesite", "secure", "httponly", "max-age"}
    result = {}
    for item in raw.split(";"):
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        if k.strip().lower() not in ignore:
            result[k.strip()] = v.strip()
    return result


def _save_cred(account_id: str, token: str, cookies: dict) -> None:
    os.makedirs(CRED_DIR, exist_ok=True)
    path = os.path.join(CRED_DIR, f"{account_id}.json")
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"token": token, "cookies": cookies}, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ──────────────────────────────────────────────
# 公开 API
# ──────────────────────────────────────────────
def get_web_headers(
    group_type: str,
    token: str | None = None,
    *,
    app_tag: str | None = None,
    api_base: str | None = None,
    web_origin: str | None = None,
) -> dict[str, str]:
    if app_tag is None:
        app_tag = "nogizaka" if group_type == "nogizaka46" else "keyakizaka"
    if web_origin:
        origin  = web_origin
        referer = web_origin.rstrip("/") + "/"
    else:
        origin  = f"https://message.{group_type}.com"
        referer = f"https://message.{group_type}.com/"

    headers = {
        "accept":               "application/json",
        "accept-language":      "zh-CN;q=1,en;q=0.9",
        "content-type":         "application/json",
        "sec-ch-ua":            '"Chromium";v="147", "Google Chrome";v="147", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile":     "?0",
        "sec-ch-ua-platform":   '"Windows"',
        "sec-fetch-dest":       "empty",
        "sec-fetch-mode":       "cors",
        "sec-fetch-site":       "same-site",
        "user-agent":           (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
        "x-talk-app-id":        f"jp.co.sonymusic.communication.{app_tag} 2.5",
        "x-talk-app-platform":  "web",
        "referer":              referer,
        "origin":               origin,
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def get_file_lock(file_path: str) -> asyncio.Lock:
    if file_path not in _file_locks:
        _file_locks[file_path] = asyncio.Lock()
    return _file_locks[file_path]


async def write_time_record(time_file: str, file_lock: asyncio.Lock, updated: str) -> None:
    tmp = time_file + ".tmp"
    async with file_lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(updated)
            os.replace(tmp, time_file)
        except Exception as e:
            log_all(f"🚨 时间戳写入失败 ({time_file}): {e}", is_error=True)
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_all_accounts() -> None:
    """从磁盘加载所有账号凭证，不存在时用初始凭证创建。"""
    os.makedirs(CRED_DIR, exist_ok=True)
    for acc_id, acc_cfg in ACCOUNTS.items():
        if acc_id in ACCOUNT_CREDS:
            continue
        path = os.path.join(CRED_DIR, f"{acc_id}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                ACCOUNT_CREDS[acc_id] = json.load(f)
            log_all(f"📂 读取账号凭证: {acc_id}")
        else:
            cookies = _clean_cookie_string(acc_cfg["init_cookie"])
            ACCOUNT_CREDS[acc_id] = {"token": acc_cfg["init_token"], "cookies": cookies}
            _save_cred(acc_id, acc_cfg["init_token"], cookies)
            log_all(f"📝 初始化账号凭证: {acc_id}")


async def refresh_token(account_id: str, target_group: int, old_token: str | None = None) -> bool:
    """
    刷新指定账号的 Token。
    - 若其他协程已抢先刷新（token 已变），直接返回 True。
    - 失败时触发 QQ 报警（带冷却）。
    """
    # 延迟导入，避免循环依赖
    from src.notifier import send_alert_message

    lock = _token_refresh_locks.setdefault(account_id, asyncio.Lock())
    async with lock:
        cred = ACCOUNT_CREDS.get(account_id)
        if not cred:
            log_all(f"🚨 账号 {account_id} 无凭据", is_error=True)
            return False

        if old_token and cred["token"] != old_token:
            log_all(f"✅ 账号 {account_id} token 已被其他协程刷新，跳过")
            return True

        acc_cfg    = ACCOUNTS[account_id]
        group_type = acc_cfg["group_type"]
        api_base   = acc_cfg.get("api_base")
        if api_base:
            url = f"{api_base}/v2/update_token"
        else:
            url = f"https://api.message.{group_type}.com/v2/update_token"
        cookie_str = "; ".join(f"{k}={v}" for k, v in cred["cookies"].items())
        headers    = get_web_headers(
            group_type,
            app_tag=acc_cfg.get("app_tag"),
            api_base=api_base,
            web_origin=acc_cfg.get("web_origin"),
        )
        headers["cookie"] = cookie_str

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, headers=headers, content=b'{"refresh_token":null}')
            log_response(r.text)

            if r.status_code == 200:
                try:
                    new_token = r.json().get("access_token")
                except ValueError:
                    new_token = None
                    log_all(f"🚨 账号 {account_id} 续期响应不是合法 JSON", is_error=True)

                if new_token:
                    cred["token"] = new_token
                    for sc in r.headers.get_list("set-cookie"):
                        sc = sc.split(";")[0].strip()
                        if "=" in sc:
                            k, v = sc.split("=", 1)
                            cred["cookies"][k] = v
                    _save_cred(account_id, cred["token"], cred["cookies"])
                    log_all(f"✅ 账号 {account_id} 续期成功")
                    return True
                else:
                    log_all(f"🚨 账号 {account_id} 续期响应无 access_token", is_error=True)
            else:
                log_all(f"🚨 账号 {account_id} 续期被拒: HTTP {r.status_code}", is_error=True)

        except Exception as e:
            log_all(f"🔥 账号 {account_id} 续期网络异常: {e}", is_error=True)

    # ---- 续期失败，触发报警 ----
    log_all(f"🚨 致命错误：账号 {account_id} 续期失败，Cookie 可能已死亡", is_error=True)
    now  = datetime.now().timestamp()
    last = _alert_last_sent.get(account_id, 0)
    if now - last > ALERT_COOLDOWN_SECONDS:
        _alert_last_sent[account_id] = now
        try:
            await send_alert_message(
                target_group,
                f"📢 警报：账号 {account_id} 续期失败，Cookie 已死亡！请重新抓包！",
            )
        except Exception:
            pass
    else:
        remaining = int(ALERT_COOLDOWN_SECONDS - (now - last))
        log_all(f"⏳ 账号 {account_id} 报警冷却中，{remaining}s 后可再次通知")

    return False


# ──────────────────────────────────────────────
# 改进 1：Token 主动刷新
# ──────────────────────────────────────────────
def _decode_token_exp(token: str) -> int | None:
    """
    解码 JWT payload，返回 exp 字段（Unix 时间戳）。
    JWT 格式：header.payload.signature，payload 是 base64url 编码的 JSON。
    解码失败时返回 None，不抛出异常。
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        # base64url 解码：补齐 "=" padding 后解码
        padding = "=" * (-len(parts[1]) % 4)
        payload_bytes = base64.urlsafe_b64decode(parts[1] + padding)
        payload = json.loads(payload_bytes)
        exp = int(payload.get("exp", 0))
        return exp if exp > 0 else None
    except Exception:
        return None


def get_token_remaining_seconds(account_id: str) -> float | None:
    """返回指定账号 Token 距过期的剩余秒数，无法解析时返回 None。"""
    cred = ACCOUNT_CREDS.get(account_id)
    if not cred:
        return None
    exp = _decode_token_exp(cred.get("token", ""))
    if exp is None:
        return None
    return exp - datetime.now(timezone.utc).timestamp()


async def proactive_refresh_if_expiring(account_id: str, target_group: int) -> None:
    """
    每轮巡查前调用。
    若 Token 剩余时间 <= TOKEN_REFRESH_BEFORE_SECONDS，主动刷新，
    避免在实际 API 请求时才触发 401 浪费一轮。
    """
    remaining = get_token_remaining_seconds(account_id)
    if remaining is None:
        log_all(f"⚠️ 无法解析 {account_id} 的 Token 过期时间，跳过主动刷新", is_debug=True)
        return
    if remaining <= TOKEN_REFRESH_BEFORE_SECONDS:
        log_all(
            f"🔄 {account_id} Token 剩余 {int(remaining)}s"
            f"（阈值 {TOKEN_REFRESH_BEFORE_SECONDS}s），主动刷新...",
        )
        await refresh_token(account_id, target_group)
    else:
        log_all(
            f"✅ {account_id} Token 剩余 {int(remaining // 60)}min，无需刷新",
            is_debug=True,
        )
