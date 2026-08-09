"""监听官方 QQ Bot 群聊事件，打印群 group_openid。

group_openid 只能从「群成员 @机器人」的事件里获得，所以流程是：
先把 Bot 拉进目标群 → 连接 Bot 网关 → 在群里 @机器人 发一条消息 → 从事件里提取 group_openid。

用法:
    python tools/get_qq_group_openid.py              # 用 .env 里第一个已配置的 Bot
    python tools/get_qq_group_openid.py <APP_ID> <CLIENT_SECRET>   # 直接指定凭证

⚠️ group_openid 是 app 级别的（和 Bot 绑定），不同 Bot 的 group_openid 不通用。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.config as cfg  # noqa: E402
from src.qq_openid import listen_once  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _pick_credentials(argv: list[str]) -> tuple[str, str, str]:
    """返回 (app_id, client_secret, 来源说明)。"""
    if len(argv) >= 2:
        return argv[0], argv[1], "命令行参数"
    for bot in cfg.QQ_OFFICIAL_BOTS:
        if bot.get("app_id") and bot.get("client_secret"):
            return bot["app_id"], bot["client_secret"], f"配置中的 Bot [{bot.get('name', '?')}]"
    return "", "", ""


async def main() -> None:
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return

    app_id, secret, source = _pick_credentials(argv)
    if not app_id or not secret:
        print("✗ 没有可用的 Bot 凭证。")
        print("  请在 config.json 声明 Bot 并在 .env 填 {Bot名称大写}_CLIENT_SECRET，")
        print("  或直接传参：python tools/get_qq_group_openid.py <APP_ID> <CLIENT_SECRET>")
        return

    print(f"▸ 使用凭证来源: {source}")
    print("▸ 正在连接 Bot 网关…")
    print("▸ 等待群 @机器人 事件（5 分钟超时，Ctrl+C 退出）")
    print("▸ 提示：先把 Bot 拉进目标群，然后在群里 @机器人 发一条消息\n")

    try:
        result = await listen_once(app_id, secret, mode="group")
    except TimeoutError as e:
        print(f"✗ {e}")
        print("  请确认：1) Bot 已在目标群里  2) 群里有人 @机器人 发了消息")
        return
    except Exception as e:
        print(f"✗ 失败: {type(e).__name__}: {e}")
        return

    print("✅ 已捕获群 group_openid：")
    print(f"   {result['openid']}")
    if result.get("sender"):
        print(f"   （来自 {result['sender']}）")
    print("\n把它填进 config.json 对应成员的 qq_official_groups 字段。")
    print("如果是多 Bot，记得用 {\"bot\": \"名称\", \"group_openid\": \"...\"} 格式指定 Bot。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出。")
