"""
src/webui_modules/system_handlers.py — WebUI 系统监控、日志流、运维工具与凭证智能导入服务

提供：
  1. 系统运行状态与实时 Token 健康度快照 (/api/status, /api/health)
  2. 实时日志流与滚动文件日志查询 (/api/logs)
  3. 磁盘存储分项统计与清理 (/api/storage, /api/storage/clean)
  4. 成员花名册同步与代理探测 (/api/members, /api/proxy/test)
  5. 测试消息跨渠道推送 (/api/test_push)
  6. QQ OpenID 监听器控制与状态快照 (/api/qq_openid/*)
  7. 浏览器 cURL / HAR / Headers 凭证智能解析导入 (/api/credentials/smart_parse, /api/accounts/smart_parse)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs

import httpx

from src.logger import get_recent
from src.utils import format_bytes, get_storage_breakdown, clean_storage_category
from src.webui_modules.static_handler import send_json

_TAIL_READ_BYTES = 262144  # 256KB


def tail_file(path: Path, max_lines: int) -> list[str]:
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
        lines = lines[1:]  # 首行可能被截断
    return [ln[:2000] for ln in lines[-max_lines:]]


def env_status() -> dict:
    """返回常用环境变量的配置状态（只报有无，不报值）。"""
    return {
        "GEMINI_API_KEY": bool(os.getenv("GEMINI_API_KEY")),
        "ZHIPU_API_KEY": bool(os.getenv("ZHIPU_API_KEY")),
        "TG_BOT_TOKEN": bool(os.getenv("TG_BOT_TOKEN")),
        "INSTAGRAM_SESSIONID": bool(os.getenv("INSTAGRAM_SESSIONID")),
        "X_AUTH_TOKEN": bool(os.getenv("X_AUTH_TOKEN")),
        "TIKTOK_SESSIONID": bool(os.getenv("TIKTOK_SESSIONID")),
    }


def handle_status(handler, on_poll_cb=None) -> None:
    """GET /api/status 运行状态快照。"""
    from src.health import get_tracker
    snap = get_tracker().snapshot()

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
    snap["now_epoch"] = time.time()
    snap["embedded"] = on_poll_cb is not None
    send_json(handler, snap)


def handle_logs(handler) -> None:
    """GET /api/logs 查看日志（内存环增量或文件尾部）。"""
    qs = parse_qs(handler.path.partition("?")[2])

    def qs_int(key: str, default: int) -> int:
        try:
            return int((qs.get(key) or [str(default)])[0])
        except ValueError:
            return default

    source = (qs.get("source") or ["live"])[0]
    if source == "live":
        entries, seq = get_recent(qs_int("after", 0))
        send_json(handler, {"ok": True, "source": "live", "entries": entries, "seq": seq})
        return

    if source in ("error", "response", "system"):
        import config.config as cfg
        if source == "error":
            fp = Path(cfg.ERROR_LOG_FILE)
        elif source == "system":
            fp = Path(getattr(cfg, "SYSTEM_LOG_FILE", "logs/system_info.log"))
        else:
            fp = Path(cfg.RESPONSE_LOG_FILE)

        tail = max(1, min(qs_int("tail", 200), 1000))
        try:
            lines = tail_file(fp, tail)
        except OSError as e:
            send_json(handler, {"ok": False, "errors": [f"读取日志文件失败: {e}"]}, 500)
            return
        send_json(handler, {"ok": True, "source": source, "lines": lines, "file": str(fp)})
        return

    send_json(handler, {"ok": False, "errors": [f"未知日志源: {source!r}"]}, 400)


def handle_storage(handler) -> None:
    """GET /api/storage 返回磁盘空间与分项统计。"""
    qs = parse_qs(handler.path.partition("?")[2])
    force = bool((qs.get("refresh") or [""])[0])
    data = get_storage_breakdown(force_refresh=force)
    send_json(handler, {"ok": True, "storage": data, **data})


def handle_storage_clean(handler, body: dict) -> None:
    """POST /api/storage/clean 触发指定分类的磁盘清理。"""
    category = str(body.get("category", "")).strip()
    if not category:
        send_json(handler, {"ok": False, "errors": ["缺少清理分类参数 category"]}, 400)
        return
    ok, msg, freed_b = clean_storage_category(category)
    send_json(handler, {
        "ok": ok,
        "category": category,
        "msg": msg,
        "message": msg,
        "freed_bytes": freed_b,
        "freed_formatted": format_bytes(freed_b),
        "deleted_bytes": freed_b,
        "deleted_formatted": format_bytes(freed_b),
    })



def handle_proxy_test(handler, body: dict) -> None:
    """POST /api/proxy/test 测试网络代理连通性。"""
    proxy_url = str(body.get("proxy", "")).strip()
    targets = [
        ("Google (Gemini)", "https://generativelanguage.googleapis.com"),
        ("Telegram Bot API", "https://api.telegram.org"),
        ("X (Twitter)", "https://x.com"),
        ("Instagram", "https://www.instagram.com"),
    ]

    async def _probe(name, url):
        t0 = time.time()
        try:
            async with httpx.AsyncClient(proxy=proxy_url or None, timeout=8.0, follow_redirects=True) as client:
                r = await client.head(url)
                latency = round((time.time() - t0) * 1000)
                return {"target": name, "ok": r.status_code < 500, "status": r.status_code, "latency_ms": latency}
        except Exception as e:
            return {"target": name, "ok": False, "error": str(e), "latency_ms": None}

    async def _run_all():
        tasks = [_probe(name, url) for name, url in targets]
        return await asyncio.gather(*tasks)

    results = asyncio.run(_run_all())
    send_json(handler, {"ok": True, "proxy": proxy_url or "(直连)", "results": results})


def handle_members(handler, load_raw_config_fn) -> None:
    """拉取成员花名册（从官方 API 或本地缓存）。query: ?account=..."""
    qs = parse_qs(handler.path.partition("?")[2])
    account = (qs.get("account") or [""])[0]
    if not account:
        send_json(handler, {"ok": False, "errors": ["缺少 account 参数"]}, 400)
        return
    try:
        raw = load_raw_config_fn()
    except Exception as e:
        send_json(handler, {"ok": False, "errors": [f"读取 config.json 失败: {e}"]}, 500)
        return
    if account not in raw.get("accounts", {}):
        send_json(handler, {"ok": False, "errors": [f"未知账号: {account!r}"]}, 400)
        return

    try:
        from src.member_directory import get_member_directory_async
        members, err = asyncio.run(get_member_directory_async(account))
        if err:
            send_json(handler, {"ok": False, "errors": [err]}, 502)
            return
        send_json(handler, {"ok": True, "account": account, "members": members})
    except Exception as e:
        send_json(handler, {"ok": False, "errors": [f"拉取花名册失败: {e}"]}, 500)


def handle_test_push(handler, body: dict, on_test_push_cb) -> None:
    """POST /api/test_push 测试向各渠道发送测试消息。"""
    if on_test_push_cb is None:
        send_json(handler, {"ok": False, "errors": ["独立运行模式不支持测试推送"]}, 400)
        return
    channel = str(body.get("channel", "")).strip()
    target = str(body.get("target", "")).strip()
    text = str(body.get("text", "这是来自 Sakamichi WebUI 的测试推送。")).strip()
    if channel not in ("tg", "napcat", "qq_official"):
        send_json(handler, {"ok": False, "errors": [f"不支持的通道: {channel!r}"]}, 400)
        return
    if not target:
        send_json(handler, {"ok": False, "errors": ["缺少推送目标 target"]}, 400)
        return
    if channel == "napcat" and not target.isdigit():
        send_json(handler, {"ok": False, "errors": ["NapCat 目标群号必须是数字"]}, 400)
        return
    ok, err = on_test_push_cb(channel, target, text)
    if not ok:
        send_json(handler, {"ok": False, "errors": [f"推送失败: {err}"]}, 502)
        return
    send_json(handler, {"ok": True, "message": "测试推送成功发送"})


def handle_openid_status(handler, on_openid_cb) -> None:
    """GET /api/qq_openid/status 返回 OpenID 监听状态。"""
    from src import qq_openid
    snap = qq_openid.get_state()
    snap["available"] = on_openid_cb is not None
    send_json(handler, {"ok": True, **snap})


def handle_openid_action(handler, action: str, body: dict, on_openid_cb) -> None:
    """POST /api/qq_openid/start 或 /api/qq_openid/stop 控制 OpenID 监听器。"""
    if on_openid_cb is None:
        send_json(handler, {"ok": False, "errors": ["独立模式下不可用，请通过主程序启动"]}, 400)
        return
    if action == "stop":
        ok, msg = on_openid_cb("stop", "", "")
        send_json(handler, {"ok": ok, "message": msg})
        return
    app_id = str(body.get("app_id", "")).strip()
    secret = str(body.get("client_secret", "")).strip()
    bot_name = str(body.get("bot_name", "")).strip()
    if not app_id:
        send_json(handler, {"ok": False, "errors": ["缺少 App ID"]}, 400)
        return
    if not secret:
        if bot_name:
            sec_key = f"{bot_name.upper()}_CLIENT_SECRET"
            secret = os.getenv(sec_key, "").strip()
        if not secret:
            secret = os.getenv("QQ_BOT_CLIENT_SECRET", "").strip() or os.getenv("QQ_OFFICIAL_BOT_CLIENT_SECRET", "").strip()
    if not secret:
        send_json(handler, {"ok": False, "errors": ["缺少 Client Secret 且 .env 中未找到"]}, 400)
        return
    ok, msg = on_openid_cb("start", app_id, secret)
    if not ok:
        send_json(handler, {"ok": False, "errors": [msg]}, 400)
        return
    send_json(handler, {"ok": True, "message": msg})


async def smart_parse_credentials_text(raw: str, account: str = "") -> dict:
    """智能解析用户粘贴的 cURL / Headers / Signin Payload 文本。"""
    # 彻底清洗 Windows cmd 特有的所有转义模式 (如 ^\^", ^", ^{, ^}, ^&, ^|, ^$)
    cleaned = (
        raw.replace(r'^\^"', '"')
        .replace(r'\^"', '"')
        .replace(r'^\^', '')
        .replace('^^', '^')
        .replace('^"', '"')
        .replace('^{', '{')
        .replace('^}', '}')
        .replace('^&', '&')
        .replace('^|', '|')
        .replace('^$', '$')
    )
    result = {"token": "", "cookie": "", "refresh_token": "", "extracted": []}

    group_type = "nogizaka"
    acc_api_base = ""
    acc_web_origin = ""
    acc_app_tag = ""
    if account:
        try:
            import json5
            with open("config/config.json", "r", encoding="utf-8") as f:
                raw_cfg = json5.load(f)
            acc_data = raw_cfg.get("accounts", {}).get(account, {})
            group_type = acc_data.get("group_type") or acc_data.get("group") or "nogizaka"
            acc_api_base = acc_data.get("api_base") or ""
            acc_web_origin = acc_data.get("web_origin") or ""
            acc_app_tag = acc_data.get("app_tag") or ""
        except Exception:
            pass

    # 1. 检查是否包含 signin 请求体（用户在登录瞬间复制的 cURL）
    signin_match = (
        re.search(r'--data-raw\s+["\']?(\{.+?\})["\']?(?:\s+&|\s*$|\s+-)', cleaned, re.DOTALL)
        or re.search(r'-d\s+["\']?(\{.+?\})["\']?(?:\s+&|\s*$|\s+-)', cleaned, re.DOTALL)
        or re.search(r'--data-raw\s+["\'](\{.+?\})["\']', cleaned)
        or re.search(r'-d\s+["\'](\{.+?\})["\']', cleaned)
    )
    if "signin" in cleaned and signin_match:
        try:
            import json
            json_str = signin_match.group(1).strip()
            try:
                body_json = json.loads(json_str)
            except Exception:
                body_json = json.loads(json_str.replace(r'\"', '"'))

            # 从 cURL 中动态提取目标 URL
            url = ""
            url_m = re.search(r'(?:--url\s+["\']?|curl\s+["\']?)(https?://[^\s"\'>]+)', cleaned)
            if url_m and "signin" in url_m.group(1).lower():
                url = url_m.group(1).strip().strip('"').strip("'")
            if not url:
                if acc_api_base:
                    url = f"{acc_api_base.rstrip('/')}/v2/signin"
                elif group_type.lower() == "yodel" or "yodel" in cleaned.lower():
                    url = "https://api.service.yodel-app.com/v2/signin"
                else:
                    domain_part = group_type if group_type.endswith("46") else f"{group_type}46"
                    url = f"https://api.message.{domain_part}.com/v2/signin"

            # 提取 app-id
            app_id = ""
            app_id_m = re.search(r'x-talk-app-id:\s*([^\r\n"\']+)', cleaned, re.IGNORECASE)
            if app_id_m:
                app_id = app_id_m.group(1).strip()
            if not app_id:
                if acc_app_tag:
                    app_id = f"jp.co.sonymusic.communication.{acc_app_tag} 2.5"
                elif group_type.lower() == "yodel" or "yodel" in url:
                    app_id = "jp.co.sonymusic.communication.yodel 2.5"
                else:
                    app_id = f"jp.co.sonymusic.communication.{group_type} 2.5"

            # 提取 origin 与 referer
            origin = ""
            origin_m = re.search(r'origin:\s*([^\r\n"\']+)', cleaned, re.IGNORECASE)
            if origin_m:
                origin = origin_m.group(1).strip()
            if not origin:
                if acc_web_origin:
                    origin = acc_web_origin.rstrip("/")
                elif group_type.lower() == "yodel" or "yodel" in url:
                    origin = "https://service.yodel-app.com"
                else:
                    domain_part = group_type if group_type.endswith("46") else f"{group_type}46"
                    origin = f"https://message.{domain_part}.com"

            headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "origin": origin,
                "referer": origin + "/",
                "x-talk-app-id": app_id,
                "x-talk-app-platform": "web",
            }

            # 附带 cURL 中可能包含的前置 Cookie
            req_cookie_m = re.search(r'(?:-b|--cookie)\s+["\']([^"\']+)["\']', cleaned, re.IGNORECASE)
            if req_cookie_m:
                headers["cookie"] = req_cookie_m.group(1).strip()

            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.post(url, headers=headers, json=body_json)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("access_token"):
                        result["token"] = data["access_token"]
                        result["extracted"].append("access_token (由登录接口自动换取)")
                    cookies = []
                    for sc in r.headers.get_list("set-cookie"):
                        sc_part = sc.split(";")[0].strip()
                        if sc_part and "=" in sc_part:
                            cookies.append(sc_part)
                    if req_cookie_m:
                        for part in req_cookie_m.group(1).split(";"):
                            p_trim = part.strip()
                            if p_trim and "=" in p_trim:
                                cookies.append(p_trim)
                    if cookies:
                        from config.credentials import _clean_cookie_string
                        merged = {}
                        for c_item in cookies:
                            merged.update(_clean_cookie_string(c_item))
                        if merged:
                            result["cookie"] = "; ".join(f"{k}={v}" for k, v in merged.items())
                            result["extracted"].append("session Cookie (由登录响应下发，可长期自动续期)")
        except Exception as e:
            from src.logger import log_all
            log_all(f"⚠️ smart_parse 模拟登录请求失败: {e}")

    # 2. 提取所有 Authorization Bearer 或 access_token，并自动优选最新未过期的 Token
    if not result["token"]:
        token_candidates = []
        for m in re.finditer(r'(?:authorization|bearer)\s*[:=]?\s*(?:bearer\s+)?([a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-+/=]+)', cleaned, re.IGNORECASE):
            token_candidates.append(m.group(1).strip())
        for m in re.finditer(r'["\']?access_token["\']?\s*[:=]\s*["\']([a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-+/=]+)["\']', cleaned, re.IGNORECASE):
            token_candidates.append(m.group(1).strip())

        if token_candidates:
            from config.credentials import _decode_token_exp
            token_candidates = list(dict.fromkeys(token_candidates))
            token_candidates.sort(key=lambda t: _decode_token_exp(t) or 0, reverse=True)
            result["token"] = token_candidates[0]
            result["extracted"].append("Token (JWT)")

    # 2.1 若未匹配到三段式 JWT，则回退匹配任意通用 API Token (16 位以上)
    if not result["token"]:
        m = re.search(r'(?:authorization|bearer|x-access-token|token)\s*[:=]\s*(?:bearer\s+)?([a-zA-Z0-9_\-\.]{16,})', cleaned, re.IGNORECASE)
        if m:
            result["token"] = m.group(1).strip()
            result["extracted"].append("Token (API)")

    # 3. 提取并智能合并所有 Cookie (-b, --cookie, -H "cookie: ...", cookie: ..., Set-Cookie)
    if not result["cookie"]:
        cookie_candidates = []
        for m in re.finditer(r'(?:-b|--cookie)\s+["\']([^"\']+)["\']', cleaned, re.IGNORECASE):
            cookie_candidates.append(m.group(1).strip())
        for m in re.finditer(r'(?:-H|--header)\s+["\']cookie:\s*([^"\']+)["\']', cleaned, re.IGNORECASE):
            cookie_candidates.append(m.group(1).strip())
        for m in re.finditer(r'^cookie:\s*(.+)$', cleaned, re.IGNORECASE | re.MULTILINE):
            cookie_candidates.append(m.group(1).strip())
        for m in re.finditer(r'set-cookie:\s*([^;\r\n]+)', cleaned, re.IGNORECASE):
            cookie_candidates.append(m.group(1).strip())

        if cookie_candidates:
            from config.credentials import _clean_cookie_string
            merged_cookies = {}
            cookie_candidates.sort(key=lambda c: ("session=" in c.lower(), len(c)))
            for cand in cookie_candidates:
                parsed = _clean_cookie_string(cand)
                merged_cookies.update(parsed)

            if merged_cookies:
                result["cookie"] = "; ".join(f"{k}={v}" for k, v in merged_cookies.items())
                result["extracted"].append("Cookie")
                if "session" in merged_cookies:
                    result["extracted"].append("session (长期会话)")

    # 4. 提取 refresh_token
    if not result["refresh_token"]:
        m = re.search(r'["\']?refresh_token["\']?\s*[:=]\s*["\']([a-f0-9\-]{32,36})["\']', cleaned, re.IGNORECASE)
        if m:
            result["refresh_token"] = m.group(1).strip()
            result["extracted"].append("Refresh Token")

    return result
