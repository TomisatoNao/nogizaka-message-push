"""单元断言：时间解析、日志截断、HTML 转义、消息链提取

运行: python tests/test_units.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")



from src.constants import ROLE_KEY, TRANSLATION_SEPARATOR


def test_utc_to_jst() -> None:
    print("=== utc_to_jst ===")
    from src.utils import utc_to_jst

    # 标准格式：UTC 12:00 → JST 21:00
    assert utc_to_jst("2026-07-26T12:00:00Z") == "07/26 21:00:00"
    # 带小数秒
    assert utc_to_jst("2026-07-26T12:00:00.123Z") == "07/26 21:00:00"
    # 带显式偏移：已经是 JST 21:00
    assert utc_to_jst("2026-07-26T21:00:00+09:00") == "07/26 21:00:00"
    # 无 tzinfo 时按 UTC 处理
    assert utc_to_jst("2026-07-26T12:00:00") == "07/26 21:00:00"
    # 跨日：UTC 16:00 → JST 次日 01:00
    assert utc_to_jst("2026-07-26T16:00:00Z") == "07/27 01:00:00"
    # 无法解析时原样返回，不抛异常
    assert utc_to_jst("garbage") == "garbage"
    assert utc_to_jst("") == ""
    # 自定义格式
    assert utc_to_jst("2026-07-26T12:00:00Z", "%Y-%m-%d %H:%M") == "2026-07-26 21:00"
    print("  ✅ 7 种输入全部符合预期")


def test_log_truncation() -> None:
    print("=== log_all 逐行截断 ===")
    from src import logger

    long_line = "x" * 200
    short_lines = "\n".join(["a" * 50] * 5)

    import builtins

    captured: list[str] = []
    real_print = builtins.print
    builtins.print = lambda *a, **k: captured.append(" ".join(str(x) for x in a))
    try:
        logger.log_all(short_lines, is_debug=True)
        logger.log_all(long_line, is_debug=True)
    finally:
        builtins.print = real_print

    multi = captured[0]
    single = captured[1]
    # 多行内容每行都短于上限 → 一个字符都不该被截掉
    assert "[TRUNCATED]" not in multi, f"多行内容被误截断: {multi[:120]}"
    assert multi.count("\n") == 4, f"多行结构被破坏: {multi!r}"
    # 单行超长仍然截断
    assert "[TRUNCATED]" in single, "超长单行应被截断"
    print("  ✅ 多行不截断、超长单行仍截断")


def test_escape_html() -> None:
    print("=== tgbot._escape_html / _to_html ===")
    from src.platforms.tgbot import _escape_html, _to_html

    # 不可信文本里的标签必须全部实体化，不能被"还原"成真标签
    assert _escape_html("<b>粗体</b>") == "&lt;b&gt;粗体&lt;/b&gt;"
    assert _escape_html('<a href="x">y</a>') == '&lt;a href="x"&gt;y&lt;/a&gt;'
    assert _escape_html("a & b") == "a &amp; b"
    assert _escape_html("<code>x</code> <i>y</i>") == "&lt;code&gt;x&lt;/code&gt; &lt;i&gt;y&lt;/i&gt;"
    # 转义顺序正确：& 先处理，不会产生 &amp;lt;
    assert _escape_html("&lt;") == "&amp;lt;"

    # _to_html 的裁剪不能切断实体
    text = "&" * 100                      # 转义后每个字符变成 5 个字符
    out = _to_html(text, 50)
    assert len(out) <= 50, f"超出上限: {len(out)}"
    assert out.endswith("..."), out
    body = out[:-3]
    assert body.count("&amp;") * 5 == len(body), f"实体被切断: {body[-8:]!r}"
    # 不超限时原样返回
    assert _to_html("hello", 100) == "hello"
    print("  ✅ 转义无还原、裁剪不切断实体")


def test_chain_extract() -> None:
    print("=== tgbot._chain_extract ===")
    from src.platforms.napcat import build_message_chain, strip_internal_keys
    from src.platforms.tgbot import _chain_extract

    msg_text = {"text": "こんにちは", "updated_at": "2026-07-26T12:00:00Z"}
    msg_media = {"text": "写真です", "file": "https://example.com/a.jpg", "type": "image"}

    # 1) 纯文本 + 翻译（带模型名）
    chain = build_message_chain("冨里奈央", "2026-07-26T12:00:00Z", msg_text, "你好", model_name="glm-4-flash")
    caption, media, translation = _chain_extract(chain)
    assert "こんにちは" in caption and "冨里奈央" in caption, caption
    assert media == [], media
    assert translation.endswith("你好"), translation
    assert "─── 🌐 译文 (glm-4-flash) ───" in translation, f"翻译段应带模型徽章分隔线: {translation}"

    # 2) 图片 + 翻译（未指定模型名）
    chain = build_message_chain("冨里奈央", "2026-07-26T12:00:00Z", msg_media, "照片")
    caption, media, translation = _chain_extract(chain)
    assert media == [("image", "https://example.com/a.jpg")], media
    assert translation.endswith("照片"), translation
    assert "─── 🌐 译文 ───" in translation, f"翻译段应带默认译文分隔线: {translation}"

    # 3) 无翻译
    chain = build_message_chain("冨里奈央", "2026-07-26T12:00:00Z", msg_text, "")
    caption, media, translation = _chain_extract(chain)
    assert translation == "", translation

    # 4) 正文里出现分隔线也不会被误判为翻译段（旧实现会）
    tricky = {"text": f"看这条{TRANSLATION_SEPARATOR}分隔线", "updated_at": "2026-07-26T12:00:00Z"}
    chain = build_message_chain("冨里奈央", "2026-07-26T12:00:00Z", tricky, "")
    caption, media, translation = _chain_extract(chain)
    assert translation == "", f"正文中的分隔线被误判为翻译: {translation!r}"
    assert "分隔线" in caption, caption

    # 5) 发给 OneBot 的 payload 不含内部字段
    chain = build_message_chain("冨里奈央", "2026-07-26T12:00:00Z", msg_text, "你好")
    assert any(ROLE_KEY in item for item in chain), "翻译段应带角色标记"
    stripped = strip_internal_keys(chain)
    assert all(ROLE_KEY not in item for item in stripped), stripped
    assert all(not k.startswith("_") for item in stripped for k in item), stripped
    # 剥离后协议字段完好
    assert [item["type"] for item in stripped] == [item["type"] for item in chain]
    assert stripped[-1]["data"]["text"].endswith("你好")
    print("  ✅ 4 种消息链提取正确、payload 无内部字段")


def test_health_rolling() -> None:
    print("=== health 通道计数滚动清零 ===")
    from src.health import HealthTracker

    tracker = HealthTracker()
    tracker.initialize(summary_interval=2)
    tracker.record_channel("napcat", True)
    tracker.record_channel("napcat", False, "群 1 发送失败")

    assert tracker.cycle_complete() is None          # 第 1 轮：未到摘要周期
    summary = tracker.cycle_complete()               # 第 2 轮：输出摘要
    assert summary and "napcat" in summary and "1/2" in summary, summary

    stats = tracker._channels["napcat"]
    assert stats.total == 0 and stats.success == 0, "摘要后计数应清零"
    assert stats.last_error == "群 1 发送失败", "last_error 应保留"
    print("  ✅ 摘要输出 1/2，输出后计数清零、last_error 保留")


def test_time_record_skip() -> None:
    print("=== write_time_record 值未变时跳过写盘 ===")
    import asyncio
    import os
    import tempfile
    from config.credentials import write_time_record

    async def run() -> None:
        path = os.path.join(tempfile.gettempdir(), "_nmp_time_test.txt")
        lock = asyncio.Lock()

        await write_time_record(path, lock, "2026-07-26T12:00:00Z")
        with open(path, encoding="utf-8") as f:
            assert f.read() == "2026-07-26T12:00:00Z"

        # 删掉文件后用相同值再写：应被值缓存跳过，文件不重新出现
        os.remove(path)
        await write_time_record(path, lock, "2026-07-26T12:00:00Z")
        assert not os.path.exists(path), "值未变时不应写盘"

        # 值变了则正常写入
        await write_time_record(path, lock, "2026-07-26T13:00:00Z")
        with open(path, encoding="utf-8") as f:
            assert f.read() == "2026-07-26T13:00:00Z"
        os.remove(path)

    asyncio.run(run())
    print("  ✅ 相同值跳过、新值正常落盘")


def test_stop_signal_file() -> None:
    """停止信号文件：外部创建即请求优雅退出。

    计划任务 / systemd 启动的进程常需管理员权限才能强杀，信号文件绕开
    权限问题，也保证走完整清理流程。
    """
    print("=== 停止信号文件 ===")
    import src.app as app_mod

    orig = app_mod.STOP_FILE
    tmp = Path(tempfile.mkdtemp(prefix="stopsig_")) / "service.stop"
    app_mod.STOP_FILE = tmp
    try:
        assert not app_mod._stop_requested(), "文件不存在时不应请求停止"
        tmp.write_text("stop", encoding="utf-8")
        assert app_mod._stop_requested(), "文件存在时应请求停止"
        tmp.unlink()
        assert not app_mod._stop_requested(), "删除后应恢复"
    finally:
        app_mod.STOP_FILE = orig
    print("  ✅ 信号文件语义正确")


def test_powershell_scripts_have_bom() -> None:
    """含中文的 .ps1 必须带 UTF-8 BOM。

    Windows PowerShell 5.1 用系统 ANSI 代码页读取无 BOM 的脚本，
    中文会变乱码并导致语法错误——脚本直接跑不起来。
    """
    print("=== .ps1 编码（UTF-8 BOM）===")
    root = Path(__file__).resolve().parent.parent
    checked = 0
    for ps1 in sorted(root.glob("tools/*.ps1")):
        raw = ps1.read_bytes()
        if not any(b > 0x7F for b in raw):
            continue                      # 纯 ASCII 脚本无所谓
        assert raw[:3] == b"\xef\xbb\xbf", (
            f"{ps1.name} 含非 ASCII 字符但缺少 UTF-8 BOM，"
            f"PowerShell 5.1 会解析失败"
        )
        checked += 1
def test_cookie_cleaner() -> None:
    print("=== _clean_cookie_string ===")
    from config.credentials import _clean_cookie_string

    # 1) 标准单行
    c1 = _clean_cookie_string("session=abc123; S5SI=def456; Path=/; Domain=.nogizaka46.com; Secure; HttpOnly")
    assert c1 == {"session": "abc123", "S5SI": "def456"}, c1

    # 2) 带有 Cookie: 或 Set-Cookie: 标头前缀的多行格式
    c2 = _clean_cookie_string("Cookie: session=xyz789; other=111\nSet-Cookie: S5SI=222; max-age=3600; samesite=lax")
    assert c2 == {"session": "xyz789", "other": "111", "S5SI": "222"}, c2

    # 3) 忽略空值与纯属性
    c3 = _clean_cookie_string("; ;; Secure; HttpOnly;  ")
    assert c3 == {}, c3
    print("  ✅ Cookie 增强解析符合预期")


def test_smart_parse_credentials() -> None:
    print("=== _smart_parse_credentials_text ===")
    from src.webui import _Handler
    import asyncio

    h = _Handler.__new__(_Handler)

    # 1) Windows cURL with -b and token
    text1 = '''curl --url "https://api.message.nogizaka46.com/v2/profile" ^
  -H "authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3ODY3MTUzODgsInN1YiI6IjE4MTU2NyJ9.XBuPTPyjCV0_EJquCdvEpo_OMmqzbdjQa2nQOHo6hms" ^
  -b "wap_last_event=showWidgetPage; _tt_enable_cookie=1; session=sess_abc"'''
    res1 = asyncio.run(h._smart_parse_credentials_text(text1))
    assert res1["token"] == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3ODY3MTUzODgsInN1YiI6IjE4MTU2NyJ9.XBuPTPyjCV0_EJquCdvEpo_OMmqzbdjQa2nQOHo6hms", res1
    assert "session=sess_abc" in res1["cookie"], res1
    assert "Token (JWT)" in res1["extracted"]
    assert "Cookie" in res1["extracted"]
    print("  ✅ cURL -b 与 Authorization 提取正确")


def test_extract_bilingual_pairs() -> None:
    print("=== _extract_bilingual_pairs ===")
    from src.notifier import _extract_bilingual_pairs

    # 1) 包含未翻译声音拟声词/AA（无对应 span 译文），不能跨越 em 偷取后文 span
    html = (
        "<em>ｸﾞﾙｸﾞﾙ</em><br><br>"
        "<em>ｼｭｰｰｰｰｰ</em><br><br>"
        "<em>おひさまが幸せに暮らせますように.。.☆</em><br>"
        "<span>希望Ohisama（日向坂粉丝名）们都能幸福地生活.。.☆</span><br><br>"
        "<em>ｷﾗﾝｯｯ</em><br><br>"
        "<em>流れ星がお願いを聞いてくれる時間</em><br>"
        "<span>流星能听见愿望的时间</span>"
    )
    pairs = _extract_bilingual_pairs(html)
    assert len(pairs) == 5, pairs
    assert pairs[0] == ("ｸﾞﾙｸﾞﾙ", ""), pairs[0]
    assert pairs[1] == ("ｼｭｰｰｰｰｰ", ""), pairs[1]
    assert pairs[2] == ("おひさまが幸せに暮らせますように.。.☆", "希望Ohisama（日向坂粉丝名）们都能幸福地生活.。.☆"), pairs[2]
    assert pairs[3] == ("ｷﾗﾝｯｯ", ""), pairs[3]
    assert pairs[4] == ("流れ星がお願いを聞いてくれる時間", "流星能听见愿望的时间"), pairs[4]

    # 2) 包含图片标签自动压缩
    html2 = (
        "<em>段落1</em><br><span>译文1</span><br><br>"
        "<img src='https://example.com/1.jpg'><br><br>"
        "<img src='https://example.com/2.jpg'><br><br>"
        "<em>段落2</em><br><span>译文2</span>"
    )
    pairs2 = _extract_bilingual_pairs(html2, media_urls=["https://example.com/1.jpg", "https://example.com/2.jpg"])
    assert len(pairs2) == 3, pairs2
    assert pairs2[0] == ("段落1", "译文1")
    assert pairs2[1] == "[写真1-2]"
    assert pairs2[2] == ("段落2", "译文2")
    print("  ✅ _extract_bilingual_pairs 边界隔离与图片压缩测试通过")


def main() -> None:
    test_utc_to_jst()
    test_log_truncation()
    test_escape_html()
    test_chain_extract()
    test_cookie_cleaner()
    test_smart_parse_credentials()
    test_extract_bilingual_pairs()
    test_health_rolling()
    test_time_record_skip()
    test_stop_signal_file()
    test_powershell_scripts_have_bom()
    print("\n" + "=" * 50)
    print("🎉 全部单元断言通过")


if __name__ == "__main__":
    main()
