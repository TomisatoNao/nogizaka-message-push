"""测试 tagger 模块：文件不存在、功能关闭、schedule_tag 跳过、search 匹配标签

运行: python tests/test_tagger.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config.config as cfg


def setup_test() -> None:
    """每个测试前的公共配置"""
    cfg.ENABLE_IMAGE_TAGGING = True
    cfg.GEMINI_TAG_MODELS = [
        {"name": "test-model", "url": "https://example.com/model:generateContent"},
    ]
    cfg.GEMINI_TAG_MIN_INTERVAL = 0.01
    cfg.GEMINI_API_KEY = "test-key"
    cfg.ARCHIVE_DIR = "/tmp/test_archive"


def test_tag_image_file_not_found() -> None:
    """文件不存在时返回空字符串"""
    print("=== tag_image 文件不存在 ===")
    import asyncio
    from src.tagger import initialize, tag_image
    setup_test()
    initialize()

    result = asyncio.run(tag_image("冨里奈央", "2026/07/images/nonexistent.jpg"))
    assert result == "", f"期望空字符串，实际得到 {result!r}"
    print("  ✅ 正确返回空字符串")


def test_tag_image_disabled() -> None:
    """功能关闭时返回空"""
    print("=== tag_image 功能关闭 ===")
    import asyncio
    from src.tagger import initialize, tag_image
    setup_test()
    cfg.ENABLE_IMAGE_TAGGING = False
    initialize()

    result = asyncio.run(tag_image("冨里奈央", "2026/07/images/whatever.jpg"))
    assert result == "", f"功能关闭时应返回空字符串，实际得到 {result!r}"
    print("  ✅ 正确返回空字符串")


def test_schedule_tag_skips_non_picture() -> None:
    """非图片类型的消息跳过"""
    print("=== schedule_tag 非图片跳过 ===")
    from src.tagger import schedule_tag
    setup_test()

    # 不应该抛异常
    schedule_tag("冨里奈央", {"_local_file": "test.mp4", "type": "video"})
    schedule_tag("冨里奈央", {"_local_file": "test.m4a", "type": "voice"})
    schedule_tag("冨里奈央", {"_local_file": "test.txt", "type": "text"})
    print("  ✅ 非图片消息正确跳过（无异常）")


def test_schedule_tag_skips_already_tagged() -> None:
    """已有 _tags 的消息跳过"""
    print("=== schedule_tag 已有标签跳过 ===")
    from src.tagger import schedule_tag
    setup_test()

    schedule_tag("冨里奈央", {"_local_file": "test.jpg", "type": "picture", "_tags": "笑脸 自拍"})
    print("  ✅ 已有标签消息正确跳过（无异常）")


def test_schedule_tag_skips_no_local_file() -> None:
    """没有本地文件的消息跳过"""
    print("=== schedule_tag 无本地文件跳过 ===")
    from src.tagger import schedule_tag
    setup_test()

    schedule_tag("冨里奈央", {"type": "picture", "_local_file": ""})
    print("  ✅ 无本地文件消息正确跳过（无异常）")


def test_schedule_tag_disabled() -> None:
    """功能关闭时 schedule_tag 无动作"""
    print("=== schedule_tag 功能关闭 ===")
    from src.tagger import schedule_tag
    setup_test()
    cfg.ENABLE_IMAGE_TAGGING = False

    schedule_tag("冨里奈央", {"_local_file": "test.jpg", "type": "picture"})
    print("  ✅ 功能关闭时无动作（无异常）")


def test_search_matches_tags(monkeypatch=None) -> None:
    """search() 应匹配 _tags 字段"""
    print("=== search() 匹配标签 ===")
    from src.archive import search
    import src.archive as arch

    setup_test()

    # 保存原始函数
    orig_list_months = arch.list_months
    orig_load_month = arch.load_month

    def mock_list_months(member):
        return [{"year": 2026, "month": 7, "count": 1}]

    def mock_load_month(member, year, month):
        return [{
            "id": 1,
            "type": "picture",
            "text": "今日はいい天気",
            "_translation": "今天天气真好",
            "_tags": "笑容 自拍 海边",
        }]

    arch.list_months = mock_list_months
    arch.load_month = mock_load_month

    try:
        hits = search("冨里奈央", "笑容")
        assert len(hits) == 1, f"搜索'笑容'应返回 1 条，实际 {len(hits)}"

        hits = search("冨里奈央", "海边")
        assert len(hits) == 1, f"搜索'海边'应返回 1 条，实际 {len(hits)}"

        hits = search("冨里奈央", "舞台")
        assert len(hits) == 0, f"搜索'舞台'应返回 0 条，实际 {len(hits)}"
        print("  ✅ 标签搜索全部正确")
    finally:
        arch.list_months = orig_list_months
        arch.load_month = orig_load_month


def main() -> None:
    test_tag_image_file_not_found()
    test_tag_image_disabled()
    test_schedule_tag_skips_non_picture()
    test_schedule_tag_skips_already_tagged()
    test_schedule_tag_skips_no_local_file()
    test_schedule_tag_disabled()
    test_search_matches_tags()
    print()
    print("✅ tagger 模块全部测试通过")


if __name__ == "__main__":
    main()
