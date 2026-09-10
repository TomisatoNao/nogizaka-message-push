"""订阅面板语义的回归测试。

订阅弹窗由静态 HTML/JS 渲染；这里锁定用户可见的关键语义，避免后续 UI
整理时又把“全部关闭”和“全量订阅”混为一谈，或丢失手动推送的边界说明。
"""

from pathlib import Path


HTML_PATH = Path(__file__).resolve().parents[1] / "src" / "webui_static" / "index.html"


def test_subscription_summary_has_a_distinct_paused_state():
    html = HTML_PATH.read_text(encoding="utf-8")
    assert 'class="sub-filter-tag paused">已暂停</span>' in html
    assert "enabledSubscriptions(item)" in html
    assert "未设置过滤（接收全部）" in html


def test_subscription_dialog_explains_empty_filters_and_direct_delivery():
    html = HTML_PATH.read_text(encoding="utf-8")
    assert "官方博客团体过滤（留空 = 全部团体）" in html
    assert "手动指定目标会直达所选通道，不读取该通道的订阅类型和白名单过滤规则" in html
    assert "测试推送直达目标，不读取该目标的订阅过滤规则" in html


def test_subscription_ui_defaults_match_backend_compatibility_defaults():
    """缺字段的旧配置继续使用兼容默认值，避免历史路由被静默改动。"""
    html = HTML_PATH.read_text(encoding="utf-8")
    definition_lines = {
        line.strip()
        for line in html.splitlines()
        if line.strip().startswith("{ key: \"push_")
    }
    for key, input_id, default in (
        ("push_message", "subMsg", "true"),
        ("push_blog", "subBlog", "false"),
        ("push_x", "subX", "true"),
        ("push_instagram", "subIg", "true"),
        ("push_tiktok", "subTiktok", "true"),
        ("push_live", "subLive", "true"),
        ("push_alert", "subAlert", "false"),
    ):
        assert any(
            f'{{ key: "{key}", inputId: "{input_id}"' in line
            and f"def: {default}" in line
            for line in definition_lines
        )


def test_new_channel_targets_start_with_incremental_zero_subscriptions():
    """新建的 QQ/NapCat/TG 目标显式关闭全部订阅，按需增量勾选。"""
    html = HTML_PATH.read_text(encoding="utf-8")

    defaults_start = html.index("const INCREMENTAL_SUBSCRIPTION_DEFAULTS")
    defaults_end = html.index("function createIncrementalSubscriptionDefaults", defaults_start)
    defaults = html[defaults_start:defaults_end]
    for key in (
        "push_message", "push_blog", "push_x", "push_instagram",
        "push_tiktok", "push_live", "push_alert",
    ):
        assert f"{key}: false" in defaults

    # QQ 官方 Bot、NapCat 路由、Telegram Bot 三个新增入口共用同一份
    # 显式关闭的默认对象；现有配置仍由兼容默认逻辑负责展示和运行。
    assert html.count("...createIncrementalSubscriptionDefaults()") == 3
    for add_id in ("btnAddQqBot", "btnAddNapcat", "btnAddTGBot"):
        assert add_id in html
