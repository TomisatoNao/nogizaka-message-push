"""管理端移动端布局契约。

这些断言不替代浏览器截图测试，而是锁住容易被回归改坏的结构性约束：
成对输入不能拆行、状态表必须在自己的容器内横向滚动、单个开关不能被拆开。
"""

from pathlib import Path
import re


_HTML = (
    Path(__file__).resolve().parents[1] / "src" / "webui_static" / "index.html"
).read_text(encoding="utf-8")


def test_compact_pairs_stay_single_line_on_mobile():
    """移动端覆盖规则必须让 min/max 与起/止输入保持一行。"""
    mobile_css = _HTML.split('@media (max-width: 640px)', 1)[1]
    assert re.search(r"\.pair\s*\{[^}]*flex-wrap:\s*nowrap", mobile_css)
    assert re.search(
        r"\.pair input\s*\{[^}]*flex:\s*1 1 0;[^}]*width:\s*auto;[^}]*min-width:\s*0",
        mobile_css,
    )
    assert re.search(r"\.pair\s*>\s*span\s*\{[^}]*white-space:\s*nowrap", mobile_css)


def test_status_table_has_horizontal_scroll_contract():
    """状态表关键列不能被窄屏压成逐字换行。"""
    assert '<table class="status-table">' in _HTML
    assert re.search(r"\.status-table\s*\{[^}]*min-width:\s*560px", _HTML)
    assert re.search(
        r"\.status-table th:nth-child\(2\).*?white-space:\s*nowrap",
        _HTML,
        re.DOTALL,
    )
    assert ".table-wrap { overflow-x: auto; }" in _HTML


def test_switch_group_keeps_each_toggle_atomic():
    """团体开关可在项目之间换行，但单个开关和标签不可拆分。"""
    assert 'class="switch-group"' in _HTML
    assert re.search(r"\.switch\s*\{[^}]*white-space:\s*nowrap", _HTML)
    assert re.search(r"\.switch-group \.switch\s*\{[^}]*flex:\s*0 0 auto", _HTML)
