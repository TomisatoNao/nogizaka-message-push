# ============================================================
# credentials.py — 账号凭证管理、Token 刷新、文件锁工具
# ============================================================
import asyncio
import base64
import binascii
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import httpx

# 统一通过 cfg.X 访问，热重载后标量值（告警冷却、刷新阈值等）才能生效
import config.config as cfg
from src.health import ErrorTier, get_tracker as _health_tracker
from src.logger import format_httpx_error, log_all, log_response

# ---- 运行时状态 ----
ACCOUNT_CREDS:        dict[str, dict]          = {}
_file_locks:          dict[tuple, asyncio.Lock]  = {}
_token_refresh_locks: dict[tuple, asyncio.Lock]  = {}
_alert_last_sent:     dict[str, float]         = {}
_http_client:         httpx.AsyncClient | None = None
_auth_http_client:    httpx.AsyncClient | None = None
_last_time_written:   dict[str, str]           = {}   # write_time_record 的值缓存

# 续期失败状态只保存在进程内：它是网络熔断/退避状态，不是凭据本身。
# kind: transient_network / credential_invalid / response_invalid / persistence_failure
_refresh_state:       dict[str, dict]           = {}
_refresh_semaphores:  dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore, int]] = {}


def _get_refresh_lock(account_id: str) -> asyncio.Lock:
    try:
        loop = asyncio.get_running_loop()
        key = (id(loop), account_id)
    except RuntimeError:
        key = (0, account_id)
    if key not in _token_refresh_locks:
        _token_refresh_locks[key] = asyncio.Lock()
    return _token_refresh_locks[key]


def _get_file_lock(filepath: str) -> asyncio.Lock:
    try:
        loop = asyncio.get_running_loop()
        key = (id(loop), filepath)
    except RuntimeError:
        key = (0, filepath)
    if key not in _file_locks:
        _file_locks[key] = asyncio.Lock()
    return _file_locks[key]


def initialize(
    client: httpx.AsyncClient,
    *,
    auth_client: httpx.AsyncClient | None = None,
) -> None:
    """注入普通请求与认证请求客户端。

    ``auth_client`` 可选：主程序为 Token 续期提供独立连接池，避免媒体/翻译
    请求占满普通连接池。单独运行工具时未传入则回退到 ``client``。
    """
    global _http_client, _auth_http_client
    _http_client = client
    _auth_http_client = auth_client


def _get_refresh_semaphore() -> asyncio.Semaphore:
    """按事件循环创建 Token 续期并发闸门，防止多账号同时冲击代理。"""
    loop = asyncio.get_running_loop()
    try:
        limit = max(1, int(getattr(cfg, "TOKEN_REFRESH_CONCURRENCY", 2)))
    except (TypeError, ValueError):
        limit = 2
    key = id(loop)
    current = _refresh_semaphores.get(key)
    if current is None or current[0] is not loop or current[2] != limit:
        semaphore = asyncio.Semaphore(limit)
        _refresh_semaphores[key] = (loop, semaphore, limit)
        return semaphore
    return current[1]


def _network_cooldown_seconds(failure_count: int) -> float:
    """计算网络型续期失败的指数退避时间，并限制最大值。"""
    try:
        base = max(5.0, float(getattr(cfg, "TOKEN_REFRESH_NETWORK_COOLDOWN_SECONDS", 90)))
    except (TypeError, ValueError):
        base = 90.0
    try:
        ceiling = max(base, float(getattr(cfg, "TOKEN_REFRESH_MAX_COOLDOWN_SECONDS", 600)))
    except (TypeError, ValueError):
        ceiling = 600.0
    return min(ceiling, base * (2 ** max(0, failure_count - 1)))


def _record_refresh_failure(account_id: str, kind: str, detail: str) -> dict:
    """记录账号续期失败并返回当前状态；不记录任何 Token/Cookie 内容。"""
    now = time.monotonic()
    previous = _refresh_state.get(account_id, {})
    count = int(previous.get("failure_count", 0)) + 1 if previous.get("kind") == kind else 1
    if kind == "credential_invalid":
        blocked_until = float("inf")
    else:
        blocked_until = now + _network_cooldown_seconds(count)
    state = {
        "kind": kind,
        "detail": detail[:240],
        "failure_count": count,
        "failed_at": now,
        "blocked_until": blocked_until,
        # 凭据对象被管理端轮换/测试替换后，旧的熔断状态自动失效。
        "cred_ref": id(ACCOUNT_CREDS.get(account_id)),
    }
    _refresh_state[account_id] = state
    # 网络/上游暂态不应污染为 PERSISTENT；凭据、落盘和响应结构问题需要人工关注。
    _health_tracker().record_error(
        f"账号 {account_id} Token 续期失败 [{kind}]: {detail[:160]}",
        ErrorTier.TRANSIENT if kind == "transient_network" else ErrorTier.PERSISTENT,
    )
    return state


def clear_refresh_state(account_id: str) -> None:
    """凭据被更新/轮换后清除熔断状态，允许立即重新握手。"""
    _refresh_state.pop(account_id, None)
    # 新凭据是一条新的故障生命周期，不能被旧凭据的告警冷却吞掉。
    _alert_last_sent.pop(account_id, None)


def get_refresh_state(account_id: str) -> dict:
    """返回账号续期状态的安全副本（不包含凭据）。"""
    raw_state = _refresh_state.get(account_id, {})
    if raw_state and raw_state.get("cred_ref") != id(ACCOUNT_CREDS.get(account_id)):
        _refresh_state.pop(account_id, None)
        raw_state = {}
    state = dict(raw_state)
    if not state:
        return {"kind": "available", "blocked": False, "cooldown_remaining": 0.0}
    blocked_until = state.get("blocked_until", 0.0)
    remaining = float("inf") if blocked_until == float("inf") else max(0.0, float(blocked_until) - time.monotonic())
    state.pop("cred_ref", None)
    state["blocked"] = remaining > 0
    state["cooldown_remaining"] = remaining
    return state


def is_account_fetch_available(account_id: str) -> tuple[bool, str]:
    """判断账号是否允许发起成员抓取，并返回不可用原因。"""
    state = get_refresh_state(account_id)
    if not state.get("blocked"):
        return True, ""
    kind = state.get("kind", "unknown")
    remaining = state.get("cooldown_remaining", 0.0)
    if remaining == float("inf"):
        return False, f"续期确认凭据失效（{kind}）"
    return False, f"续期临时失败（{kind}，{int(remaining)}s 后重试）"


def _classify_refresh_status(status_code: int, body: str) -> str:
    """把 HTTP 续期响应分为认证失败或可恢复的上游故障。"""
    if status_code in (401, 403):
        return "credential_invalid"
    if status_code in (408, 425, 429) or status_code >= 500:
        return "transient_network"
    # 400 常见于 refresh token / Cookie 被服务端拒绝；其它 4xx 视为响应/配置问题。
    if status_code == 400:
        return "credential_invalid"
    return "response_invalid"


async def _report_refresh_failure(
    account_id: str,
    target_group: int,
    *,
    kind: str,
    detail: str,
    platform: str,
) -> None:
    """记录、分级并按冷却发送续期告警。"""
    from src.notifier import send_alert_message

    previous_kind = _refresh_state.get(account_id, {}).get("kind")
    state = _record_refresh_failure(account_id, kind, detail)
    if kind == "transient_network":
        cooldown = int(state.get("cooldown_remaining", 0))
        log_all(
            f"⚠️ 账号 {account_id} {platform} 续期暂时失败（{detail}），"
            f"未判定凭据失效；{cooldown}s 后自动重试",
            is_error=True,
        )
        alert_text = (
            f"📢 提示：账号 {account_id} {platform} 续期网络失败（{detail}）。"
            f"未判定 Cookie/Token 失效，将在约 {cooldown}s 后自动重试。"
        )
    elif kind == "credential_invalid":
        log_all(
            f"🚨 账号 {account_id} {platform} 续期被拒，凭据可能已失效（{detail}）",
            is_error=True,
        )
        alert_text = f"📢 警报：账号 {account_id} {platform} 续期被认证服务拒绝，Cookie/Token 可能已失效，请更新凭据。"
    else:
        log_all(
            f"🚨 账号 {account_id} {platform} 续期失败（{kind}: {detail}）",
            is_error=True,
        )
        alert_text = f"📢 警报：账号 {account_id} {platform} 续期处理失败（{detail}），请检查 Cookie/Token 持久化和接口响应。"

    now = datetime.now().timestamp()
    last = _alert_last_sent.get(account_id, 0)
    # 从暂态网络故障升级为明确认证拒绝时，必须立即通知，不能受旧告警冷却影响。
    if kind == "credential_invalid" and previous_kind != kind:
        last = 0
    if now - last <= cfg.ALERT_COOLDOWN_SECONDS:
        remaining = int(cfg.ALERT_COOLDOWN_SECONDS - (now - last))
        _health_tracker().record_alert_cooldown(account_id, float(remaining))
        log_all(f"⏳ 账号 {account_id} 报警冷却中，{remaining}s 后可再次通知")
        return

    _alert_last_sent[account_id] = now
    try:
        await send_alert_message(target_group, alert_text)
    except Exception as exc:  # 告警发送失败不能覆盖原始凭证故障。
        log_all(
            f"⚠️ 账号 {account_id} {platform} 续期告警发送失败: {type(exc).__name__}: {exc}",
            is_error=True,
        )


async def _post(url: str, *, headers: dict,
                json_body: dict | None = None,
                content: bytes | None = None) -> httpx.Response:
    client_to_use = None
    try:
        curr_loop = asyncio.get_running_loop()
    except RuntimeError:
        curr_loop = None

    # 续期优先使用隔离的认证连接池；单独脚本未注入时回退普通客户端。
    for candidate in (_auth_http_client, _http_client):
        if candidate is None or candidate.is_closed or curr_loop is None:
            continue
        transport = getattr(candidate, "_transport", None)
        client_loop = getattr(transport, "_loop", None)
        if client_loop is None or client_loop is curr_loop:
            client_to_use = candidate
            break

    if client_to_use is not None:
        return await client_to_use.post(
            url, headers=headers, json=json_body, content=content, timeout=15,
        )
    async with httpx.AsyncClient(
        timeout=15,
        proxy=getattr(cfg, "PROXY", "") or None,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        return await client.post(url, headers=headers, json=json_body, content=content)


# ──────────────────────────────────────────────
# 移动端 Group 配置（来源: colmsg src/http/client.rs）
# ──────────────────────────────────────────────
_MOBILE_GROUP_CONFIG: dict[str, dict[str, str]] = {
    "nogizaka":     {"base_url": "https://api.n46.glastonr.net",
                     "app_id":  "jp.co.sonymusic.communication.nogizaka 2.4"},
    "hinatazaka":   {"base_url": "https://api.kh.glastonr.net",
                     "app_id":  "jp.co.sonymusic.communication.keyakizaka 2.4"},
    "sakurazaka":   {"base_url": "https://api.s46.glastonr.net",
                     "app_id":  "jp.co.sonymusic.communication.sakurazaka 2.4"},
    "asuka":        {"base_url": "https://api.asukasaito.glastonr.net",
                     "app_id":  "jp.co.sonymusic.communication.asukasaito 2.4"},
    "maishiraishi": {"base_url": "https://api.maishiraishi.glastonr.net",
                     "app_id":  "jp.co.sonymusicsolutions.maishiraishi 2.4"},
    "yodel":        {"base_url": "https://api.ydl.glastonr.net",
                     "app_id":  "jp.co.sonymusic.communication.yodel 2.4"},
}

# group_type → mobile_group key 映射
_GROUP_TYPE_TO_MOBILE: dict[str, str] = {
    "nogizaka46":   "nogizaka",
    "hinatazaka46": "hinatazaka",
    "sakurazaka46": "sakurazaka",
    "yodel":        "yodel",
}


# ──────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────
def _clean_cookie_string(raw: str) -> dict[str, str]:
    """将 Cookie / Set-Cookie 格式字符串解析为键值字典，忽略属性字段与标头前缀。"""
    if not raw or not isinstance(raw, str):
        return {}
    cleaned_raw = raw.replace("\r", "\n")
    ignore = {"path", "domain", "expires", "samesite", "secure", "httponly", "max-age", "priority"}
    result = {}

    for line in cleaned_raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("cookie:"):
            line = line[7:].strip()
        elif line.lower().startswith("set-cookie:"):
            line = line[11:].strip()

        for item in line.split(";"):
            item = item.strip()
            if "=" not in item:
                continue
            k, v = item.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k and k.lower() not in ignore:
                result[k] = v
    return result


# 记录「上次从 .env 消费过的凭证指纹」。
# 磁盘凭证优先于 .env，而 Token 正常续期后磁盘值必然与 .env 不同 ——
# 只有 .env 本身发生变化时才应该提醒用户「改了 .env 但不会生效」。
_ENV_SEEN_FILE = "_env_seen.json"


def _env_fingerprint(acc_cfg: dict) -> str:
    """对 .env 提供的初始凭证取指纹；未提供任何凭证时返回空字符串。"""
    raw = "|".join((
        acc_cfg.get("init_token", ""),
        acc_cfg.get("init_cookie", ""),
        acc_cfg.get("init_refresh_token", ""),
    ))
    if not raw.strip("|"):
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _load_env_seen() -> dict[str, str]:
    try:
        from src import auth
        return auth.load_env_seen()
    except (ImportError, OSError, sqlite3.Error) as exc:
        log_all(f"⚠️ 读取凭证环境指纹失败: {type(exc).__name__}: {exc}", is_error=True)
        return {}


def _save_env_seen(seen: dict[str, str]) -> None:
    try:
        from src import auth
        auth.save_env_seen(seen)
    except (ImportError, OSError, sqlite3.Error) as exc:
        log_all(f"⚠️ 保存凭证环境指纹失败: {type(exc).__name__}: {exc}", is_error=True)


def _save_cred(account_id: str, token: str, cookies: dict) -> bool:
    # 1. 持久化到 SQLite 数据库
    try:
        from src import auth
        auth.set_account_credential(account_id, "web", {"token": token, "cookies": cookies})
        return True
    except (ImportError, OSError, sqlite3.Error) as exc:
        log_all(f"🚨 账号 {account_id} Web 凭证持久化失败: {type(exc).__name__}: {exc}", is_error=True)
        return False


# ──────────────────────────────────────────────
# 移动端内部工具
# ──────────────────────────────────────────────
def _resolve_mobile_group(acc_cfg: dict) -> str:
    """解析账号的 mobile group key，优先使用显式配置，否则从 group_type 推导。"""
    explicit = acc_cfg.get("mobile_group")
    if explicit and explicit in _MOBILE_GROUP_CONFIG:
        return explicit
    derived = _GROUP_TYPE_TO_MOBILE.get(acc_cfg.get("group_type", ""))
    if derived:
        return derived
    raise ValueError(
        f"无法解析 mobile group（group_type={acc_cfg.get('group_type')}），"
        "请在 config.json 中为该账号设置 mobile_group 字段"
    )


def _save_mobile_cred(account_id: str, access_token: str, refresh_token: str) -> bool:
    """保存移动端凭证：{access_token, refresh_token} 到 SQLite 数据库。"""
    try:
        from src import auth
        auth.set_account_credential(account_id, "mobile", {"access_token": access_token, "refresh_token": refresh_token})
        return True
    except (ImportError, OSError, sqlite3.Error) as exc:
        log_all(f"🚨 账号 {account_id} 移动端凭证持久化失败: {type(exc).__name__}: {exc}", is_error=True)
        return False


# ──────────────────────────────────────────────
# 公开 API — 移动端
# ──────────────────────────────────────────────
def get_mobile_headers(account_id: str) -> dict[str, str]:
    """构建 iOS 端请求头（无 Cookie，仅 Bearer Token + X-Talk-App-ID）。"""
    acc_cfg = cfg.ACCOUNTS.get(account_id, {})
    mg_key = _resolve_mobile_group(acc_cfg)
    mg = _MOBILE_GROUP_CONFIG[mg_key]
    headers = {
        "Accept":          "application/json",
        "Content-Type":    "application/json",
        "X-Talk-App-ID":   mg["app_id"],
        "Accept-Language": "ja-JP;q=1, en;q=0.9",
        "User-Agent":      "nogizaka46/1.8.01.169 (iPhone; iOS 16.0; Scale/3.00)",
        "Accept-Encoding": "gzip, deflate, br",
    }
    cred = ACCOUNT_CREDS.get(account_id)
    if cred and cred.get("token"):
        headers["Authorization"] = f"Bearer {cred['token']}"
    return headers


def get_mobile_api_base(account_id: str) -> str:
    """获取移动端 API 基础 URL（优先使用显式 api_base 配置，否则用 glastonr.net）。"""
    acc_cfg = cfg.ACCOUNTS.get(account_id, {})
    if acc_cfg.get("api_base"):
        return acc_cfg["api_base"]
    mg_key = _resolve_mobile_group(acc_cfg)
    return _MOBILE_GROUP_CONFIG[mg_key]["base_url"]


async def refresh_mobile_token(account_id: str, target_group: int,
                               old_token: str | None = None) -> bool:
    """
    刷新移动端账号的 Token（使用 refresh_token 交换新 access_token）。
    与 web 端关键区别：
      - 请求体为 {"refresh_token": "<uuid>"}，非空
      - 请求头不含 Authorization（仅凭 refresh_token 本身认证）
      - 响应中同时返回新的 access_token 和 refresh_token
    """
    failure_kind = "credential_invalid"
    failure_detail = "refresh_token 不可用"
    lock = _get_refresh_lock(account_id)
    async with lock:
        cred = ACCOUNT_CREDS.get(account_id)
        if not cred:
            log_all(f"🚨 账号 {account_id} 无凭据", is_error=True)
            _record_refresh_failure(account_id, "credential_invalid", "凭据未加载")
            return False

        if old_token and cred.get("token") != old_token:
            log_all(f"✅ 账号 {account_id} token 已被其他协程刷新，跳过")
            return True

        available, reason = is_account_fetch_available(account_id)
        if not available:
            log_all(f"⏸️ 账号 {account_id} 移动端续期处于冷却状态，跳过重复请求：{reason}", is_debug=True)
            return False

        acc_cfg = cfg.ACCOUNTS.get(account_id)
        if not acc_cfg:
            log_all(f"🚨 账号 {account_id} 缺少配置，无法执行移动端续期", is_error=True)
            _record_refresh_failure(account_id, "credential_invalid", "账号配置缺失")
            return False
        rt = cred.get("refresh_token") or acc_cfg.get("init_refresh_token", "")
        if not rt:
            log_all(f"🚨 账号 {account_id} 无 refresh_token", is_error=True)
            _record_refresh_failure(account_id, "credential_invalid", "refresh_token 缺失")
            return False

        url = f"{get_mobile_api_base(account_id)}/v2/update_token"
        headers = get_mobile_headers(account_id)
        headers.pop("Authorization", None)  # 移动端刷新时不含旧 Auth

        try:
            async with _get_refresh_semaphore():
                r = await _post(url, headers=headers, json_body={"refresh_token": rt})
            log_response(r.text)

            if r.status_code == 200:
                try:
                    data = r.json()
                    new_access = data.get("access_token")
                    new_refresh = data.get("refresh_token") or rt
                except ValueError:
                    failure_kind = "response_invalid"
                    failure_detail = "续期响应不是合法 JSON"
                    new_access = None

                if new_access:
                    cred["token"] = new_access
                    cred["refresh_token"] = new_refresh
                    if not _save_mobile_cred(account_id, new_access, new_refresh):
                        failure_kind = "persistence_failure"
                        failure_detail = "新凭证无法持久化"
                        log_all(f"🚨 账号 {account_id} 移动端凭证未持久化，拒绝将续期视为成功", is_error=True)
                    else:
                        clear_refresh_state(account_id)
                        log_all(f"✅ 账号 {account_id} 移动端续期成功")
                        # 记录 Token 状态
                        remaining = get_token_remaining_seconds(account_id)
                        if remaining is not None:
                            _health_tracker().record_token(account_id, max(0, remaining))
                        return True
                else:
                    failure_kind = "response_invalid"
                    failure_detail = "续期响应无 access_token"
                    log_all(f"🚨 账号 {account_id} 移动端续期响应无 access_token", is_error=True)
            else:
                body_snippet = r.text[:300] if r.text else "(空响应)"
                failure_kind = _classify_refresh_status(r.status_code, body_snippet)
                failure_detail = f"HTTP {r.status_code}"
                log_all(
                    f"🚨 账号 {account_id} 移动端续期被拒: HTTP {r.status_code} | {body_snippet}",
                    is_error=True,
                )

        except httpx.TimeoutException as e:
            failure_kind = "transient_network"
            failure_detail = f"{type(e).__name__}: {format_httpx_error(e)}"
            log_all(
                f"🔥 账号 {account_id} 移动端续期超时: {format_httpx_error(e)}",
                is_error=True,
            )
        except httpx.RequestError as e:
            failure_kind = "transient_network"
            failure_detail = f"{type(e).__name__}: {format_httpx_error(e)}"
            log_all(
                f"🔥 账号 {account_id} 移动端续期网络异常: {format_httpx_error(e)}",
                is_error=True,
            )
        except (OSError, ValueError) as e:
            failure_kind = "response_invalid"
            failure_detail = f"{type(e).__name__}: {e}"
            log_all(f"🔥 账号 {account_id} 移动端续期处理失败: {type(e).__name__}: {e}", is_error=True)
        except Exception as e:  # 最终边界：刷新失败必须记录，不能让主巡查崩溃。
            failure_kind = "response_invalid"
            failure_detail = f"{type(e).__name__}: {e}"
            log_all(f"🔥 账号 {account_id} 移动端续期未处理异常: {type(e).__name__}: {e}", is_error=True)

    await _report_refresh_failure(
        account_id, target_group, kind=failure_kind, detail=failure_detail,
        platform="移动端",
    )
    return False


def get_web_headers(
    group_type: str,
    token: str | None = None,
    *,
    app_tag: str | None = None,
    api_base: str | None = None,
    web_origin: str | None = None,
) -> dict[str, str]:
    if app_tag is None:
        if group_type == "nogizaka46":
            app_tag = "nogizaka"
        elif group_type.lower() == "yodel":
            app_tag = "yodel"
        else:
            app_tag = "keyakizaka"
    if web_origin:
        origin  = web_origin
        referer = web_origin.rstrip("/") + "/"
    elif group_type.lower() == "yodel":
        origin  = "https://service.yodel-app.com"
        referer = "https://service.yodel-app.com/"
    else:
        origin  = f"https://message.{group_type}.com"
        referer = f"https://message.{group_type}.com/"

    headers = {
        "accept":               "application/json",
        "accept-encoding":      "gzip, deflate, br",
        "accept-language":      "ja-JP;q=1,en;q=0.9",
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


def get_source_headers_for_account(account_id: str, group_type: str) -> dict[str, str]:
    """构造访问 message 私有资源的请求头，按账号认证方式自动选择 Web / mobile。"""
    cred = ACCOUNT_CREDS.get(account_id)
    if not cred:
        return {}

    acc_cfg = cfg.ACCOUNTS.get(account_id, {})
    if acc_cfg.get("auth_method") == "mobile":
        return get_mobile_headers(account_id)

    token = cred.get("token", "")
    cookies = cred.get("cookies") or {}
    headers = get_web_headers(
        group_type,
        token,
        app_tag=acc_cfg.get("app_tag"),
        api_base=acc_cfg.get("api_base"),
        web_origin=acc_cfg.get("web_origin"),
    )
    if cookies:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return headers


async def write_time_record(time_file: str, file_lock: asyncio.Lock | None, updated: str) -> None:
    # 状态与水位线已统一由 archive.db 持久化；仅当外部目录存在时同步写文件（保持测试与向后兼容）
    if not time_file or _last_time_written.get(time_file) == updated:
        return
    parent = os.path.dirname(time_file)
    if not parent or not os.path.isdir(parent):
        _last_time_written[time_file] = updated
        return
    tmp = time_file + ".tmp"
    if file_lock is not None:
        async with file_lock:
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(updated)
                os.replace(tmp, time_file)
                _last_time_written[time_file] = updated
            except Exception as e:
                log_all(f"🚨 时间戳写入失败 ({time_file}): {e}", is_error=True)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    else:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(updated)
            os.replace(tmp, time_file)
            _last_time_written[time_file] = updated
        except Exception as e:
            log_all(f"🚨 时间戳写入失败 ({time_file}): {e}", is_error=True)
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_all_accounts() -> None:
    """从数据库加载所有账号凭证，不存在时用初始凭证创建并持久化至数据库。
    自动识别凭证格式：包含 refresh_token → 移动端，否则 → Web 端。

    幂等：已加载的账号会跳过，因此热重载后可安全重复调用以加载新增账号。
    优先级：数据库持久化凭证 > `.env`。若 `.env` 相比上次启动发生变化，会打告警提示
    正确的轮换方式（在管理端重置凭证），而不会自动采用 `.env` 的值。"""
    try:
        from src import auth
    except ImportError:
        auth = None

    env_seen = _load_env_seen()
    seen_dirty = False

    for acc_id, acc_cfg in cfg.ACCOUNTS.items():
        is_mobile = acc_cfg.get("auth_method") == "mobile"
        fingerprint = _env_fingerprint(acc_cfg)
        data = None

        # 1. 优先从 SQLite 数据库获取
        if auth is not None:
            try:
                data = auth.get_account_credential(acc_id)
            except Exception:
                data = None

        # 2. 兼容检查旧磁盘 json 文件
        path = os.path.join(cfg.CRED_DIR, f"{acc_id}.json") if getattr(cfg, "CRED_DIR", None) else ""
        if data is None and path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data and auth is not None:
                    c_type = "mobile" if "refresh_token" in data else "web"
                    auth.set_account_credential(acc_id, c_type, data)
                try:
                    os.remove(path)
                except OSError:
                    pass
            except Exception:
                data = None

        # ── 认证方式切换检测 ──────────────────────────
        if acc_id in ACCOUNT_CREDS:
            cred_is_mobile = "refresh_token" in ACCOUNT_CREDS[acc_id]
            if cred_is_mobile == is_mobile:
                continue
            log_all(
                f"🔁 {acc_id} 认证方式已切换为 {'mobile' if is_mobile else 'web'}，"
                f"丢弃旧凭证并用 .env 重建",
            )
            del ACCOUNT_CREDS[acc_id]
            if auth is not None:
                auth.delete_account_credential(acc_id)

        if data is not None:
            disk_is_mobile = "refresh_token" in data
            if disk_is_mobile != is_mobile:
                log_all(
                    f"🔁 {acc_id} 凭证为旧认证方式（{'mobile' if disk_is_mobile else 'web'}）"
                    f"，作废并用 .env 重建",
                )
                if auth is not None:
                    auth.delete_account_credential(acc_id)
                data = None

        if data is not None:
            if "refresh_token" in data:
                ACCOUNT_CREDS[acc_id] = {
                    "token": data.get("access_token", ""),
                    "refresh_token": data["refresh_token"],
                }
                log_all(f"📂 读取移动端凭证: {acc_id}")
            else:
                ACCOUNT_CREDS[acc_id] = data
                log_all(f"📂 读取账号凭证: {acc_id}")

            if fingerprint and acc_id not in env_seen:
                env_seen[acc_id] = fingerprint
                seen_dirty = True
            elif fingerprint and env_seen[acc_id] != fingerprint:
                log_all(
                    f"⚠️ {acc_id} 的 .env 凭证已修改，但数据库凭证优先，本次修改不会生效；"
                    f"如需强制轮换请在管理端重置该账号凭证后重启",
                    is_error=True,
                )
        elif is_mobile:
            rt = acc_cfg.get("init_refresh_token", "")
            init_token = acc_cfg.get("init_token", "")
            ACCOUNT_CREDS[acc_id] = {"token": init_token, "refresh_token": rt}
            if rt or init_token:
                _save_mobile_cred(acc_id, init_token, rt)
            log_all(f"📝 初始化移动端凭证: {acc_id}")
            if fingerprint:
                env_seen[acc_id] = fingerprint
                seen_dirty = True
        else:
            # 缺失时留空而非 KeyError —— 由 validate_account_cred 在健康检查里统一报告
            init_token = acc_cfg.get("init_token", "")
            cookies = _clean_cookie_string(acc_cfg.get("init_cookie", ""))
            ACCOUNT_CREDS[acc_id] = {"token": init_token, "cookies": cookies}
            if init_token or cookies:
                _save_cred(acc_id, init_token, cookies)
            log_all(f"📝 初始化账号凭证: {acc_id}")
            if fingerprint:
                env_seen[acc_id] = fingerprint
                seen_dirty = True

    if seen_dirty:
        _save_env_seen(env_seen)


def validate_account_cred(account_id: str) -> tuple[bool, str]:
    """校验账号凭证内容是否满足当前 auth_method 的最低要求。"""
    acc_cfg = cfg.ACCOUNTS.get(account_id)
    if not acc_cfg:
        return False, "账号未在 ACCOUNTS 中定义"

    cred = ACCOUNT_CREDS.get(account_id)
    if not cred:
        return False, "凭证未加载"

    if acc_cfg.get("auth_method") == "mobile":
        if not cred.get("refresh_token"):
            return False, "mobile 账号缺少 refresh_token"
        return True, "mobile 凭证完整"

    if not cred.get("token"):
        return False, "web 账号缺少 token"
    if not cred.get("cookies"):
        return False, "web 账号缺少 cookie"
    return True, "web 凭证完整"


async def refresh_token(account_id: str, target_group: int, old_token: str | None = None) -> bool:
    """
    刷新指定账号的 Token。
    - 若其他协程已抢先刷新（token 已变），直接返回 True。
    - 失败时触发 QQ 报警（带冷却）。
    """
    failure_kind = "credential_invalid"
    failure_detail = "Cookie/Token 不可用"
    lock = _get_refresh_lock(account_id)
    async with lock:
        cred = ACCOUNT_CREDS.get(account_id)
        if not cred:
            log_all(f"🚨 账号 {account_id} 无凭据", is_error=True)
            _record_refresh_failure(account_id, "credential_invalid", "凭据未加载")
            return False

        if old_token and cred.get("token") != old_token:
            log_all(f"✅ 账号 {account_id} token 已被其他协程刷新，跳过")
            return True

        available, reason = is_account_fetch_available(account_id)
        if not available:
            log_all(f"⏸️ 账号 {account_id} Web 续期处于冷却状态，跳过重复请求：{reason}", is_debug=True)
            return False

        acc_cfg = cfg.ACCOUNTS.get(account_id)
        if not acc_cfg:
            log_all(f"🚨 账号 {account_id} 缺少配置，无法执行 Web 续期", is_error=True)
            _record_refresh_failure(account_id, "credential_invalid", "账号配置缺失")
            return False
        if not cred.get("token") or not isinstance(cred.get("cookies"), dict):
            log_all(f"🚨 账号 {account_id} Web 凭证结构不完整，无法执行续期", is_error=True)
            _record_refresh_failure(account_id, "credential_invalid", "Web 凭证结构不完整")
            return False
        group_type = acc_cfg["group_type"]
        api_base   = acc_cfg.get("api_base")
        if api_base:
            url = f"{api_base.rstrip('/')}/v2/update_token"
        elif group_type.lower() == "yodel":
            url = "https://api.service.yodel-app.com/v2/update_token"
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
            async with _get_refresh_semaphore():
                r = await _post(url, headers=headers, content=b'{"refresh_token":null}')
            log_response(r.text)

            if r.status_code == 200:
                try:
                    new_token = r.json().get("access_token")
                except ValueError:
                    new_token = None
                    failure_kind = "response_invalid"
                    failure_detail = "续期响应不是合法 JSON"
                    log_all(f"🚨 账号 {account_id} 续期响应不是合法 JSON", is_error=True)

                if new_token:
                    cred["token"] = new_token
                    for sc in r.headers.get_list("set-cookie"):
                        sc = sc.split(";")[0].strip()
                        if "=" in sc:
                            k, v = sc.split("=", 1)
                            cred["cookies"][k] = v
                    if not _save_cred(account_id, cred["token"], cred["cookies"]):
                        failure_kind = "persistence_failure"
                        failure_detail = "新凭证无法持久化"
                        log_all(f"🚨 账号 {account_id} Web 凭证未持久化，拒绝将续期视为成功", is_error=True)
                    else:
                        clear_refresh_state(account_id)
                        log_all(f"✅ 账号 {account_id} 续期成功")
                        # 记录 Token 状态
                        remaining = get_token_remaining_seconds(account_id)
                        if remaining is not None:
                            _health_tracker().record_token(account_id, max(0, remaining))
                        return True
                else:
                    failure_kind = "response_invalid"
                    failure_detail = "续期响应无 access_token"
                    log_all(f"🚨 账号 {account_id} 续期响应无 access_token", is_error=True)
            else:
                body_snippet = r.text[:120].strip() if r.text else ""
                failure_kind = _classify_refresh_status(r.status_code, body_snippet)
                failure_detail = f"HTTP {r.status_code}"
                log_all(f"🚨 账号 {account_id} 续期被拒: HTTP {r.status_code} | {body_snippet}", is_error=True)

        except httpx.TimeoutException as e:
            failure_kind = "transient_network"
            failure_detail = f"{type(e).__name__}: {format_httpx_error(e)}"
            log_all(
                f"🔥 账号 {account_id} Web 续期超时: {format_httpx_error(e)}",
                is_error=True,
            )
        except httpx.RequestError as e:
            failure_kind = "transient_network"
            failure_detail = f"{type(e).__name__}: {format_httpx_error(e)}"
            log_all(
                f"🔥 账号 {account_id} 续期网络异常: {format_httpx_error(e)}",
                is_error=True,
            )
        except (OSError, ValueError) as e:
            failure_kind = "response_invalid"
            failure_detail = f"{type(e).__name__}: {e}"
            log_all(f"🔥 账号 {account_id} Web 续期处理失败: {type(e).__name__}: {e}", is_error=True)
        except Exception as e:  # 最终边界：刷新失败必须记录，不能让主巡查崩溃。
            failure_kind = "response_invalid"
            failure_detail = f"{type(e).__name__}: {e}"
            log_all(f"🔥 账号 {account_id} Web 续期未处理异常: {type(e).__name__}: {e}", is_error=True)

    await _report_refresh_failure(
        account_id, target_group, kind=failure_kind, detail=failure_detail,
        platform="Web",
    )
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
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
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


async def proactive_refresh_if_expiring(account_id: str, target_group: int) -> bool:
    """
    每轮巡查前调用。
    若 Token 剩余时间 <= cfg.TOKEN_REFRESH_BEFORE_SECONDS，主动刷新，
    避免在实际 API 请求时才触发 401 浪费一轮。
    按 auth_method 自动派发到 Web 或移动端刷新。
    """
    available, reason = is_account_fetch_available(account_id)
    if not available:
        log_all(f"⏸️ {account_id} 跳过主动续期：{reason}", is_debug=True)
        return False

    remaining = get_token_remaining_seconds(account_id)
    if remaining is None:
        log_all(f"⚠️ 无法解析 {account_id} 的 Token 过期时间，跳过主动刷新", is_debug=True)
        return True
    if remaining <= cfg.TOKEN_REFRESH_BEFORE_SECONDS:
        acc_cfg = cfg.ACCOUNTS.get(account_id, {})
        log_all(
            f"🔄 {account_id} Token 剩余 {int(remaining)}s"
            f"（阈值 {cfg.TOKEN_REFRESH_BEFORE_SECONDS}s），主动刷新...",
        )
        if acc_cfg.get("auth_method") == "mobile":
            return await refresh_mobile_token(account_id, target_group)
        else:
            return await refresh_token(account_id, target_group)
    else:
        log_all(
            f"✅ {account_id} Token 剩余 {int(remaining // 60)}min，无需刷新",
            is_debug=True,
        )
        return True


async def verify_and_handshake_account(account_id: str, custom_client: httpx.AsyncClient | None = None) -> tuple[bool, str, dict]:
    """对账号执行在线握手测试与凭证长期化。
    - Web 账号：立即尝试 update_token 并自动捕获 Set-Cookie 长期化；若暂不可用则回退测试 timeline/groups API。
    - Mobile 账号：尝试 refresh_mobile_token 刷新 access_token。

    返回 (ok: bool, message: str, details: dict)。
    """
    acc_cfg = cfg.ACCOUNTS.get(account_id)
    if not acc_cfg:
        return False, f"未找到账号配置: {account_id}", {}

    cred = ACCOUNT_CREDS.get(account_id)
    if not cred:
        return False, f"未找到账号凭证数据: {account_id}", {}

    is_mobile = acc_cfg.get("auth_method") == "mobile"

    if is_mobile:
        rt = cred.get("refresh_token")
        if not rt:
            return False, "缺少 refresh_token，无法执行移动端刷新", {}
        ok = await refresh_mobile_token(account_id, 0)
        rem = get_token_remaining_seconds(account_id)
        if ok:
            return True, f"✅ 移动端 Token 刷新成功！有效期约 {int(rem or 3600)//60} 分钟", {
                "auth_method": "mobile",
                "remaining_seconds": rem,
            }
        return False, "❌ 移动端 Token 刷新失败，请检查 refresh_token 是否正确", {"auth_method": "mobile"}

    # Web 账号处理
    token = cred.get("token", "")
    cookies = cred.get("cookies", {})

    if not token and not cookies:
        return False, "缺少 Token 和 Cookie", {}

    # 1. 优先尝试 update_token（全自动换取长期 Set-Cookie 与最新 Token）
    if cookies:
        ok = await refresh_token(account_id, 0)
        if ok:
            rem = get_token_remaining_seconds(account_id)
            updated_cookies = ACCOUNT_CREDS.get(account_id, {}).get("cookies", {})
            return True, f"✅ 凭证自动握手成功！已成功从 update_token 提取最新长期 Cookie 与 Token（有效时长约 {int(rem or 3600)//60} 分钟，后台将自动持久续期）", {
                "auth_method": "web",
                "handshake_type": "update_token",
                "remaining_seconds": rem,
                "cookie_keys": list(updated_cookies.keys()),
            }

    # 2. 如果 update_token 失败，测试当前 timeline 接口 (GET /v2/groups) 是否能用
    if token:
        group_type = acc_cfg.get("group_type", "nogizaka46")
        if acc_cfg.get("api_base"):
            api_base = acc_cfg["api_base"]
        elif group_type.lower() == "yodel":
            api_base = "https://api.service.yodel-app.com"
        else:
            api_base = f"https://api.message.{group_type}.com"
        url = f"{api_base.rstrip('/')}/v2/groups"
        headers = get_web_headers(
            group_type,
            token=token,
            app_tag=acc_cfg.get("app_tag"),
            api_base=api_base,
            web_origin=acc_cfg.get("web_origin"),
        )
        if cookies:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        try:
            client_to_use = custom_client
            if client_to_use is None and _http_client is not None and not _http_client.is_closed:
                try:
                    curr_loop = asyncio.get_running_loop()
                    transport = getattr(_http_client, "_transport", None)
                    client_loop = getattr(transport, "_loop", None)
                    if client_loop is None or client_loop is curr_loop:
                        client_to_use = _http_client
                except RuntimeError:
                    client_to_use = None

            if client_to_use is not None:
                r = await client_to_use.get(url, headers=headers, timeout=10)
            else:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(url, headers=headers)

            if r.status_code == 200:
                # update_token 可能因 Cookie 缺少 session 而失败，但当前 Token
                # 仍然可用；握手成功时解除之前的网络/续期熔断状态。
                clear_refresh_state(account_id)
                rem = get_token_remaining_seconds(account_id)
                rem_min = max(0, int(rem or 0) // 60)
                return True, (
                    f"⚠️ 当前 Token 可用（剩余有效约 {rem_min} 分钟），但【自动续期未生效】（Cookie 中缺少 session 鉴权项）！\n"
                    f"注意：约 {rem_min} 分钟后 Token 到期将无法自动续期并报错 400/401。\n"
                    f"建议：在 Cookie 中补填 session=xxx，或在 F12 -> Application -> Cookies 中复制 session 的值。"
                ), {
                    "auth_method": "web",
                    "handshake_type": "api_valid_no_renewal",
                    "remaining_seconds": rem,
                    "warning": True,
                }
            else:
                return False, f"❌ 凭证验证失败：API 返回 HTTP {r.status_code}，Token 或 Cookie 可能已失效", {"auth_method": "web"}
        except httpx.TimeoutException:
            return False, "❌ 凭证验证超时：请检查代理或稍后重试", {"auth_method": "web", "error_code": "timeout"}
        except httpx.RequestError as exc:
            return False, f"❌ 凭证验证网络异常: {format_httpx_error(exc)}", {"auth_method": "web", "error_code": "network_error"}
        except (OSError, ValueError) as exc:
            log_all(f"🚨 账号 {account_id} 凭证验证处理失败: {type(exc).__name__}: {exc}", is_error=True)
            return False, f"❌ 凭证验证处理失败: {type(exc).__name__}", {"auth_method": "web", "error_code": "processing_error"}
        except Exception as exc:  # 最终边界：网页端必须收到可处理的错误，而不是请求中断。
            log_all(f"🚨 账号 {account_id} 凭证验证未处理异常: {type(exc).__name__}: {exc}", is_error=True)
            return False, "❌ 凭证验证发生内部错误，请查看系统日志", {"auth_method": "web", "error_code": "internal_error"}

    return False, "❌ 凭证不完整，无法完成验证", {"auth_method": "web"}


def rename_account(old_id: str, new_id: str) -> None:
    """内存与数据库中同步重命名账号凭证。"""
    if old_id == new_id:
        return
    if old_id in ACCOUNT_CREDS:
        ACCOUNT_CREDS[new_id] = ACCOUNT_CREDS.pop(old_id)
    if old_id in _refresh_state:
        _refresh_state[new_id] = _refresh_state.pop(old_id)
    try:
        from src import auth
        auth.rename_account_credential(old_id, new_id)
    except (ImportError, OSError, sqlite3.Error) as exc:
        log_all(f"⚠️ 重命名账号 {old_id} 的持久化凭证失败: {type(exc).__name__}: {exc}", is_error=True)
