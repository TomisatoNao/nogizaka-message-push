"""Static frontend contracts for the admin member route diagnostics panel."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "webui_static"


def test_route_diagnostics_panel_is_admin_gated_and_uses_one_script():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "admin_route_diagnostics.js").read_text(encoding="utf-8")

    script_url = "/static/admin_route_diagnostics.js?v=20261026_1"
    assert page.count(script_url) == 1
    assert page.count('/static/admin_route_diagnostics.css?v=20261026_1') == 1
    assert 'id="routeDiagnosticsPanel" hidden' in page
    assert 'window.webAdminRole === "admin"' in script
    assert 'window.addEventListener("webadminauthready", syncAuthorization' in script
    assert 'if (!authorized || requestInFlight) return;' in script
    assert 'version !== requestVersion' in script


def test_route_diagnostics_uses_read_only_admin_endpoint_and_explicit_states():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "admin_route_diagnostics.js").read_text(encoding="utf-8")

    assert script.count('"/api/route-diagnostics"') == 1
    assert 'method: "GET"' in script
    assert 'cache: "no-store"' in script
    for label in (
        "抓取账号关联", "订阅缓存", "未知 / 未同步", "不能据此判断为未订阅",
        "符合条件", "未满足", "已载入", "暂时无法加载", "重试加载诊断",
        "不代表已尝试推送或已送达",
    ):
        assert label in page + script
    assert 'id="btnRefreshRouteDiagnostics"' in page
    assert 'id="btnRetryRouteDiagnostics"' in page


def test_route_diagnostics_does_not_render_targets_bot_names_or_raw_config():
    script = (STATIC / "admin_route_diagnostics.js").read_text(encoding="utf-8")

    for private_field in (
        "route.label", "target_openid", "group_openid", "target_chat", "client_secret",
        "raw config", "raw_config", "napcat_api_token",
    ):
        assert private_field not in script
    assert "innerHTML" not in script
    assert "delivery_status" not in script
