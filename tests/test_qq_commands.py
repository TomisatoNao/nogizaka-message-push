"""验证官方 Bot 指令：权限白名单、指令分发、输出格式

运行: python tests/test_qq_commands.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ME = "OWNER_OPENID_123"
STRANGER = "SOMEONE_ELSE_456"


async def _check_listener_sync(cfg) -> None:
    """_sync_command_listeners() 应按配置增删任务，不必重启进程。"""
    from src import app, qq_openid

    started: list[tuple[str, str]] = []

    async def fake_listen(app_id, secret, _on_message, *args, **kwargs):
        started.append((app_id, secret))
        await asyncio.sleep(3600)

    real_listen = qq_openid.listen_forever
    qq_openid.listen_forever = fake_listen
    bots = cfg.QQ_OFFICIAL_BOTS
    try:
        bots.clear()
        cfg.QQ_COMMANDS_ENABLED = False
        app._sync_command_listeners()
        assert not app._command_listeners, "开关关闭时不该有监听"

        # 热重载：打开开关并加一个 Bot
        cfg.QQ_COMMANDS_ENABLED = True
        bots.append({"name": "b1", "app_id": "A1", "client_secret": "s1", "target_openid": "O1"})
        app._sync_command_listeners()
        await asyncio.sleep(0)
        assert list(app._command_listeners) == ["A1"], app._command_listeners

        app._sync_command_listeners()      # 再次重载不应重复启动
        await asyncio.sleep(0)
        assert len(started) == 1, f"重复启动: {started}"

        bots.append({"name": "b2", "app_id": "A2", "client_secret": "s2", "target_openid": "O2"})
        app._sync_command_listeners()
        await asyncio.sleep(0)
        assert sorted(app._command_listeners) == ["A1", "A2"], app._command_listeners

        bots[0]["client_secret"] = "s1new"  # 改 secret 应重挂该 Bot
        app._sync_command_listeners()
        await asyncio.sleep(0)
        assert started[-1] == ("A1", "s1new"), started

        removed = app._command_listeners["A2"][1]
        bots.pop()
        app._sync_command_listeners()
        await asyncio.sleep(0)
        assert list(app._command_listeners) == ["A1"], app._command_listeners
        assert removed.cancelled() or removed.done(), "删掉的 Bot 监听应被取消"

        cfg.QQ_COMMANDS_ENABLED = False    # 关总开关应全部停掉
        app._sync_command_listeners()
        await asyncio.sleep(0)
        assert not app._command_listeners, app._command_listeners
    finally:
        for _, task in list(app._command_listeners.values()):
            task.cancel()
        app._command_listeners.clear()
        qq_openid.listen_forever = real_listen
        cfg.QQ_COMMANDS_ENABLED = False


def main() -> None:
    import config.config as cfg
    from src import archive, qq_commands

    tmpdir = Path(tempfile.mkdtemp(prefix="qqcmd_test_"))
    saved = {
        "archive_dir": cfg.ARCHIVE_DIR, "archive_enabled": cfg.ARCHIVE_ENABLED,
        "archive_media": cfg.ARCHIVE_MEDIA, "bots": list(cfg.QQ_OFFICIAL_BOTS),
        "monitor": list(cfg.MONITOR_LIST), "allow": list(getattr(cfg, "QQ_COMMANDS_ALLOW", [])),
    }
    cfg.ARCHIVE_DIR = str(tmpdir)
    cfg.ARCHIVE_ENABLED = True
    cfg.ARCHIVE_MEDIA = False
    cfg.QQ_OFFICIAL_BOTS.clear()
    cfg.QQ_OFFICIAL_BOTS.append({"name": "b1", "app_id": "1", "client_secret": "s",
                                 "target_openid": ME})
    cfg.MONITOR_LIST.clear()
    cfg.MONITOR_LIST.append({"m_name": "测试 成员", "m_id": "55", "group_type": "nogizaka46",
                             "account_id": "acc", "target_groups": [123], "tg_chat_id": ""})
    cfg.QQ_COMMANDS_ALLOW = []

    try:
        # ── Test 1: 权限白名单 ───────────────────────────
        print("=== Test 1: 权限控制 ===")
        assert qq_commands.allowed_senders() == {ME}, "默认白名单应取 target_openid"
        assert qq_commands.handle("/status", STRANGER) is None, "陌生人必须无响应"
        assert qq_commands.handle("/help", ME) is not None, "白名单用户应有响应"
        assert qq_commands.handle("你好", ME) is None, "非指令消息不响应"
        assert qq_commands.handle("", ME) is None

        cfg.QQ_COMMANDS_ALLOW = ["EXPLICIT_ONE"]
        assert qq_commands.allowed_senders() == {"EXPLICIT_ONE"}, "显式白名单应覆盖默认"
        assert qq_commands.handle("/help", ME) is None, "不在显式白名单内应无响应"
        cfg.QQ_COMMANDS_ALLOW = []

        cfg.QQ_OFFICIAL_BOTS[0]["target_openid"] = ""
        assert qq_commands.allowed_senders() == set()
        assert qq_commands.handle("/help", ME) is None, "白名单为空时任何人都不响应"
        cfg.QQ_OFFICIAL_BOTS[0]["target_openid"] = ME
        print("✅ Test 1 通过\n")

        # ── Test 2: 指令分发 ────────────────────────────
        print("=== Test 2: 指令分发 ===")
        help_text = qq_commands.handle("/help", ME)
        for name in ("status", "members", "latest", "search", "stats"):
            assert f"/{name}" in help_text, f"帮助应含 /{name}"
        assert "未知指令" in qq_commands.handle("/nonexistent", ME)
        assert qq_commands.handle("/HELP", ME) is not None, "指令名应大小写不敏感"

        members = qq_commands.handle("/members", ME)
        assert "测试 成员" in members and "id=55" in members
        status = qq_commands.handle("/status", ME)
        assert "轮" in status and "运行" in status
        print("✅ Test 2 通过\n")

        # ── Test 3: 归档类指令 ──────────────────────────
        print("=== Test 3: 归档查询 ===")
        member = {"m_name": "测试 成员"}
        for i, (text, trans) in enumerate([
            ("ライブ楽しかった", "LIVE 很开心"),
            ("おはよう", "早上好"),
            ("ありがとう", "谢谢"),
        ]):
            utc = f"2026-07-0{i + 1}T10:00:00Z"
            asyncio.run(archive.archive_message(
                member, {"id": 900 + i, "type": "text", "text": text,
                         "published_at": utc, "updated_at": utc}, translated=trans))

        latest = qq_commands.handle("/latest", ME)
        assert "谢谢" in latest or "ありがとう" in latest, f"应含最新一条: {latest}"
        assert qq_commands.handle("/latest 测试成员 2", ME).count("[") >= 2, "应支持指定条数"
        assert "没找到该成员" in qq_commands.handle("/latest 不存在的人", ME)

        found = qq_commands.handle("/search LIVE", ME)
        assert "命中 1 条" in found, f"译文应可搜: {found}"
        assert "没有命中" in qq_commands.handle("/search 绝对不存在的词", ME)
        assert "用法" in qq_commands.handle("/search", ME), "缺参数应给用法提示"

        stats = qq_commands.handle("/stats", ME)
        assert "3 条" in stats, f"统计应正确: {stats}"
        print("✅ Test 3 通过\n")

        # ── Test 4: 输出长度与异常隔离 ───────────────────
        print("=== Test 4: 边界 ===")
        for cmd in ("/help", "/status", "/members", "/latest", "/stats", "/search a"):
            reply = qq_commands.handle(cmd, ME)
            assert reply and len(reply) <= qq_commands.MAX_REPLY_CHARS, \
                f"{cmd} 回复超长: {len(reply or '')}"

        # 指令内部抛异常时应回报错误而不是崩掉监听循环
        orig = qq_commands._COMMANDS["stats"]
        qq_commands._COMMANDS["stats"] = lambda _a: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            assert "出错" in qq_commands.handle("/stats", ME)
        finally:
            qq_commands._COMMANDS["stats"] = orig
        print("✅ Test 4 通过\n")

        # ── Test 5: 热重载增删监听 ───────────────────────
        # 曾经的 bug：监听只在进程启动时挂载，管理端热重载加的 Bot 要等重启才上线，
        # 表现为 QQ 那边一直回"机器人灵魂不在线"
        print("=== Test 5: 热重载同步监听 ===")
        asyncio.run(_check_listener_sync(cfg))
        print("✅ Test 5 通过\n")

        # ── Test 6: 社交媒体链接识别 ─────────────────────
        print("=== Test 6: 社交媒体链接识别 ===")
        cfg.QQ_OFFICIAL_BOTS.clear()
        cfg.QQ_OFFICIAL_BOTS.append({"app_id": "A1", "target_openid": ME})
        cfg.QQ_COMMANDS_ALLOW = [ME]
        orig_trigger = qq_commands._trigger_social_reply_task
        qq_commands._trigger_social_reply_task = lambda *a, **kw: None
        try:
            assert qq_commands.handle("https://www.instagram.com/p/123456/", STRANGER) is None, "未授权用户不应响应"
            res_link = qq_commands.handle("https://x.com/nogizaka46/status/123456", ME)
            assert res_link and "社媒链接" in res_link, f"应识别 X 链接: {res_link}"
            res_ins = qq_commands.handle("分享帖子：https://www.instagram.com/p/abcdef/ 看看", ME)
            assert res_ins and "社媒链接" in res_ins, f"应识别 Ins 链接: {res_ins}"
            res_tt = qq_commands.handle("https://vt.tiktok.com/ZSV2J7Bor/", ME)
            assert res_tt and "社媒链接" in res_tt, f"应识别 TikTok 短链接: {res_tt}"
        finally:
            qq_commands._trigger_social_reply_task = orig_trigger
        print("✅ Test 6 通过\n")

    finally:
        cfg.ARCHIVE_DIR = saved["archive_dir"]
        cfg.ARCHIVE_ENABLED = saved["archive_enabled"]
        cfg.ARCHIVE_MEDIA = saved["archive_media"]
        cfg.QQ_OFFICIAL_BOTS.clear()
        cfg.QQ_OFFICIAL_BOTS.extend(saved["bots"])
        cfg.MONITOR_LIST.clear()
        cfg.MONITOR_LIST.extend(saved["monitor"])
        cfg.QQ_COMMANDS_ALLOW = saved["allow"]

    print("=" * 50)
    print("🎉 全部测试通过！官方 Bot 指令工作正常")


if __name__ == "__main__":
    main()
