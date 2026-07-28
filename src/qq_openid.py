# ============================================================
# qq_openid.py — 监听官方 QQ Bot 单聊事件，捕获对方 openid
# ============================================================
# 官方 Bot 推送需要目标用户的 openid，而 openid 只能从"用户主动给 Bot
# 发消息"的事件里拿到。这里把这套流程封装成可被网页端驱动的会话：
#
#   start_session(app_id, secret) → 后台连 WebSocket 网关并等待事件
#   get_state()                   → 前端轮询：connecting / waiting / captured / error
#   stop_session()                → 主动结束
#
# 命令行工具 tools/get_qq_openid.py 与网页端共用本模块。
# ============================================================
from __future__ import annotations

import asyncio
import json
import time

import httpx

import config.config as cfg

# 订阅群聊与单聊事件（openid 来自 C2C_MESSAGE_CREATE）
GROUP_AND_C2C_EVENT_INTENT = 1 << 25

SESSION_TIMEOUT = 300      # 无人发消息时 5 分钟自动结束，避免空转占用连接


class OpenIdSession:
    """一次 openid 捕获会话（同一时刻只跑一个）。"""

    def __init__(self) -> None:
        self.state = "idle"        # idle | connecting | waiting | captured | error | stopped
        self.openid = ""
        self.sender = ""           # 附带的用户昵称（若事件里有）
        self.error = ""
        self.started_at = 0.0
        self.task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        remaining = 0
        if self.state in ("connecting", "waiting") and self.started_at:
            remaining = max(0, int(SESSION_TIMEOUT - (time.time() - self.started_at)))
        return {
            "state": self.state,
            "openid": self.openid,
            "sender": self.sender,
            "error": self.error,
            "seconds_left": remaining,
        }


_session = OpenIdSession()


def get_state() -> dict:
    return _session.snapshot()


def find_openid_values(obj, path: str = "") -> list[tuple[str, str]]:
    """深度遍历事件体，找出所有键名含 openid 的字符串值。"""
    found: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_path = f"{path}.{key}" if path else key
            if "openid" in key.lower() and isinstance(value, str) and value:
                found.append((next_path, value))
            found.extend(find_openid_values(value, next_path))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found.extend(find_openid_values(value, f"{path}[{i}]"))
    return found


async def get_access_token(client: httpx.AsyncClient, app_id: str, client_secret: str) -> str:
    resp = await client.post(
        cfg.QQ_OFFICIAL_TOKEN_URL,
        json={"appId": app_id, "clientSecret": client_secret},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"获取 access_token 失败: HTTP {resp.status_code} {resp.text[:200]}")
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"响应里没有 access_token: {resp.text[:200]}")
    return token


async def get_gateway_url(client: httpx.AsyncClient, token: str) -> str:
    headers = {"Authorization": f"QQBot {token}"}
    last = ""
    for url in (f"{cfg.QQ_OFFICIAL_API_BASE}/gateway",
                f"{cfg.QQ_OFFICIAL_API_BASE}/gateway/bot"):
        resp = await client.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and resp.json().get("url"):
            return resp.json()["url"]
        last = f"{url} → HTTP {resp.status_code}"
    raise RuntimeError(f"获取 WebSocket 网关失败: {last}")


async def _heartbeat(ws, interval_ms: int, seq_ref: dict) -> None:
    interval = max(interval_ms / 1000, 1)
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": 1, "d": seq_ref.get("seq")}))


async def listen_once(app_id: str, client_secret: str,
                      on_event=None, timeout: float = SESSION_TIMEOUT) -> dict:
    """连接网关并等待第一个带 openid 的事件。
    返回 {"openid": ..., "sender": ..., "raw": {...}}；超时抛 TimeoutError。"""
    try:
        import websockets
    except ImportError as e:
        raise RuntimeError("缺少依赖 websockets，请执行: pip install websockets") from e

    async with httpx.AsyncClient() as client:
        token = await get_access_token(client, app_id, client_secret)
        gateway = await get_gateway_url(client, token)

    seq_ref: dict = {"seq": None}
    async with websockets.connect(gateway) as ws:
        hello = json.loads(await ws.recv())
        interval_ms = hello.get("d", {}).get("heartbeat_interval", 45000)
        hb = asyncio.create_task(_heartbeat(ws, interval_ms, seq_ref))
        try:
            await ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": GROUP_AND_C2C_EVENT_INTENT,
                    "shard": [0, 1],
                    "properties": {"$os": "linux", "$browser": "sakamichi", "$device": "sakamichi"},
                },
            }))

            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise TimeoutError("等待超时：期间没有收到任何私聊消息")
                raw = await asyncio.wait_for(ws.recv(), timeout=left)
                event = json.loads(raw)
                if event.get("s") is not None:
                    seq_ref["seq"] = event["s"]
                if event.get("op") == 11:          # 心跳 ACK
                    continue
                if on_event:
                    on_event(event)
                hits = find_openid_values(event)
                if hits:
                    author = (event.get("d") or {}).get("author") or {}
                    return {
                        "openid": hits[0][1],
                        "sender": author.get("username", "") or author.get("user_openid", ""),
                        "raw": event,
                    }
        finally:
            hb.cancel()


async def listen_forever(app_id: str, client_secret: str, on_message) -> None:
    """长期监听 Bot 网关的私聊消息（指令功能用），断线自动重连。

    on_message(text, sender_openid) -> str | None，返回非空则作为回复发送。
    """
    from src.logger import log_all
    try:
        import websockets
    except ImportError:
        log_all("⚠️ 缺少 websockets 依赖，官方 Bot 指令功能不可用", is_error=True)
        return

    backoff = 5
    while True:
        try:
            async with httpx.AsyncClient() as client:
                token = await get_access_token(client, app_id, client_secret)
                gateway = await get_gateway_url(client, token)

            seq_ref: dict = {"seq": None}
            async with websockets.connect(gateway) as ws:
                hello = json.loads(await ws.recv())
                hb = asyncio.create_task(
                    _heartbeat(ws, hello.get("d", {}).get("heartbeat_interval", 45000), seq_ref))
                try:
                    await ws.send(json.dumps({
                        "op": 2,
                        "d": {
                            "token": f"QQBot {token}",
                            "intents": GROUP_AND_C2C_EVENT_INTENT,
                            "shard": [0, 1],
                            "properties": {"$os": "linux", "$browser": "sakamichi",
                                           "$device": "sakamichi"},
                        },
                    }))
                    log_all("🤖 官方 Bot 指令监听已连接")
                    backoff = 5
                    # QQ WebSocket 网关约 30 分钟强制踢人（4009 Session timed out），
                    # 被动等掉线再重连会有几秒~几十秒断口。主动在 25 分钟时
                    # 平滑断开重连，保证指令监听始终在线。
                    reconnect_at = time.monotonic() + 1500  # 25 分钟
                    while True:
                        event = await asyncio.wait_for(
                            ws.recv(), timeout=max(reconnect_at - time.monotonic(), 1))
                        if time.monotonic() >= reconnect_at:
                            break
                        if event.get("s") is not None:
                            seq_ref["seq"] = event["s"]
                        if event.get("op") == 11 or event.get("t") != "C2C_MESSAGE_CREATE":
                            continue
                        data = event.get("d") or {}
                        author = data.get("author") or {}
                        sender = author.get("user_openid", "")
                        reply = on_message(data.get("content", ""), sender)
                        if reply:
                            await _reply(data, sender, reply, app_id, client_secret)
                finally:
                    hb.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_all(f"⚠️ 官方 Bot 指令监听断开（{type(e).__name__}: {e}），{backoff}s 后重连",
                    is_error=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


async def _reply(data: dict, sender_openid: str, text: str,
                 app_id: str, client_secret: str) -> None:
    """回复用户私聊。带 msg_id 才算被动回复，不消耗主动推送额度。"""
    from src.logger import log_all
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await get_access_token(client, app_id, client_secret)
            payload = {"content": text, "msg_type": 0}
            if data.get("id"):
                payload["msg_id"] = data["id"]
            resp = await client.post(
                f"{cfg.QQ_OFFICIAL_API_BASE}/v2/users/{sender_openid}/messages",
                headers={"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code >= 400:
                log_all(f"⚠️ Bot 指令回复失败: HTTP {resp.status_code} {resp.text[:150]}",
                        is_error=True)
    except Exception as e:
        log_all(f"⚠️ Bot 指令回复异常: {type(e).__name__}: {e}", is_error=True)


async def _run_session(app_id: str, client_secret: str) -> None:
    from src.logger import log_all
    try:
        _session.state = "connecting"
        result = await listen_once(app_id, client_secret)
        _session.openid = result["openid"]
        _session.sender = result.get("sender", "")
        _session.state = "captured"
        log_all(f"🎯 已捕获 QQ 官方 Bot 目标 openid（来自 {_session.sender or '未知用户'}）")
    except asyncio.CancelledError:
        _session.state = "stopped"
        raise
    except TimeoutError as e:
        _session.state = "error"
        _session.error = str(e)
    except Exception as e:
        _session.state = "error"
        _session.error = f"{type(e).__name__}: {e}"
        log_all(f"⚠️ openid 监听失败: {_session.error}", is_error=True)


def start_session(app_id: str, client_secret: str) -> tuple[bool, str]:
    """启动监听（需在事件循环内调用）。返回 (是否启动, 说明)。"""
    if _session.state in ("connecting", "waiting"):
        return False, "已有监听在进行中，请先停止"
    _session.__init__()          # 重置状态
    _session.state = "connecting"
    _session.started_at = time.time()
    _session.task = asyncio.create_task(_run_session(app_id, client_secret))

    def _mark_waiting(_task=None):
        if _session.state == "connecting":
            _session.state = "waiting"
    asyncio.get_running_loop().call_later(2, _mark_waiting)
    return True, "已开始监听，请让目标用户现在给 Bot 发一条私聊消息"


def stop_session() -> None:
    if _session.task and not _session.task.done():
        _session.task.cancel()
    _session.state = "stopped" if _session.state in ("connecting", "waiting") else _session.state
