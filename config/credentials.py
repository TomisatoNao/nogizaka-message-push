# ============================================================
# credentials.py — 账号凭证管理、Token 刷新、文件锁工具
# ============================================================
import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timezone

import httpx

# 统一通过 cfg.X 访问，热重载后标量值（告警冷却、刷新阈值等）才能生效
import config.config as cfg
from src.health import get_tracker as _health_tracker
from src.logger import format_httpx_error, log_all, log_response

# ---- 运行时状态 ----
ACCOUNT_CREDS:        dict[str, dict]          = {}
_file_locks:          dict[str, asyncio.Lock]  = {}
_token_refresh_locks: dict[str, asyncio.Lock]  = {}
_alert_last_sent:     dict[str, float]         = {}
_http_client:         httpx.AsyncClient | None = None
_last_time_written:   dict[str, str]           = {}   # write_time_record 的值缓存


def initialize(client: httpx.AsyncClient) -> None:
    """注入共享 HTTP 客户端（Token 刷新复用连接池）。
    未注入时（如 tools/list_members.py 单独运行）按需临时创建。"""
    global _http_client
    _http_client = client


async def _post(url: str, *, headers: dict,
                json_body: dict | None = None,
                content: bytes | None = None) -> httpx.Response:
    if _http_client is not None:
        return await _http_client.post(
            url, headers=headers, json=json_body, content=content, timeout=15,
        )
    async with httpx.AsyncClient(timeout=15) as client:
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
    path = os.path.join(cfg.CRED_DIR, _ENV_SEEN_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_env_seen(seen: dict[str, str]) -> None:
    path = os.path.join(cfg.CRED_DIR, _ENV_SEEN_FILE)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(seen, f)
        os.replace(tmp, path)
    except Exception as e:
        log_all(f"⚠️ 写入 {_ENV_SEEN_FILE} 失败: {e}", is_debug=True)


def _save_cred(account_id: str, token: str, cookies: dict) -> None:
    os.makedirs(cfg.CRED_DIR, exist_ok=True)
    path = os.path.join(cfg.CRED_DIR, f"{account_id}.json")
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"token": token, "cookies": cookies}, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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


def _save_mobile_cred(account_id: str, access_token: str, refresh_token: str) -> None:
    """保存移动端凭证：{access_token, refresh_token}。"""
    os.makedirs(cfg.CRED_DIR, exist_ok=True)
    path = os.path.join(cfg.CRED_DIR, f"{account_id}.json")
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token}, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


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
    from src.notifier import send_alert_message

    lock = _token_refresh_locks.setdefault(account_id, asyncio.Lock())
    async with lock:
        cred = ACCOUNT_CREDS.get(account_id)
        if not cred:
            log_all(f"🚨 账号 {account_id} 无凭据", is_error=True)
            return False

        if old_token and cred.get("token") != old_token:
            log_all(f"✅ 账号 {account_id} token 已被其他协程刷新，跳过")
            return True

        acc_cfg = cfg.ACCOUNTS[account_id]
        rt = cred.get("refresh_token") or acc_cfg.get("init_refresh_token", "")
        if not rt:
            log_all(f"🚨 账号 {account_id} 无 refresh_token", is_error=True)
            return False

        url = f"{get_mobile_api_base(account_id)}/v2/update_token"
        headers = get_mobile_headers(account_id)
        headers.pop("Authorization", None)  # 移动端刷新时不含旧 Auth

        try:
            r = await _post(url, headers=headers, json_body={"refresh_token": rt})
            log_response(r.text)

            if r.status_code == 200:
                try:
                    data = r.json()
                    new_access = data.get("access_token")
                    new_refresh = data.get("refresh_token") or rt
                except ValueError:
                    log_all(f"🚨 账号 {account_id} 移动端续期响应不是合法 JSON", is_error=True)
                    new_access = None

                if new_access:
                    cred["token"] = new_access
                    cred["refresh_token"] = new_refresh
                    _save_mobile_cred(account_id, new_access, new_refresh)
                    log_all(f"✅ 账号 {account_id} 移动端续期成功")
                    # 记录 Token 状态
                    remaining = get_token_remaining_seconds(account_id)
                    if remaining is not None:
                        _health_tracker().record_token(account_id, max(0, remaining))
                    return True
                else:
                    log_all(f"🚨 账号 {account_id} 移动端续期响应无 access_token", is_error=True)
            else:
                body_snippet = r.text[:300] if r.text else "(空响应)"
                log_all(
                    f"🚨 账号 {account_id} 移动端续期被拒: HTTP {r.status_code} | {body_snippet}",
                    is_error=True,
                )

        except Exception as e:
            log_all(
                f"🔥 账号 {account_id} 移动端续期网络异常: {format_httpx_error(e)}",
                is_error=True,
            )

    # ---- 续期失败，触发报警 ----
    log_all(f"🚨 致命错误：账号 {account_id} 移动端续期失败，refresh_token 可能已失效", is_error=True)
    now  = datetime.now().timestamp()
    last = _alert_last_sent.get(account_id, 0)
    if now - last > cfg.ALERT_COOLDOWN_SECONDS:
        _alert_last_sent[account_id] = now
        try:
            await send_alert_message(
                target_group,
                f"📢 警报：账号 {account_id} 移动端续期失败！请更新 refresh_token！",
            )
        except Exception:
            pass
    else:
        remaining = int(cfg.ALERT_COOLDOWN_SECONDS - (now - last))
        _health_tracker().record_alert_cooldown(account_id, float(remaining))
        log_all(f"⏳ 账号 {account_id} 报警冷却中，{remaining}s 后可再次通知")

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
        app_tag = "nogizaka" if group_type == "nogizaka46" else "keyakizaka"
    if web_origin:
        origin  = web_origin
        referer = web_origin.rstrip("/") + "/"
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


async def write_time_record(time_file: str, file_lock: asyncio.Lock, updated: str) -> None:
    # 值没变就不落盘：无新消息的轮次会以完全相同的内容反复重写同一文件
    if _last_time_written.get(time_file) == updated:
        return
    tmp = time_file + ".tmp"
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


def load_all_accounts() -> None:
    """从磁盘加载所有账号凭证，不存在时用初始凭证创建。
    自动识别凭证格式：磁盘文件含 refresh_token → 移动端，否则 → Web 端。

    幂等：已加载的账号会跳过，因此热重载后可安全重复调用以加载新增账号。
    优先级：磁盘凭证 > `.env`。若 `.env` 相比上次启动发生变化，会打告警提示
    正确的轮换方式（删除磁盘凭证文件），而不会自动采用 `.env` 的值。"""
    os.makedirs(cfg.CRED_DIR, exist_ok=True)
    env_seen = _load_env_seen()
    seen_dirty = False

    for acc_id, acc_cfg in cfg.ACCOUNTS.items():
        is_mobile = acc_cfg.get("auth_method") == "mobile"
        fingerprint = _env_fingerprint(acc_cfg)
        path = os.path.join(cfg.CRED_DIR, f"{acc_id}.json")

        # ── 认证方式切换检测 ──────────────────────────
        # 账号在 config.json 里从 mobile 改成 web（或反之）后，内存/磁盘里
        # 旧格式的凭证结构不再匹配（web 需要 cookies，mobile 需要 refresh_token），
        # 继续沿用会在抓取时 KeyError。这里检测到不匹配就丢弃旧凭证，
        # 走下方初始化分支用 .env 的新凭证重建。
        if acc_id in ACCOUNT_CREDS:
            cred_is_mobile = "refresh_token" in ACCOUNT_CREDS[acc_id]
            if cred_is_mobile == is_mobile:
                continue
            log_all(
                f"🔁 {acc_id} 认证方式已切换为 {'mobile' if is_mobile else 'web'}，"
                f"丢弃旧凭证并用 .env 重建",
            )
            del ACCOUNT_CREDS[acc_id]
            try:
                os.remove(path)
            except OSError:
                pass

        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            disk_is_mobile = "refresh_token" in data
            if disk_is_mobile != is_mobile:
                # 磁盘凭证也是切换前的旧格式（如切换后首次重启）：作废重建
                log_all(
                    f"🔁 {acc_id} 磁盘凭证为旧认证方式（{'mobile' if disk_is_mobile else 'web'}）"
                    f"，作废并用 .env 重建",
                )
                try:
                    os.remove(path)
                except OSError:
                    pass
                data = None
        else:
            data = None

        if data is not None:
            # 自动识别格式：含 refresh_token → 移动端
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
                # 首次记录（老用户升级）：无从判断 .env 是否变过，静默记下即可
                env_seen[acc_id] = fingerprint
                seen_dirty = True
            elif fingerprint and env_seen[acc_id] != fingerprint:
                log_all(
                    f"⚠️ {acc_id} 的 .env 凭证已修改，但磁盘凭证优先，本次修改不会生效；"
                    f"如需强制轮换请删除 {path} 后重启",
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

        acc_cfg    = cfg.ACCOUNTS[account_id]
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
            r = await _post(url, headers=headers, content=b'{"refresh_token":null}')
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
                    # 记录 Token 状态
                    remaining = get_token_remaining_seconds(account_id)
                    if remaining is not None:
                        _health_tracker().record_token(account_id, max(0, remaining))
                    return True
                else:
                    log_all(f"🚨 账号 {account_id} 续期响应无 access_token", is_error=True)
            else:
                log_all(f"🚨 账号 {account_id} 续期被拒: HTTP {r.status_code}", is_error=True)

        except Exception as e:
            log_all(
                f"🔥 账号 {account_id} 续期网络异常: {format_httpx_error(e)}",
                is_error=True,
            )

    # ---- 续期失败，触发报警 ----
    log_all(f"🚨 致命错误：账号 {account_id} 续期失败，Cookie 可能已死亡", is_error=True)
    now  = datetime.now().timestamp()
    last = _alert_last_sent.get(account_id, 0)
    if now - last > cfg.ALERT_COOLDOWN_SECONDS:
        _alert_last_sent[account_id] = now
        try:
            await send_alert_message(
                target_group,
                f"📢 警报：账号 {account_id} 续期失败，Cookie 已死亡！请重新抓包！",
            )
        except Exception:
            pass
    else:
        remaining = int(cfg.ALERT_COOLDOWN_SECONDS - (now - last))
        _health_tracker().record_alert_cooldown(account_id, float(remaining))
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
    若 Token 剩余时间 <= cfg.TOKEN_REFRESH_BEFORE_SECONDS，主动刷新，
    避免在实际 API 请求时才触发 401 浪费一轮。
    按 auth_method 自动派发到 Web 或移动端刷新。
    """
    remaining = get_token_remaining_seconds(account_id)
    if remaining is None:
        log_all(f"⚠️ 无法解析 {account_id} 的 Token 过期时间，跳过主动刷新", is_debug=True)
        return
    if remaining <= cfg.TOKEN_REFRESH_BEFORE_SECONDS:
        acc_cfg = cfg.ACCOUNTS.get(account_id, {})
        log_all(
            f"🔄 {account_id} Token 剩余 {int(remaining)}s"
            f"（阈值 {cfg.TOKEN_REFRESH_BEFORE_SECONDS}s），主动刷新...",
        )
        if acc_cfg.get("auth_method") == "mobile":
            await refresh_mobile_token(account_id, target_group)
        else:
            await refresh_token(account_id, target_group)
    else:
        log_all(
            f"✅ {account_id} Token 剩余 {int(remaining // 60)}min，无需刷新",
            is_debug=True,
        )
