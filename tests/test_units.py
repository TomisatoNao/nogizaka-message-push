"""单元断言：时间解析、日志截断、HTML 转义、消息链提取

运行: python tests/test_units.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.constants import ROLE_KEY, ROLE_TRANSLATION, TRANSLATION_SEPARATOR


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

    # 1) 纯文本 + 翻译
    chain = build_message_chain("冨里奈央", "2026-07-26T12:00:00Z", msg_text, "你好")
    caption, media, translation = _chain_extract(chain)
    assert "こんにちは" in caption and "冨里奈央" in caption, caption
    assert media == [], media
    assert translation.endswith("你好"), translation
    assert TRANSLATION_SEPARATOR in translation, "翻译段应带分隔线"

    # 2) 图片 + 翻译
    chain = build_message_chain("冨里奈央", "2026-07-26T12:00:00Z", msg_media, "照片")
    caption, media, translation = _chain_extract(chain)
    assert media == [("image", "https://example.com/a.jpg")], media
    assert translation.endswith("照片"), translation

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


def main() -> None:
    test_utc_to_jst()
    test_log_truncation()
    test_escape_html()
    test_chain_extract()
    test_health_rolling()
    test_time_record_skip()
    print("\n" + "=" * 50)
    print("🎉 全部单元断言通过")


if __name__ == "__main__":
    main()
