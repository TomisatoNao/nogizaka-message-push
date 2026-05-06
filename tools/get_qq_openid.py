# ============================================================
# get_qq_openid.py — 临时工具：监听官方 QQ Bot 单聊事件并打印 openid
# ============================================================
import asyncio
import json
import os
import sys
import time

import httpx

try:
    import websockets
except ImportError:
    websockets = None

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config.config import (
    QQ_OFFICIAL_API_BASE,
    QQ_OFFICIAL_BOTS,
    QQ_OFFICIAL_TOKEN_URL,
)

GROUP_AND_C2C_EVENT_INTENT = 1 << 25


def _find_openid_values(obj, path=""):
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_path = f"{path}.{key}" if path else key
            if "openid" in key.lower() and isinstance(value, str):
                found.append((next_path, value))
            found.extend(_find_openid_values(value, next_path))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found.extend(_find_openid_values(value, f"{path}[{i}]"))
    return found


async def _get_access_token(client: httpx.AsyncClient, bot_index: int = 0) -> str:
    """获取指定 Bot 的 access_token。bot_index 为 QQ_OFFICIAL_BOTS 列表中的索引。"""
    if not QQ_OFFICIAL_BOTS or bot_index >= len(QQ_OFFICIAL_BOTS):
        raise RuntimeError("请先在 .env 里填写 QQ_OFFICIAL_BOT1_APP_ID 和 QQ_OFFICIAL_BOT1_CLIENT_SECRET")

    bot_cfg = QQ_OFFICIAL_BOTS[bot_index]
    app_id = bot_cfg.get("app_id")
    client_secret = bot_cfg.get("client_secret")

    if not app_id or not client_secret:
        raise RuntimeError(f"Bot[{bot_index}] 配置不完整，请检查 APP_ID 和 CLIENT_SECRET")

    print(f"使用 Bot [{bot_cfg.get('name', bot_index)}] 连接...")

    resp = await client.post(
        QQ_OFFICIAL_TOKEN_URL,
        json={
            "appId": app_id,
            "clientSecret": client_secret,
        },
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"token 响应里没有 access_token: {data}")
    return token


async def _get_gateway_url(client: httpx.AsyncClient, token: str) -> str:
    headers = {"Authorization": f"QQBot {token}"}
    candidates = [
        f"{QQ_OFFICIAL_API_BASE}/gateway",
        f"{QQ_OFFICIAL_API_BASE}/gateway/bot",
    ]
    last_error = ""
    for url in candidates:
        resp = await client.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            gateway_url = data.get("url")
            if gateway_url:
                return gateway_url
        last_error = f"{url} -> HTTP {resp.status_code}: {resp.text[:200]}"
    raise RuntimeError(f"获取 WebSocket 网关地址失败：{last_error}")


async def _heartbeat(ws, interval_ms: int, seq_ref: dict):
    interval = max(interval_ms / 1000, 1)
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": 1, "d": seq_ref.get("seq")}))


async def main():
    if websockets is None:
        raise RuntimeError(
            "缺少 websockets 依赖。请先运行：\n"
            "C:\\Users\\tomis\\AppData\\Local\\Programs\\Python\\Python314\\python.exe -m pip install websockets"
        )

    # 列出可用的 Bot
    print("可用的官方 QQ Bot：")
    for i, bot in enumerate(QQ_OFFICIAL_BOTS):
        status = "✓ 已配置" if bot.get("app_id") else "✗ 未配置"
        print(f"  [{i}] {bot.get('name', 'unnamed')} - {status}")

    # 选择 Bot（默认第一个）
    bot_index = 0
    if QQ_OFFICIAL_BOTS and QQ_OFFICIAL_BOTS[0].get("app_id"):
        bot_index = 0
    else:
        # 找第一个已配置的
        for i, bot in enumerate(QQ_OFFICIAL_BOTS):
            if bot.get("app_id"):
                bot_index = i
                break

    async with httpx.AsyncClient() as client:
        token = await _get_access_token(client, bot_index)
        gateway_url = await _get_gateway_url(client, token)

    print("已获取 WebSocket 网关，正在连接...")
    print("连接成功后，请用你的 QQ 私聊机器人发一句话。按 Ctrl+C 退出。")

    seq_ref = {"seq": None}
    async with websockets.connect(gateway_url) as ws:
        hello = json.loads(await ws.recv())
        print("收到 Hello:", json.dumps(hello, ensure_ascii=False))

        interval_ms = hello.get("d", {}).get("heartbeat_interval", 45000)
        heartbeat_task = asyncio.create_task(_heartbeat(ws, interval_ms, seq_ref))

        identify = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": GROUP_AND_C2C_EVENT_INTENT,
                "shard": [0, 1],
                "properties": {
                    "$os": "windows",
                    "$browser": "enversion-openid-helper",
                    "$device": "enversion-openid-helper",
                },
            },
        }
        await ws.send(json.dumps(identify))

        try:
            while True:
                raw = await ws.recv()
                event = json.loads(raw)
                if "s" in event and event["s"] is not None:
                    seq_ref["seq"] = event["s"]

                event_type = event.get("t")
                if event.get("op") == 11:
                    continue

                print("\n收到事件:", event_type or f"op={event.get('op')}")
                print(json.dumps(event, ensure_ascii=False, indent=2))

                openids = _find_openid_values(event)
                if openids:
                    print("\n可能的 openid：")
                    for path, value in openids:
                        print(f"  {path} = {value}")
                    print("\n把这个 openid 填入 .env 文件中对应 Bot 的 TARGET_OPENID：")
                    bot_name = QQ_OFFICIAL_BOTS[bot_index].get("name", "bot_1")
                    print(f"  （Bot: {bot_name}）")
                    print(f"QQ_OFFICIAL_{bot_name.upper()}_TARGET_OPENID={openids[0][1]}")
                    print("\n继续监听中，如已拿到可按 Ctrl+C 退出。")
        finally:
            heartbeat_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出。")
    except Exception as e:
        print(f"\n运行失败：{type(e).__name__}: {e}")
        time.sleep(1)
