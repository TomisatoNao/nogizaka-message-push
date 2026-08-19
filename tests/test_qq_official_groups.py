"""验证 QQ 官方 Bot 群聊推送功能

运行: python tests/test_qq_official_groups.py
"""
import asyncio
import sys
import time
from pathlib import Path
try:
    import pytest
    _async_test = pytest.mark.asyncio
except ImportError:
    def _async_test(fn):
        return fn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")


# ── Test 1: is_configured() — 只看凭证，不要求 target_openid ──
def test_is_configured():
    print("\n── Test 1: is_configured() ──")
    from src.platforms.qq_official import QQOfficialBot

    bot_with_target = QQOfficialBot("b1", "app", "sec", "OPENID123")
    check("有 target_openid", bot_with_target.is_configured())

    bot_without_target = QQOfficialBot("b2", "app", "sec", "")
    check("无 target_openid（群专用）", bot_without_target.is_configured())

    bot_no_app = QQOfficialBot("b3", "", "sec", "OPENID")
    check("缺 app_id", not bot_no_app.is_configured())

    bot_no_sec = QQOfficialBot("b4", "app", "", "OPENID")
    check("缺 client_secret", not bot_no_sec.is_configured())


# ── Test 2: _target_base URL 构造 ──
def test_target_base():
    print("\n── Test 2: _target_base() ──")
    from src.platforms.qq_official import QQOfficialBot

    bot = QQOfficialBot("t", "app", "sec", "U1")
    base = bot._target_base("users", "U1")
    check("users scope", base.endswith("/v2/users/U1"),
          f"got: {base}")

    base2 = bot._target_base("groups", "GRP1")
    check("groups scope", base2.endswith("/v2/groups/GRP1"),
          f"got: {base2}")


# ── Test 3: member_filter 过滤 ──
def test_member_filter():
    print("\n── Test 3: member_filter ──")
    from src.platforms.qq_official import QQOfficialBot

    bot = QQOfficialBot("b1", "app", "sec", "U1", group_openid="GRP1",
                        member_filter=["成员A", "成员B"])
    check("member_filter 已存", bot.member_filter == ["成员A", "成员B"])
    check("group_openid 已存", bot.group_openid == "GRP1")

    # member_filter 为空的 Bot —— 推全部成员
    bot2 = QQOfficialBot("b2", "app2", "sec2", "U2")
    check("空 member_filter", bot2.member_filter == [])
    check("空 group_openid", bot2.group_openid == "")


# ── Test 4: 异步 — 群发方法调用验证 ──
@_async_test
async def test_send_to_group():
    print("\n── Test 4: send_group_text / send_message_chain_to_group ──")

    from src.platforms.qq_official import QQOfficialBot

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kw):
            self.calls.append((url, kw.get("json"), kw.get("content")))
            from types import SimpleNamespace
            return SimpleNamespace(status_code=200, json=lambda: {"file_info": "FI_FAKE"})

    bot = QQOfficialBot("test", "app", "sec", "USER1")
    client = FakeClient()
    bot.initialize(client)
    bot._access_token = "tok"
    bot._token_expire_at = time.time() + 7200

    # send_group_text
    ok = await bot.send_group_text("GRP1", "hello group")
    check("send_group_text 成功", ok)
    check("send_group_text URL",
          "/v2/groups/GRP1/messages" in client.calls[-1][0],
          f"last URL: {client.calls[-1][0]}")
    check("send_group_text payload msg_type=0",
          client.calls[-1][1].get("msg_type") == 0)

    # send_message_chain_to_group
    chain = [{"type": "text", "data": {"text": "test"}}]
    ok2 = await bot.send_message_chain_to_group("GRP2", {"m_name": "test"}, chain, [])
    check("send_message_chain_to_group 成功", ok2)
    check("send_message_chain_to_group URL",
          "/v2/groups/GRP2/messages" in client.calls[-1][0])

    # 现有 send_text 仍走 users
    await bot.send_text("hello user")
    check("send_text 仍走 users", "/v2/users/USER1/messages" in client.calls[-1][0])

    # 现有 send_message_chain 仍走 users
    await bot.send_message_chain({"m_name": "test"}, chain, [])
    check("send_message_chain 仍走 users", "/v2/users/USER1/messages" in client.calls[-1][0],
          f"last URL: {client.calls[-1][0]}")


# ── 入口 ──
def main():
    test_is_configured()
    test_target_base()
    test_member_filter()
    asyncio.run(test_send_to_group())
    print(f"\n{'='*40}")
    print(f"  通过: {passed}  失败: {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
