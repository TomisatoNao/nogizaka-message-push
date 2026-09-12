"""本地无副作用验证 NapCat 反向 WebSocket 入站链路。

该脚本不会启动主程序、不会读取真实社媒凭证，也不会向任何 QQ 群发送消息。
它在 127.0.0.1:46047 启动一个使用真实监听器的临时端点，并用内置假服务确认：

* 错误 Token 会被拒绝；
* OneBot 11 群消息可以被接受并进入有界队列；
* 社媒 URL 会交给后台服务，且默认不翻译、不归档；
* 目标群号来自路由白名单。

运行：``python tools/test_napcat_local.py``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.platforms.napcat_listener import NapCatInboundListener


DEFAULT_GROUP_ID = 707891867
DEFAULT_PORT = 46047
DEFAULT_PATH = "/api/napcat/events"


def _read_env_token() -> str:
    """读取本地 .env 中的事件 Token，不打印 Token 内容。"""

    token = os.getenv("NAPCAT_EVENT_TOKEN", "").strip()
    if token:
        return token
    try:
        from dotenv import dotenv_values
    except ImportError:
        return ""
    values = dotenv_values(".env")
    return str(values.get("NAPCAT_EVENT_TOKEN") or "").strip()


def _event(group_id: int, message_id: int = 9001) -> dict[str, object]:
    return {
        "time": 1_758_000_000,
        "self_id": 111222333,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": group_id,
        "user_id": 999888777,
        "message": [
            {
                "type": "text",
                "data": {"text": "看这里 https://x.com/example/status/1"},
            }
        ],
        "raw_message": "看这里 https://x.com/example/status/1",
    }


async def _connect(uri: str, token: str):
    """兼容 websockets 12--16 的请求头参数名。"""

    import websockets

    headers = {"Authorization": f"Bearer {token}"}
    try:
        return await websockets.connect(uri, additional_headers=headers)
    except TypeError as exc:
        if "additional_headers" not in str(exc):
            raise
        return await websockets.connect(uri, extra_headers=headers)


async def run(port: int, group_id: int) -> int:
    token = _read_env_token()
    if len(token) < 16:
        print("FAIL: .env 未配置 NAPCAT_EVENT_TOKEN（至少 16 位）")
        return 1

    calls: list[tuple[str, dict[str, object]]] = []

    class FakeService:
        def process_url(self, url: str, **kwargs: object) -> SimpleNamespace:
            calls.append((url, kwargs))
            return SimpleNamespace(
                post=SimpleNamespace(
                    platform="tiktok",
                    post_id="local-test",
                    media=[object()],
                ),
                completed=True,
                delivery=SimpleNamespace(
                    outcome="success",
                    matched_routes=1,
                    success_routes=1,
                    media_sent=1,
                ),
            )

    settings = {
        "enable_napcat_qq": True,
        "napcat_routes": [{"group_id": group_id}],
        "napcat_inbound": {
            "enabled": True,
            "transport": "reverse_ws",
            "listen_host": "127.0.0.1",
            "listen_port": port,
            "event_path": DEFAULT_PATH,
            "translate": False,
            "archive": False,
            "queue_size": 4,
            "workers": 1,
            "cooldown_seconds": 0,
        },
    }
    listener = NapCatInboundListener(
        config_provider=lambda: settings,
        service_provider=lambda: FakeService(),
        event_token=token,
    )
    if not await listener.start():
        print("FAIL: 入站监听未启动")
        return 1

    uri = f"ws://127.0.0.1:{port}{DEFAULT_PATH}"
    try:
        # 先确认错误 Token 不会进入队列。
        rejected = False
        wrong_socket = None
        try:
            wrong_socket = await _connect(uri, "wrong-token")
            try:
                await asyncio.wait_for(wrong_socket.recv(), 1.0)
            except Exception as exc:  # websockets 12--16 均以关闭异常结束 recv
                rejected = getattr(exc, "code", None) == 4401
        finally:
            if wrong_socket is not None:
                await wrong_socket.close()
        if not rejected:
            print("FAIL: 错误 Token 未被拒绝")
            return 1

        async with await _connect(uri, token) as websocket:
            await websocket.send(json.dumps(_event(group_id)))
            await websocket.send(json.dumps(_event(group_id, 9002)))

        for _ in range(80):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.025)

        if len(calls) != 2:
            print(f"FAIL: 后台服务收到 {len(calls)} 条，预期 2 条")
            return 1
        if any(kwargs.get("translate") for _, kwargs in calls):
            print("FAIL: 本地默认翻译开关不是 false")
            return 1
        if any(kwargs.get("archive") for _, kwargs in calls):
            print("FAIL: 本地默认归档开关不是 false")
            return 1

        print(
            "PASS: NapCat 反向 WebSocket 本地链路正常 | "
            f"endpoint={uri} | group={group_id} | processed={len(calls)}"
        )
        return 0
    finally:
        await listener.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="本地验证 NapCat 反向 WebSocket 入站链路")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--group-id", type=int, default=DEFAULT_GROUP_ID)
    args = parser.parse_args()
    return asyncio.run(run(args.port, args.group_id))


if __name__ == "__main__":
    raise SystemExit(main())
