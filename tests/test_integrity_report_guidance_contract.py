"""Contract tests for Phase U2: Actionable integrity report guidance and issue filtering."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "webui_static"


def test_integrity_guidance_and_filters_contract():
    js_text = (STATIC / "admin_tasks.js").read_text(encoding="utf-8")
    css_text = (STATIC / "theme.css").read_text(encoding="utf-8")

    # 1. 验证 U2 诊断与指引卡片的存在
    assert "admin-integrity-guidance-card" in js_text
    assert "admin-integrity-guidance-card" in css_text
    assert "📋 完整性异常诊断与处置建议" in js_text
    assert "原始月度消息 JSON 与本地媒体引用检查通过" in js_text
    assert "tools/migrate_archive_identity.py --dry-run" in js_text
    assert "tools/migrate_archive_identity.py --apply" in js_text

    # 2. 验证问题明细中的针对性说明与无损分类
    assert 'category = "missing"' in js_text
    assert 'category = "mismatch"' in js_text
    assert 'category = "no_json"' in js_text
    assert "多由历史跨成员同编号覆盖导致" in js_text
    assert "原归档月未检索到对应 JSON，可能为跨月漂移或孤立调试记录" in js_text

    # 3. 验证对 message_index_entry_without_json 禁止生成伪跳转链接
    assert 'code === "message_index_entry_without_json"' in js_text

    # 4. 验证交互式快捷筛选条
    assert "admin-integrity-filter-bar" in js_text
    assert "admin-integrity-filter-btn" in js_text
    assert "admin-integrity-filter-btn" in css_text
    assert "filterCategories" in js_text
