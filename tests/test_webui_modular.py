from pathlib import Path
import sys
import json
from io import BytesIO
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.webui_modules.archive import common as archive_common, home as archive_home  # noqa: E402
from src.webui_modules import (  # noqa: E402
    archive_handlers,
    auth_handlers,
    config_service,
    media_service,
    social_handlers,
    static_handler,
    system_handlers,
)


def test_static_handler_mime():
    assert "theme.css" in static_handler._STATIC_MIME
    assert static_handler._STATIC_MIME["theme.css"] == "text/css"


class _ResponseHandler:
    def __init__(self, accept_encoding: str = ""):
        self.headers = {"Accept-Encoding": accept_encoding}
        self.sent_headers: list[tuple[str, str]] = []
        self.wfile = BytesIO()
        self._pending_headers = []
        self._pending_set_cookies = []

    def send_response(self, _code):
        pass

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


def test_static_handler_respects_gzip_quality_and_consumes_pending_headers():
    handler = _ResponseHandler("gzip;q=0")
    data, headers = static_handler.compress_if_supported(handler, b"x" * 1024)
    assert data == b"x" * 1024
    assert headers == {}

    handler._pending_headers = [("Clear-Site-Data", '"cache"')]
    handler._pending_set_cookies = ["session=abc; HttpOnly"]
    static_handler.send_json(handler, {"ok": True})
    assert ("Vary", "Accept-Encoding") in handler.sent_headers
    assert ("Clear-Site-Data", '"cache"') in handler.sent_headers
    assert ("Set-Cookie", "session=abc; HttpOnly") in handler.sent_headers

    handler.sent_headers.clear()
    static_handler.send_json(handler, {"ok": True})
    assert not any(key in {"Clear-Site-Data", "Set-Cookie"} for key, _ in handler.sent_headers)


def test_static_handler_uses_long_cache_for_versioned_assets():
    handler = _ResponseHandler("gzip")
    handler.path = "/static/archive.js?v=20260901_2"
    static_handler.send_static(handler, "archive.js")
    headers = dict(handler.sent_headers)
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"

    handler.sent_headers.clear()
    handler.path = "/static/archive.js"
    static_handler.send_static(handler, "archive.js")
    headers = dict(handler.sent_headers)
    assert headers["Cache-Control"] == "public, max-age=120, must-revalidate"


def test_system_handlers_env():
    status = system_handlers.env_status()
    assert isinstance(status, dict)
    assert "GEMINI_API_KEY" in status
    assert "ZHIPU_API_KEY" in status


def test_member_handler_uses_current_directory_api(monkeypatch):
    """成员选择器必须调用当前 fetch_member_directory，而不是已删除的旧符号。"""
    import config.credentials as credentials
    from src import member_directory

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_fetch(client, account_id):
        assert isinstance(client, FakeClient)
        assert account_id == "demo"
        return ([
            {
                "id": "55",
                "name": "测试成员",
                "state": "open",
                "tags": ["乃木坂46"],
                "subscription": {
                    "state": "active", "type": "monthly",
                    "start_at": "2026-01-01", "end_at": "2026-12-31",
                    "auto_renewing": True,
                },
            },
            {"id": "56", "name": "离线成员", "state": "closed",
             "subscription": {"state": "expired", "type": "monthly"}},
        ], None)

    monkeypatch.setattr(credentials, "load_all_accounts", lambda: None)
    monkeypatch.setattr(credentials, "is_account_fetch_available", lambda _account: (True, ""))
    monkeypatch.setattr(credentials, "validate_account_cred", lambda _account: (True, ""))
    monkeypatch.setattr(system_handlers.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(member_directory, "fetch_member_directory", fake_fetch)

    handler = _ResponseHandler()
    handler.path = "/api/members?account=demo"
    system_handlers.handle_members(handler, lambda: {"accounts": {"demo": {"group": "nogizaka46"}}})
    payload = json.loads(handler.wfile.getvalue())

    assert payload["ok"]
    assert payload["total"] == 2
    assert payload["subscribed_count"] == 1
    assert payload["past_subscribed_count"] == 1
    assert payload["open_count"] == 1
    assert payload["members"][0]["is_subscribed"]
    assert payload["members"][1]["is_past_subscribed"]
    assert payload["group"] == "nogizaka46"


def test_member_handler_rejects_unsupported_account_group():
    """成员目录只能从四个官方 Message 团体账号拉取。"""
    handler = _ResponseHandler()
    handler.path = "/api/members?account=instagram_only"
    system_handlers.handle_members(
        handler,
        lambda: {"accounts": {"instagram_only": {"group": "other"}}},
    )
    payload = json.loads(handler.wfile.getvalue())

    assert payload["ok"] is False
    assert any("不支持成员目录拉取" in error for error in payload["errors"])


def test_test_push_normalizes_legacy_official_channel():
    """旧版前端的 official 仍应路由到统一的 qq_official 通道。"""
    calls = []

    def callback(channel, target, text):
        calls.append((channel, target, text))
        return True, ""

    handler = _ResponseHandler()
    system_handlers.handle_test_push(
        handler,
        {"channel": "official", "target": "bot1|private", "text": "hello"},
        callback,
    )
    payload = json.loads(handler.wfile.getvalue())

    assert payload["ok"]
    assert calls == [("qq_official", "bot1|private", "hello")]


def test_admin_frontend_config_and_dark_select_contracts():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")
    assert "const configLabel =" in html
    assert 'msg("已载入 " + configLabel, "ok")' in html
    assert 'opt.style.color = "#000"' not in html
    assert 'channel === "qq_official" || channel === "official"' in html
    assert 'enabled.push(["qq_official", "QQ 官方 Bot"])' in html
    assert "color-scheme: dark" in html
    # 完整 Web 会话必须从 signin 登录响应提取；后续 timeline/profile 请求没有 Set-Cookie。
    assert "右键 signin 请求" in html
    assert "任意 timeline 请求" not in html
    assert "同一个 timeline 请求的 Request Headers" not in html
    assert "必须包含 URL 与请求体" in html
    assert "Request Headers 文本" not in html
    # 动态监控页集中展示全局时段、Message 与其它项目；系统页的运行参数按组展示。
    assert "全局监控调度" in html
    assert "Message 监控设置" in html
    assert "monitor_schedule" in html
    assert "🚦 发送与告警参数" in html
    assert "不控制抓取频率" in html
    assert "日间间隔（min / max 秒）" in html
    assert "深夜间隔（min / max 秒）" in html
    assert "休眠时段暂停所有内容监控" in html
    assert "socialIgIntervalMax" in html
    assert "socialIgNightIntervalMax" in html


def test_monitor_schedule_frontend_contract_and_responsive_project_containers():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")
    for element_id in (
        "scheduleDayStart", "scheduleNightStart", "sleepStart", "sleepEnd",
        "schedulePause", "scheduleState", "msgMonitorOn", "dayMin", "dayMax",
        "nightMin", "nightMax",
        "blogMonitorOn", "blogHinatazaka", "blogNogizaka", "blogSakurazaka",
        "blogDayMin", "blogDayMax", "blogNightMin", "blogNightMax",
        "socialXOn", "socialXInterval", "socialXNightInterval", "socialXAccounts",
        "socialIgOn", "socialIgFeed", "socialIgStories", "socialIgInterval",
        "socialIgIntervalMax", "socialIgNightInterval", "socialIgNightIntervalMax",
        "socialIgAccounts", "igSessionStatus", "igSessionDetail", "btnFillIgSession",
        "btnCheckIgSession", "btnClearIgSession", "socialTiktokOn", "socialTiktokInterval",
        "socialTiktokAccounts", "socialLiveOn", "socialLiveInterval", "socialLiveAccounts",
    ):
        assert html.count(f'id="{element_id}"') == 1
    assert "function updateScheduleState()" in html
    assert "config.monitor_schedule =" in html
    assert "monitor-project" in html
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in html
    assert ".monitor-project-heading h3 { flex: 1 1 auto; min-width: 0;" in html
    assert ".monitor-project-heading > .switch { flex: 0 0 auto; width: auto;" in html
    assert ".monitor-control-group .switch { flex: 0 0 auto; width: auto;" in html
    assert ".monitor-frequency-group { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in html
    assert ".monitor-field-pair input { flex: 0 1 90px; width: 90px;" in html
    # 动态监控采用摘要卡 + 独立详情弹窗；平台表单只保留在对应弹窗内。
    assert '<div class="monitor-summary-grid"' in html
    assert html.count('class="card admin-module monitor-summary-card') == 6
    for summary_id in ("message", "blog", "x", "instagram", "tiktok", "tiktok-live"):
        assert f'data-monitor-summary-card="{summary_id}"' in html
    for dialog_id in (
        "monitorMessageDialog", "monitorBlogDialog", "monitorXDialog",
        "monitorInstagramDialog", "monitorTiktokDialog", "monitorLiveDialog",
    ):
        assert html.count(f'id="{dialog_id}"') == 1
        assert f'aria-labelledby="{dialog_id}Title"' in html
    assert html.count('class="monitor-settings-dialog"') == 6
    assert html.count('data-monitor-dialog-close') >= 10
    assert 'data-monitor-dialog-target="monitorMessageDialog"' in html
    assert 'data-monitor-dialog-target="monitorBlogDialog"' in html
    assert 'data-monitor-dialog-target="monitorXDialog"' in html
    assert 'data-monitor-dialog-target="monitorInstagramDialog"' in html
    assert html.count('data-monitor-dialog-target="monitorTiktokDialog"') == 1
    assert html.count('data-monitor-dialog-target="monitorLiveDialog"') == 1
    assert ".monitor-summary-grid { display: grid; grid-template-columns: repeat(3" in html
    assert ".monitor-settings-dialog {" in html
    assert ".monitor-dialog-scroll" in html
    assert ".schedule-card .schedule-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in html
    assert ".admin-schedule-group" in html
    assert ".admin-schedule-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in html
    assert "轮询运行时段" in html and "休眠窗口" in html
    assert "深夜轮询区间为“深夜开始 → 休眠开始”" in html
    assert "0 表示午夜 00:00" in html
    assert "end && n === 0 ? \"24:00\"" in html
    assert 'class="monitor-project monitor-project--full"' in html
    assert 'class="monitor-control-group" role="group" aria-label="博客团体开关"' in html
    assert 'class="monitor-frequency-group" role="group" aria-label="博客轮询频率"' in html
    assert 'class="monitor-frequency-group" role="group" aria-label="Instagram 轮询频率"' in html
    assert 'class="monitor-account-group"' in html
    assert 'class="monitor-credential-group"' in html
    assert "settings-field-grid" in html
    assert "settings-control-group" in html
    assert ".monitor-summary-heading" in html
    assert ".monitor-summary-title { flex: 1 1 auto; min-width: 0;" in html
    assert ".monitor-summary-settings { flex: 0 0 auto;" in html
    assert ".monitor-summary-status-row" in html
    assert "function renderMonitorSummaries()" in html
    assert "function bindMonitorSettingsDialogs()" in html
    assert "dialog.showModal()" in html
    assert "dialog.addEventListener(\"close\"" in html
    # 桌面三列、平板两列、手机单列；具体断点也必须显式存在，避免依赖自动换行。
    assert "@media (max-width: 900px) and (min-width: 641px)" in html
    assert "@media (max-width: 640px)" in html
    assert ".monitor-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr));" in html
    assert ".monitor-summary-grid { grid-template-columns: 1fr;" in html
    main_start = html.index("<main>")
    main_end = html.index("</main>", main_start)
    for dialog_id in (
        "monitorMessageDialog", "monitorBlogDialog", "monitorXDialog",
        "monitorInstagramDialog", "monitorTiktokDialog", "monitorLiveDialog",
    ):
        assert main_start < html.index(f'id="{dialog_id}"') < main_end
    dialog_fields = {
        "monitorMessageDialog": ("dayMin", "dayMax", "nightMin", "nightMax"),
        "monitorBlogDialog": (
            "blogHinatazaka", "blogNogizaka", "blogSakurazaka",
            "blogDayMin", "blogDayMax", "blogNightMin", "blogNightMax",
        ),
        "monitorXDialog": ("socialXInterval", "socialXNightInterval", "socialXAccounts"),
        "monitorInstagramDialog": (
            "socialIgFeed", "socialIgStories", "socialIgInterval", "socialIgIntervalMax",
            "socialIgNightInterval", "socialIgNightIntervalMax", "socialIgAccounts",
            "igSessionStatus", "igSessionDetail", "btnFillIgSession", "btnCheckIgSession", "btnClearIgSession",
        ),
        "monitorTiktokDialog": ("socialTiktokInterval", "socialTiktokAccounts"),
        "monitorLiveDialog": ("socialLiveInterval", "socialLiveAccounts"),
    }
    for dialog_id, field_ids in dialog_fields.items():
        start = html.index(f'id="{dialog_id}"')
        dialog = html[start:html.index("</dialog>", start)]
        assert all(f'id="{field_id}"' in dialog for field_id in field_ids)
    tiktok_dialog = html[html.index('id="monitorTiktokDialog"'):html.index("</dialog>", html.index('id="monitorTiktokDialog"'))]
    live_dialog = html[html.index('id="monitorLiveDialog"'):html.index("</dialog>", html.index('id="monitorLiveDialog"'))]
    assert 'id="socialLiveInterval"' not in tiktok_dialog
    assert 'id="socialLiveAccounts"' not in tiktok_dialog
    assert 'id="socialTiktokInterval"' not in live_dialog
    assert 'id="socialTiktokAccounts"' not in live_dialog
    # TikTok / Live 的补充账号必须留在各自项目容器内，避免项目边界不清。
    for account_id in ("socialTiktokAccounts", "socialLiveAccounts"):
        project_start = html.rfind('class="monitor-project', 0, html.index(f'id="{account_id}"'))
        assert project_start >= 0
        assert html.index(f'id="{account_id}"') < html.index('</div>\n      </div>', project_start)
    assert "告警重复通知冷却" in html
    assert "QQ / NapCat 发送节流间隔" in html
    assert "NapCat 单路发送超时" in html


def test_admin_frontend_validation_contract():
    """管理页保存前应严格校验数字、区间、URL 和动态表格字段。"""
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    assert "const ADMIN_NUMBER_RULES = Object.freeze({" in html
    assert "function strictAdminNumber(" in html
    assert "function validateAdminConfig()" in html
    assert "function validateAdminSocialUrl(" in html
    assert "function validateAdminSocialAccounts(" in html
    assert "focusAdminValidationError();" in html
    assert 'const errors = validateAdminConfig();' in html
    assert "parseInt($(\"socialXInterval\").value" not in html
    assert "parseInt($(\"socialIgInterval\").value" not in html
    assert "保留非法原文，保存前校验才能阻止" in html
    assert "input.invalid, select.invalid, textarea.invalid" in html
    assert "const parsed = new URL(value);" in html
    assert "代理地址必须是有效的 HTTP、HTTPS 或 SOCKS5 链接" in html
    assert "API Base 必须是有效的 http(s) URL" in html
    assert "QQ Bot 名称只能使用小写字母、数字和下划线" in html
    assert "Telegram Bot 名称只能使用字母、数字、点、下划线或连字符" in html
    assert "用户名只能使用字母、数字、下划线和连字符" in html


def test_admin_modules_share_grouped_responsive_layout_contract():
    """状态、账号/成员和推送通道统一使用模块/控制/数据/操作层级。"""
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    for section_id in ("tab-status", "tab-monitors", "tab-channels"):
        start = html.index(f'id="{section_id}"')
        end = html.index("</section>", start)
        section = html[start:end]
        assert 'class="card admin-module"' in section
        assert "admin-module-heading" in section
        assert "admin-data-group" in section
        assert "admin-actions" in section
        # 目标管理页的静态结构不再用 style= 负责布局，避免和动态监控的组件规则分叉。
        assert "style=" not in section

    for element_id in (
        "stStartup", "stCycle", "stNext", "pollFeedback", "stTokens", "stChannels",
        "stNapcatHealth", "stDeliveryBacklog", "btnCopyDeliveryDiag", "stDiskTotal",
        "stDiskFree", "stDiskPercent", "stDiskBar", "stAppTotal", "stStorageGrid", "stErrors",
        "accountRows", "accountEmpty", "btnAddAccount", "memberRows", "memberEmpty",
        "btnPickMember", "btnSyncSubs", "pickGroupChips", "pickAccountChips", "channelSwitches", "qqBotRows",
        "btnAddQqBot", "qqBotHint", "cmdOn", "cmdOptionsBlock", "cmdMode", "cmdWhitelistWrap",
        "cmdWhitelistCountBadge", "btnSyncBotOpenids", "btnAddCmdOpenid", "cmdModeHintBanner",
        "cmdOpenidList", "napcatApi", "napcatApiToken", "napcatMediaBaseUrl", "napcatRows", "btnAddNapcat",
        "tgTokenMigrationNotice", "tgBotRows", "btnAddTGBot",
    ):
        assert html.count(f'id="{element_id}"') == 1

    for selector in (
        ".admin-module", ".admin-module-heading", ".admin-control-group",
        ".admin-data-group", ".admin-actions", ".admin-field-grid",
        ".admin-summary-grid", ".admin-status-badge", ".admin-channel-switches",
        ".admin-member-account", ".admin-member-readonly", ".admin-member-subscription-cell", ".admin-storage-grid",
        ".admin-storage-card-actions", ".admin-row-actions",
    ):
        assert selector in html
    assert ".admin-field-grid { display: grid; grid-template-columns: repeat(3" in html
    assert ".admin-field-grid { grid-template-columns: repeat(2" in html
    assert ".admin-field-grid," in html and "grid-template-columns: 1fr" in html
    assert ".admin-module-heading > .status-actions" in html
    assert ".table-wrap { overflow-x: auto; }" in html
    assert "function mkTableInput(" in html
    assert "className = \"admin-table-input\"" in html


def test_napcat_endpoint_fields_share_aligned_responsive_group():
    """NapCat 基地址、Token 和媒体地址使用紧凑的响应式端点组。"""
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    assert 'class="napcat-endpoint-grid"' in html
    assert "napcat-endpoint-note" in html
    assert 'class="field">NapCat API 地址' in html
    assert 'id="napcatApiToken"' in html
    assert 'class="field">媒体访问基地址（可选）' in html
    assert "NapCat 不在本机时填写媒体地址；共享文件系统可留空。" in html
    assert "跨容器或跨主机时必须填写 NapCat 可访问的媒体地址" not in html
    assert ".napcat-endpoint-grid { display: grid; grid-template-columns: repeat(3" in html
    assert ".napcat-endpoint-grid { grid-template-columns: repeat(2" in html
    assert ".napcat-endpoint-grid { grid-template-columns: 1fr; gap: 10px; }" in html


def test_table_action_cells_keep_table_column_alignment():
    """表格操作单元格不能继承对话框 .actions 的 flex 布局。"""
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    assert ".admin-table-actions-cell { display: table-cell;" in html
    assert 'class="admin-history-table"' in html
    assert 'td.className = "admin-table-actions-cell admin-nowrap"' in html
    assert 'tdAct.className = "admin-table-actions-cell"' in html
    assert 'td.className = "actions admin-nowrap"' not in html
    assert 'tdAct.className = "actions"' not in html


def test_admin_stage6_user_system_advanced_layout_contract():
    """用户、系统、高级和社媒工具页共享模块层级且静态布局不依赖内联样式。"""
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    assert 'class="admin-summary-grid admin-summary-grid--status"' in html
    assert 'class="admin-summary-value admin-summary-state admin-status-badge muted"' in html
    assert 'startupBox.className = "admin-summary-value admin-summary-state admin-status-badge " + tone;' in html

    for section_id in ("tab-social", "tab-system", "tab-users", "tab-advanced"):
        start = html.index(f'id="{section_id}"')
        end = html.index("</section>", start)
        section = html[start:end]
        assert 'class="card admin-module' in section
        assert "style=" not in section

    for selector in (
        ".admin-user-access", ".admin-user-table", ".admin-log-toolbar",
        ".admin-editor-group", ".admin-social-tool-input", ".admin-social-targets",
        ".admin-settings-group", ".admin-settings-title",
    ):
        assert selector in html

    for element_id in (
        "authArchivePublic", "userRows", "btnAddUser", "logSourceChips", "logErrOnly",
        "logKeyword", "logFollow", "rawJson", "btnApplyJson", "btnResetJson",
        "historyRows", "historyEmpty", "toolSocialUrl", "toolSocialChannelList",
    ):
        assert html.count(f'id="{element_id}"') == 1

    assert ".admin-summary-grid--status { grid-template-columns: minmax(148px, max-content)" in html
    assert ".admin-summary-grid--status { grid-template-columns: minmax(148px, max-content) minmax(0, 1fr); }" in html
    assert ".admin-summary-grid--status { grid-template-columns: 1fr; }" in html
    assert ".admin-user-table { min-width: 620px; }" in html
    assert ".admin-log-keyword { width: min(220px, 100%);" in html


def test_system_handlers_smart_parse():
    import asyncio
    raw_curl = "curl 'https://api.message.nogizaka46.com/v1/messages' -H 'authorization: Bearer my_jwt_token_12345678901234567890'"
    res = asyncio.run(system_handlers.smart_parse_credentials_text(raw_curl))
    assert res["token"] == "my_jwt_token_12345678901234567890"


def test_auth_handlers_loopback():
    assert "127.0.0.1" in auth_handlers.LOOPBACK_HOSTS
    assert "localhost" in auth_handlers.LOOPBACK_HOSTS


def test_auth_cookie_secure_configuration():
    original = auth_handlers.cfg.AUTH_COOKIE_SECURE
    try:
        auth_handlers.cfg.AUTH_COOKIE_SECURE = True
        assert auth_handlers._cookie("session", "abc", 60).endswith("Secure")
    finally:
        auth_handlers.cfg.AUTH_COOKIE_SECURE = original


def test_auth_origin_auto_detects_reverse_proxy_without_manual_origin():
    class Handler(_ResponseHandler):
        def __init__(self, headers):
            super().__init__()
            self.headers = headers

    original = auth_handlers.cfg.WEB_ADMIN_ORIGIN
    try:
        auth_handlers.cfg.WEB_ADMIN_ORIGIN = ""
        # 代理保留 Host 但未传协议头时，HTTPS Origin 也应能正常登录。
        assert auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "Origin": "https://push.example.com",
        }))
        # 标准代理头存在时仍严格校验协议，避免 HTTPS/HTTP 混淆。
        assert auth_handlers.check_origin(Handler({
            "Host": "internal:46046",
            "X-Forwarded-Host": "push.example.com",
            "X-Forwarded-Proto": "https",
            "Origin": "https://push.example.com",
        }))
        assert not auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "X-Forwarded-Proto": "http",
            "Origin": "https://push.example.com",
        }))
        assert not auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "Origin": "https://evil.example.com",
        }))
        assert not auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "Origin": "https://push.example.com:8443",
        }))

        # 配置了固定 Origin 时，自动模式不应放宽该约束。
        auth_handlers.cfg.WEB_ADMIN_ORIGIN = "https://admin.example.com"
        assert not auth_handlers.check_origin(Handler({
            "Host": "push.example.com",
            "Origin": "https://push.example.com",
        }))
    finally:
        auth_handlers.cfg.WEB_ADMIN_ORIGIN = original


def test_archive_handlers_and_media_service():
    assert archive_handlers.ARCHIVE_TYPES == frozenset({"text", "picture", "image", "video", "voice"})
    assert callable(media_service.serve_file_range)
    assert callable(config_service.validate_config)


def test_message_media_totals_do_not_fall_back_to_total_messages():
    members = [
        {"stats": {"total": 10, "pictures": 3, "videos": 2, "voices": 1}},
        {"stats": {"total": 7, "pictures": 0, "videos": 1, "voices": 0}},
    ]
    assert archive_handlers._message_media_totals(members) == {
        "pictures": 3,
        "videos": 3,
        "voices": 1,
        "total": 7,
    }


def test_blog_calendar_endpoint_filters_group_author_and_invalid_dates(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE blog_posts (group_key TEXT, author TEXT, date TEXT)")
    db.executemany(
        "INSERT INTO blog_posts (group_key, author, date) VALUES (?, ?, ?)",
        [
            ("nogizaka", "冨里 奈央", "2026-08-01 10:00"),
            ("nogizaka", "冨里奈央", "2026-08-01 12:00"),
            ("nogizaka", "冨里 奈央", "2026-08-02 10:00"),
            ("nogizaka", "池田 瑛紗", "2026-08-01 10:00"),
            ("sakurazaka", "冨里 奈央", "2026-08-01 10:00"),
            ("nogizaka", "冨里 奈央", "not-a-date"),
        ],
    )
    db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)

    class Handler(_ResponseHandler):
        def __init__(self, path):
            super().__init__()
            self.path = path
            self.command = "GET"
            self.payload = None
            self.code = None

        def _send_json(self, payload, code=200):
            self.payload = payload
            self.code = code

    groups = Handler("/api/archive/blog_groups")
    archive_handlers.handle_archive(groups, "blog_groups", lambda **_: True, lambda: None)
    assert groups.code == 200
    assert [(item["key"], item["total"]) for item in groups.payload["groups"]] == [
        ("nogizaka", 5),
        ("sakurazaka", 1),
    ]

    all_posts = Handler("/api/archive/blog_calendar?group=nogizaka")
    archive_handlers.handle_archive(all_posts, "blog_calendar", lambda **_: True, lambda: None)
    assert all_posts.code == 200
    assert all_posts.payload["days"] == {"2026-08-01": 3, "2026-08-02": 1}
    assert all_posts.payload["total"] == 4
    assert all_posts.payload["first_date"] == "2026-08-01"
    assert all_posts.payload["last_date"] == "2026-08-02"

    author = Handler("/api/archive/blog_calendar?group=nogizaka&author=" + quote("冨里 奈央"))
    archive_handlers.handle_archive(author, "blog_calendar", lambda **_: True, lambda: None)
    assert author.code == 200
    assert author.payload["days"] == {"2026-08-01": 2, "2026-08-02": 1}
    assert author.payload["total"] == 3


def test_blog_list_endpoint_returns_summary_dto_without_full_body(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY, group_key TEXT, author TEXT, title TEXT, url TEXT,
            date TEXT, body_html TEXT, body_text TEXT, translation TEXT,
            content_json TEXT, translation_model TEXT, images_json TEXT,
            image_paths_json TEXT, raw_json TEXT
        )"""
    )
    db.execute(
        """INSERT INTO blog_posts
           (id, group_key, author, title, url, date, body_html, body_text,
            translation, content_json, translation_model, images_json, image_paths_json, raw_json)
           VALUES (1, 'nogizaka', '冨里 奈央', '夏のブログ', 'https://example.test/1',
                   '2026-08-01 10:00', '<p>正文</p>', ?, '译文', '[]', '', ?, ?, '{}')""",
        ("正文 " * 400, '["https://cdn.test/cover.jpg"]', '["nogizaka/1/cover.jpg"]'),
    )
    db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)

    class Handler(_ResponseHandler):
        def __init__(self, path):
            super().__init__()
            self.path = path
            self.command = "GET"
            self.payload = None
            self.code = None

        def _send_json(self, payload, code=200):
            self.payload = payload
            self.code = code

    handler = Handler("/api/archive/blogs?group=nogizaka&page=1&per_page=24")
    archive_handlers.handle_archive(handler, "blogs", lambda **_: True, lambda: None)

    assert handler.code == 200
    post = handler.payload["posts"][0]
    assert post["id"] == 1
    assert post["cover"] == "/api/archive/blog_media/nogizaka/1/cover.jpg"
    assert post["cover_original"] == "https://cdn.test/cover.jpg"
    assert post["has_translation"] is True
    assert len(post["excerpt"]) <= 261
    assert "body_html" not in post
    assert "raw_json" not in post
    assert "images_json" not in post

    detail = Handler("/api/archive/blogs?id=1")
    archive_handlers.handle_archive(detail, "blogs", lambda **_: True, lambda: None)
    assert detail.code == 200
    assert detail.payload["post"]["body_html"] == "<p>正文</p>"


def test_blog_search_multi_term_and_escaping_and_case_insensitive(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY, group_key TEXT, author TEXT, title TEXT, url TEXT,
            date TEXT, body_html TEXT, body_text TEXT, translation TEXT,
            content_json TEXT, translation_model TEXT, images_json TEXT,
            image_paths_json TEXT, raw_json TEXT
        )"""
    )
    db.executemany(
        """INSERT INTO blog_posts
           (id, group_key, author, title, url, date, body_html, body_text,
            translation, content_json, translation_model, images_json, image_paths_json, raw_json)
           VALUES (?, 'nogizaka', '冨里 奈央', ?, 'https://example.test',
                   ?, '<p>body</p>', ?, ?, '[]', '', '[]', '[]', '{}')""",
        [
            (1, "アンダーライブ リハーサル", "2026-08-01 10:00", "今日はUNDER LIVEの通しリハーサルでした！楽しかった！", "今天是Live总彩排！"),
            (2, "日常の100%満足な日", "2026-08-02 10:00", "100%の力を出し切ったよ_特別な一日", "发挥了100%的实力_特别的一天"),
            (3, "普通のタイトル", "2026-08-03 10:00", "何気ない日常の文章です。", "平淡日常"),
        ],
    )
    db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)

    class Handler(_ResponseHandler):
        def __init__(self, path):
            super().__init__()
            self.path = path
            self.command = "GET"
            self.payload = None
            self.code = None

        def _send_json(self, payload, code=200):
            self.payload = payload
            self.code = code

    # 1. 多词分词 AND 语义匹配（不同语序，同时命中标题与正文）
    multi_term = Handler("/api/archive/blogs?group=nogizaka&q=" + quote("リハーサル アンダー"))
    archive_handlers.handle_archive(multi_term, "blogs", lambda **_: True, lambda: None)
    assert multi_term.code == 200
    assert multi_term.payload["total"] == 1
    assert multi_term.payload["posts"][0]["id"] == 1

    # 2. 大小写不敏感测试
    case_insensitive = Handler("/api/archive/blogs?group=nogizaka&q=live")
    archive_handlers.handle_archive(case_insensitive, "blogs", lambda **_: True, lambda: None)
    assert case_insensitive.code == 200
    assert case_insensitive.payload["total"] == 1
    assert case_insensitive.payload["posts"][0]["id"] == 1

    # 3. SQL 通配符安全转义（搜索 % 不穿透匹配全量，搜索 _ 不匹配单字符）
    percent_search = Handler("/api/archive/blogs?group=nogizaka&q=" + quote("100%"))
    archive_handlers.handle_archive(percent_search, "blogs", lambda **_: True, lambda: None)
    assert percent_search.code == 200
    assert percent_search.payload["total"] == 1
    assert percent_search.payload["posts"][0]["id"] == 2

    underscore_search = Handler("/api/archive/blogs?group=nogizaka&q=" + quote("切ったよ_特別"))
    archive_handlers.handle_archive(underscore_search, "blogs", lambda **_: True, lambda: None)
    assert underscore_search.code == 200
    assert underscore_search.payload["total"] == 1
    assert underscore_search.payload["posts"][0]["id"] == 2

    # 搜索单个下划线只匹配包含字面值下划线的文章，不应匹配所有文章
    single_underscore = Handler("/api/archive/blogs?group=nogizaka&q=_")
    archive_handlers.handle_archive(single_underscore, "blogs", lambda **_: True, lambda: None)
    assert single_underscore.code == 200
    assert single_underscore.payload["total"] == 1
    assert single_underscore.payload["posts"][0]["id"] == 2

    # 4. 超长关键词校验（> 100 字符直接返回 400）
    too_long = Handler("/api/archive/blogs?group=nogizaka&q=" + quote("a" * 101))
    archive_handlers.handle_archive(too_long, "blogs", lambda **_: True, lambda: None)
    assert too_long.code == 400
    assert "超过 100 个字符" in too_long.payload["errors"][0]


def test_blog_calendar_search_query_filter(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY, group_key TEXT, author TEXT, title TEXT,
            date TEXT, body_text TEXT, translation TEXT
        )"""
    )
    db.executemany(
        """INSERT INTO blog_posts (id, group_key, author, title, date, body_text, translation)
           VALUES (?, 'nogizaka', '冨里 奈央', ?, ?, ?, ?)""",
        [
            (1, "アンダーライブ", "2026-08-01 10:00", "リハーサル開始", "开始彩排"),
            (2, "アンダーライブ", "2026-08-01 14:00", "本番初日", "初日演出"),
            (3, "普通の日常", "2026-08-02 10:00", "美味しいケーキ", "美味蛋糕"),
        ],
    )
    db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)

    class Handler(_ResponseHandler):
        def __init__(self, path):
            super().__init__()
            self.path = path
            self.command = "GET"
            self.payload = None
            self.code = None

        def _send_json(self, payload, code=200):
            self.payload = payload
            self.code = code

    # 不带 q 时：统计全量有效天数
    cal_all = Handler("/api/archive/blog_calendar?group=nogizaka")
    archive_handlers.handle_archive(cal_all, "blog_calendar", lambda **_: True, lambda: None)
    assert cal_all.code == 200
    assert cal_all.payload["days"] == {"2026-08-01": 2, "2026-08-02": 1}
    assert cal_all.payload["total"] == 3

    # 带 q 时：仅统计命中关键词的文章与日期
    cal_query = Handler("/api/archive/blog_calendar?group=nogizaka&q=" + quote("アンダー ライブ"))
    archive_handlers.handle_archive(cal_query, "blog_calendar", lambda **_: True, lambda: None)
    assert cal_query.code == 200
    assert cal_query.payload["days"] == {"2026-08-01": 2}
    assert cal_query.payload["total"] == 2
    assert cal_query.payload["first_date"] == "2026-08-01"
    assert cal_query.payload["last_date"] == "2026-08-01"
    assert cal_query.payload["query"] == "アンダー ライブ"

    # 超长关键词 400 校验
    cal_too_long = Handler("/api/archive/blog_calendar?group=nogizaka&q=" + quote("x" * 105))
    archive_handlers.handle_archive(cal_too_long, "blog_calendar", lambda **_: True, lambda: None)
    assert cal_too_long.code == 400
    assert "超过 100 个字符" in cal_too_long.payload["errors"][0]


def test_blog_delete_translation_endpoint_clears_translation(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        """CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY, author TEXT, title TEXT,
            translation TEXT, content_json TEXT, translation_model TEXT
        )"""
    )
    db.execute(
        """INSERT INTO blog_posts
           (id, author, title, translation, content_json, translation_model)
           VALUES (1, '冨里 奈央', '夏のブログ', '译文', '[{"zh":"翻译"}]', 'gemini-test')"""
    )
    db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "record_event", lambda *args, **kwargs: None)

    class Handler(_ResponseHandler):
        def __init__(self, command="POST"):
            super().__init__()
            self.command = command
            self.path = "/api/archive/blogs/delete_translation"
            self.payload = None
            self.code = None

        def _send_json(self, payload, code=200):
            self.payload = payload
            self.code = code

    handler = Handler()
    archive_handlers.handle_archive(
        handler,
        "blogs/delete_translation",
        lambda **kwargs: kwargs.get("need_admin") is True,
        lambda: {"id": 1},
    )
    assert handler.code == 200
    assert handler.payload == {"ok": True, "id": 1, "msg": "已清除该博客的翻译"}
    row = db.execute(
        "SELECT translation, content_json, translation_model FROM blog_posts WHERE id=1"
    ).fetchone()
    assert row == (None, None, None)

    missing = Handler()
    archive_handlers.handle_archive(
        missing,
        "blogs/delete_translation",
        lambda **_: True,
        lambda: {"id": 999},
    )
    assert missing.code == 404
    assert missing.payload["msg"] == "未找到该博客"

    invalid = Handler()
    archive_handlers.handle_archive(
        invalid,
        "blogs/delete_translation",
        lambda **_: True,
        lambda: {"id": "not-a-number"},
    )
    assert invalid.code == 400
    assert "正整数" in invalid.payload["msg"]


def test_blog_translate_endpoint_persists_structured_translation(monkeypatch):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY, group_key TEXT, author TEXT, title TEXT,
            body_html TEXT, translation TEXT, content_json TEXT, translation_model TEXT
        )"""
    )
    db.execute(
        """INSERT INTO blog_posts
           (id, group_key, author, title, body_html, translation, content_json, translation_model)
           VALUES (1, 'nogizaka', '冨里 奈央', '夏のブログ', '<p>こんにちは</p>', NULL, NULL, NULL)"""
    )
    db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)

    async def fake_translate(*_args, **_kwargs):
        return ([{"type": "text", "jp": "こんにちは", "zh": "你好"}], "test-model")

    from src import translator
    monkeypatch.setattr(translator, "translate_blog_structured", fake_translate)

    class Handler(_ResponseHandler):
        command = "POST"
        path = "/api/archive/blogs/translate"

        def __init__(self):
            super().__init__()
            self.payload = None
            self.code = None

        def _send_json(self, payload, code=200):
            self.payload = payload
            self.code = code

    handler = Handler()
    archive_handlers.handle_archive(
        handler,
        "blogs/translate",
        lambda **_: True,
        lambda: {"id": 1},
    )

    assert handler.code == 200
    assert handler.payload["ok"] is True
    assert handler.payload["translation_model"] == "test-model"
    row = db.execute(
        "SELECT translation, content_json, translation_model FROM blog_posts WHERE id=1"
    ).fetchone()
    assert row[0] == "<em>こんにちは</em><br><span>你好</span>"
    assert '"zh": "你好"' in row[1]
    assert row[2] == "test-model"


def test_archive_home_cache_is_single_flight(monkeypatch):
    import threading

    cache_key = (1.0, 2.0, "2026-09-01")
    monkeypatch.setattr(archive_home, "_home_cache", None)
    monkeypatch.setattr(archive_home, "_home_cache_key", None)
    monkeypatch.setattr(archive_home, "_home_cache_building", False)
    monkeypatch.setattr(archive_home, "_home_cache_key_for_request", lambda: cache_key)

    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_impl(handler, _sub, _guard_fn, _read_body_json_fn):
        calls.append(handler)
        started.set()
        if release.wait(2):
            archive_home._home_cache = {"ok": True, "source": "builder"}
            archive_home._home_cache_key = cache_key
            handler._send_json(archive_home._home_cache)

    monkeypatch.setattr(archive_handlers, "_handle_archive_impl", fake_impl)

    class Handler:
        def __init__(self):
            self.payload = None

        def _send_json(self, payload, _code=200):
            self.payload = payload

    first, second = Handler(), Handler()
    t1 = threading.Thread(target=archive_handlers.handle_archive, args=(first, "home", None, None))
    t2 = threading.Thread(target=archive_handlers.handle_archive, args=(second, "home", None, None))
    t1.start()
    assert started.wait(1)
    t2.start()
    release.set()
    t1.join(2)
    t2.join(2)

    assert len(calls) == 1
    assert first.payload == second.payload == {"ok": True, "source": "builder"}
    assert archive_home._home_cache_building is False


def test_archive_home_latest_messages_use_one_window_query():
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE messages (id TEXT, member_dir TEXT, type TEXT, text TEXT, translation TEXT, published_at TEXT, updated_at TEXT)"
    )
    db.executemany(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("a-old", "a", "text", "old", "", "2026-08-01 10:00", "2026-08-01 10:00"),
            ("a-new", "a", "text", "new", "译", "2026-09-01 10:00", "2026-09-01 10:00"),
            ("a-mid", "a", "text", "mid", "", "2026-08-20 10:00", "2026-08-20 10:00"),
            ("b-new", "b", "text", "b-new", "", "2026-09-01 11:00", "2026-09-01 11:00"),
            ("ignored", "a", "picture", "image", "", "2026-09-01 12:00", "2026-09-01 12:00"),
        ],
    )
    rows = archive_handlers._load_latest_text_by_member(db, ["a", "b"], limit=2)

    assert [item["id"] for item in rows["a"]] == ["a-new", "a-mid"]
    assert [item["id"] for item in rows["b"]] == ["b-new"]


def test_archive_home_warmup_uses_noop_handler(monkeypatch):
    def fake_handle(handler, sub, _guard_fn, _read_body_json_fn):
        assert sub == "home"
        handler._send_json({"ok": True})

    monkeypatch.setattr(archive_handlers, "handle_archive", fake_handle)
    assert archive_handlers.warm_home_cache() is True


def test_archive_home_boot_starts_home_request_in_parallel():
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    assert "const homePromise = initialHome ? showHome() : null;" in script
    assert "await Promise.all([authPromise, membersPromise, homePromise].filter(Boolean));" in script
    assert "requestVersion !== _homeRequestVersion" in script
    assert "const renderSecondary = () => {" in script
    assert "const renderTertiary = () => {" in script
    assert "window.requestIdleCallback" in script


def test_archive_blog_route_and_request_guards_are_present():
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    assert "let blogPageVersion = 0" in script
    assert "if (version !== blogPageVersion || curMode !== \"blog\") return;" in script
    assert "function syncBlogHash(pageNum = page)" in script
    assert "const author = p.has(\"author\")" in script
    assert "blogReaderReturnHash" in script
    assert "message_media_total" in script
    assert 'switchMainTab("blog", true)' not in script


def test_archive_home_static_asset_version_bumped():
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    perf = (_ROOT / "tools" / "measure_archive_performance.py").read_text(encoding="utf-8")
    assert "/static/archive.js?v=20260911_5" in html
    assert "/static/archive.css?v=20260911_2" in html
    assert "/static/archive.js?v=20260911_5" in perf
    assert "/static/archive.css?v=20260911_2" in perf


def test_archive_favorite_filter_has_single_entry_point():
    """收藏筛选只保留消息工具栏入口，单条消息收藏按钮仍可用。"""
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    assert html.count('id="chipFav"') == 1
    assert 'id="menuMyFavorites"' not in html
    assert "menuMyFavorites" not in script
    assert '.fav-btn' in script
    assert "let isFavFilter = false" in script
    assert 'p.set("fav", "1")' in script
    assert 'if (isFavFilter) calUrl += "&favorite=1"' in script
    assert 'const favParam = isFavFilter ? "&favorite=1" : ""' in script
    assert "refreshFilteredView();" in script


def test_archive_search_calendar_uses_matching_days_and_locks_message_months():
    """搜索跨月显示，月份时间线锁定，日历仍可定位命中日期。"""
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    assert 'if (searchQuery) calUrl += "&q=" + encodeURIComponent(searchQuery);' in script
    assert "function syncMessageMonthNavigation()" in script
    assert "const locked = curMode === \"msg\" && Boolean(searchQuery);" in script
    assert "monthNav.hidden = false;" in script
    assert "if (select) select.disabled = locked;" in script
    assert "if (prev) prev.disabled = true;" in script
    assert "function syncSearchCalendarMonth()" in script
    assert 'const entryNoun = curMode === "blog" ? "篇博客" : (searchQuery ? "条匹配消息" : "条消息");' in script
    assert '"本月无匹配消息"' in script
    assert 'const keepMessageSearch = curMode === "msg" && Boolean(searchQuery);' in script
    assert "&& !keepMessageSearch" in script
    assert "if (curMode === \"msg\" && searchQuery) return;" in script
    assert script.count("loadCalendar();") >= 4


def test_archive_group_blog_backfill_contract():
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    assert 'id="blogGroupBackfillModal"' in html
    assert 'id="bgbNogizaka"' in html
    assert 'id="bgbSakurazaka"' in html
    assert 'id="bgbHinatazaka"' in html
    assert 'id="bgbSelectAll"' in html
    assert 'id="bgbAdvanced"' in html
    assert "function promptArchiveGroups()" in script
    assert 'fetch("/api/archive/blogs/archive_groups"' in script
    assert 'JSON.stringify({ groups })' in script
    assert "function promptArchiveMemberUrl()" in script
    assert "翻译请在博客页面按篇触发" in html


def test_archive_backfill_member_picker_and_blog_url_validation_contract():
    """回填成员只能通过已加载成员复选框选择，博客 URL 在前端先校验。"""
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    styles = (_ROOT / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")

    assert 'id="bmMemberOptions"' in html
    assert 'id="bmMemberSelectAll"' in html
    assert 'type="hidden" id="bmMemberInput"' in html
    assert 'id="bmMemberOptions" class="bm-member-options"' in html
    assert "function renderBackfillMemberOptions" in script
    assert "function selectedBackfillMembers" in script
    assert "selectedBackfillMembers().join(\", \")" in script
    assert "function isValidArchiveMemberBlogUrl" in script
    assert "inputType: \"url\"" in script
    assert "请输入三坂官方成员博客列表页链接" in script
    assert ".bm-member-options" in styles
    assert ".bm-member-option" in styles


def test_archive_backfill_picker_uses_current_monitor_members_only():
    """回填选择器必须消费监控成员字段，不能复用归档历史全量列表。"""
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    backend = (_ROOT / "src" / "webui_modules" / "archive" / "messages.py").read_text(encoding="utf-8")

    assert "从当前监控成员中选择目标" in html
    assert "let monitorMembers = []" in script
    assert "monitorMembers = Array.isArray(data.monitor_members)" in script
    assert "const available = (Array.isArray(monitorMembers) ? monitorMembers : [])" in script
    assert '"monitor_members": monitor_members' in backend
    assert "config.MONITOR_LIST" in backend
    assert "已归档成员" not in script[script.index("function renderBackfillMemberOptions"):script.index("function backfillMemberSelectionForOpen")]


def test_archive_members_payload_separates_monitor_list(monkeypatch):
    """归档 API 保留历史浏览列表，同时单独返回当前监控成员。"""
    from src.webui_modules.archive import messages

    monkeypatch.setattr(messages.cfg, "MONITOR_LIST", [
        {"m_name": "监控成员", "group_type": "nogizaka46"},
        {"m_name": "无归档成员", "group_type": "yodel"},
    ])
    monkeypatch.setattr(messages._archive, "list_members", lambda: ["监控成员", "历史成员"])
    monkeypatch.setattr(messages._archive, "get_letters_counts", lambda _names: {})
    monkeypatch.setattr(messages._archive, "list_months", lambda _name: [{"count": 2}])
    monkeypatch.setattr(messages._archive, "infer_member_group", lambda _name: "")

    import src.avatar_manager as avatar_manager
    monkeypatch.setattr(avatar_manager, "get_member_avatar_map", lambda: {})

    handler = _ResponseHandler()
    handler.path = "/api/archive/members"
    assert messages.handle_messages(handler, "members", lambda **_: True, lambda: {})
    payload = json.loads(handler.wfile.getvalue())

    assert {item["name"] for item in payload["members"]} == {"监控成员", "历史成员"}
    assert [item["name"] for item in payload["monitor_members"]] == ["监控成员", "无归档成员"]


def test_archive_message_month_footer_contract():
    """消息列表底部应提供与顶部一致的可用月份切换入口。"""
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    styles = (_ROOT / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")

    assert '<nav class="message-month-footer" id="messageMonthFooter"' in html
    assert 'id="prevMonthBottom"' in html
    assert 'id="nextMonthBottom"' in html
    assert 'id="messageMonthFooterCurrent"' in html
    assert 'aria-label="消息月份导航"' in html

    # 顶部/底部共用相邻月份计算，沿用已有 months 列表和 selectMonth 流程。
    assert "function currentMessageMonthIndex()" in script
    assert "function navigateAdjacentMonth(offset, { scrollToTop = false } = {})" in script
    assert "navigateAdjacentMonth(1);" in script
    assert "navigateAdjacentMonth(-1);" in script
    assert "navigateAdjacentMonth(1, { scrollToTop: true });" in script
    assert "navigateAdjacentMonth(-1, { scrollToTop: true });" in script
    assert 'window.scrollTo({ top: 0, behavior: "instant" });' in script
    assert 'curMode === "msg" && !!curMember && !searchQuery && monthIndex >= 0' in script
    assert 'footer.hidden = !visible;' in script
    assert '$("prevMonthBottom").disabled = monthIndex >= months.length - 1;' in script
    assert '$("nextMonthBottom").disabled = monthIndex <= 0;' in script
    assert '/api/archive/months?member=' in script
    assert "syncHash();" in script

    # 桌面和移动端都保留足够的触控空间，且导航不使用 fixed/sticky 覆盖内容。
    assert ".message-month-footer" in styles
    assert ".message-month-footer .nav" in styles
    assert ".message-month-current" in styles
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);" in styles
    assert ".message-month-footer .nav:first-child { justify-self: end; }" in styles
    assert ".message-month-footer .nav:last-child { justify-self: start; }" in styles
    assert "width: 92%;" in styles
    assert "margin: 24px 0 8px;" in styles
    assert "@media (max-width: 640px)" in styles
    assert "width: 132px;" in styles
    assert "width: 100%;" in styles[styles.rfind(".message-month-footer {"):]
    assert "margin: 20px 0 8px;" in styles
    assert "position: fixed" not in styles.split(".message-month-footer", 1)[1].split("}", 1)[0]


def test_archive_message_media_visibility_contract():
    """Message 媒体离开视野/切后台时必须暂停，且两条创建路径都接入观察器。"""
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    assert "ARCHIVE_MEDIA_VISIBILITY_THRESHOLD = 0.25" in script
    assert "new IntersectionObserver" in script
    assert "entry.intersectionRatio < ARCHIVE_MEDIA_VISIBILITY_THRESHOLD" in script
    assert "function pauseAllArchiveMedia()" in script
    assert "archiveMediaElements().forEach(pauseArchiveMedia);" in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert 'document.hidden || document.visibilityState !== "visible"' in script
    assert 'window.addEventListener("pagehide", pauseAllArchiveMedia)' in script

    # 不支持 IntersectionObserver 时，滚动/resize 仅通过 requestAnimationFrame 节流。
    assert "function bindArchiveMediaFallback()" in script
    assert 'window.addEventListener("scroll", scheduleArchiveMediaVisibilityCheck' in script
    assert 'window.addEventListener("resize", scheduleArchiveMediaVisibilityCheck' in script
    assert "window.requestAnimationFrame" in script
    assert "return window.setTimeout(callback, 0);" in script

    # 初次渲染与重试下载后的动态媒体都必须注册；回收旧卡片时解除观察。
    assert 'b.querySelectorAll("video, audio").forEach(observeArchiveMedia);' in script
    assert "missDiv.replaceWith(mediaEl);" in script
    assert "observeArchiveMedia(mediaEl);" in script
    assert "clearArchiveMediaObservers($(\"timeline\"));" in script
    assert "if (observedArchiveMedia.has(media)) return;" in script

    # 可见性回调只 pause，不调用 play，返回视野后保持暂停。
    observer_block = script.split("archiveMediaObserver = new IntersectionObserver", 1)[1].split("});", 1)[0]
    assert "pauseArchiveMedia(entry.target)" in observer_block
    assert ".play()" not in observer_block


def test_archive_home_omits_duplicate_history_section():
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    styles = (_ROOT / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")
    handler = (_ROOT / "src" / "webui_modules" / "archive_handlers.py").read_text(encoding="utf-8")

    for content in (html, script, styles):
        assert "homeTimeTunnel" not in content
        assert "portal-tunnel-grid" not in content
        assert "hmc-tunnel-badge" not in content
    assert "往昔时光 · 历史回顾" not in html
    assert '"time_tunnel"' not in handler


def test_shared_header_sticky_is_not_disabled_by_root_overflow_container():
    theme = (_ROOT / "src" / "webui_static" / "theme.css").read_text(encoding="utf-8")
    archive = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    admin = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    # html/body 的 hidden overflow 会创建一个额外的滚动容器，使 sticky header
    # 跟随 body 一起离开视口；clip 只裁剪横向溢出，不改变 sticky 的参照物。
    assert "header.app-header" in theme
    assert "position: sticky;" in theme
    assert "top: 0;" in theme
    assert "html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; overflow-x: clip;" in theme
    assert "body { min-height: 100vh; min-height: 100dvh; overflow-x: clip;" in theme
    assert "html, body {\n    /* clip 不会创建额外的滚动容器" in theme
    assert "/static/theme.css?v=20260906_1" in archive
    assert "/static/theme.css?v=20260906_1" in admin


def test_mobile_header_2row_layout_and_actions_guard():
    theme = (_ROOT / "src" / "webui_static" / "theme.css").read_text(encoding="utf-8")

    # 杜绝移动端 actions 换行后因旧版 calc(100vw - 125px) 导致左侧残留死区黑块（遮罩现象）
    assert "calc(100vw - 125px)" not in theme

    # 保证移动端统一为两行布局：
    # 第一行：Brand 居左（order: 1），Actions 紧凑并排居右（order: 2, flex: 1 1 0, safe flex-end）
    # 第二行：Nav 标签栏独占全宽（order: 3, flex: 0 0 100%）
    assert "order: 1;" in theme
    assert "order: 2;" in theme
    assert "order: 3;" in theme
    assert "flex: 1 1 0;" in theme
    assert "justify-content: safe flex-end;" in theme
    assert "flex: 0 0 100%;" in theme

    archive = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    admin = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    # 用户相关操作（改密、登出）紧密相邻，跨系统跳转按钮置于最右侧
    assert archive.index('id="changePwBtn"') < archive.index('id="logoutBtn"') < archive.index('id="adminLink"')
    assert admin.index('id="btnChangePw"') < admin.index('id="btnLogout"') < admin.index('id="archiveLink"')


def test_admin_mobile_member_and_openid_layout_guards_are_present():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    # 各尺寸均保留稳定列宽；手机端与账号池一样由表格容器负责横向滚动。
    assert '<table class="member-table">' in html
    assert ".member-table { min-width: 760px; }" in html
    assert ".member-table { min-width: 760px; width: 100%; border-collapse: collapse; }" in html
    assert ".member-table thead { display: table-header-group; }" in html
    assert ".member-table tbody tr { display: table-row; }" in html
    assert ".member-table tbody td::before { content: none; display: none; }" in html
    assert 'tdName.dataset.label = "姓名"' in html
    assert 'tdAcc.dataset.label = "Message 账号"' in html
    assert 'tdSub.dataset.label = "订阅状态"' in html
    assert 'tdOps.dataset.label = "操作"' in html
    assert 'tr.append(tdName, tdAcc, tdSub, tdOps)' in html
    assert 'className = "admin-member-readonly admin-member-name"' in html
    assert 'className = "admin-member-readonly admin-member-account-readonly"' in html
    assert ".admin-member-account-readonly { display: inline; min-height: 0;" in html
    assert "background: transparent; color: var(--text-strong); font-family: var(--mono);" in html
    assert 'mkTableInput(m.id' not in html
    assert 'mkTableInput(m.name' not in html
    assert 'm.name = v.trim()' not in html
    assert 'm.account = sel.value' not in html
    assert 'id="btnAddMember"' not in html
    assert 'memberSocialDialog' not in html
    assert 'memSocial' not in html
    assert 'const SUPPORTED_MEMBER_GROUPS = [' in html
    for group in ("nogizaka46", "hinatazaka46", "sakurazaka46", "yodel"):
        assert f'["{group}"' in html
    assert 'chipGroup("pickGroupChips"' in html
    assert 'getSupportedMemberAccounts(group)' in html
    assert 'getMemberAccountGroup(account) !== group' in html
    assert 'SUPPORTED_MEMBER_GROUP_SET.has(getMemberAccountGroup(_currentFetchedAccount))' in html

    # 存储卡片将容量作为主体，项目数与清理动作放在同一条元信息行。
    assert ".admin-storage-card-meta" in html
    assert ".admin-storage-card-actions" in html
    assert ".admin-storage-card-summary" not in html
    assert "actions.classList.add(\"is-empty\")" in html
    assert "meta.appendChild(actions)" in html
    assert "card.append(heading, size, meta)" in html

    # 账号/用户/历史及各推送路由共用横向操作组，避免按钮被挤成竖列。
    assert ".admin-row-actions { display: inline-flex; align-items: center; gap: 6px; flex-wrap: nowrap;" in html
    assert ".admin-row-actions--account" in html
    assert ".account-table th:last-child, .account-table td:last-child { min-width: 280px; }" in html

    # OpenID 卡片的信息区与操作区必须可被移动端 CSS 独立换行，防止按钮覆盖长 ID。
    assert 'left.className = "cmd-openid-main"' in html
    assert 'code.className = "cmd-openid-code"' in html
    assert 'right.className = "cmd-openid-actions"' in html
    assert ".cmd-openid-card > .cmd-openid-actions" in html
    assert "width: 100%; flex: 0 0 auto; justify-content: flex-start" in html
    assert "display: grid !important; grid-template-columns: auto minmax(0, 1fr)" in html


def test_member_picker_uses_step_layout_and_shared_result_components():
    """成员拉取弹窗应以步骤卡片表达选择流程，并将结果项交给统一 CSS 管理。"""
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")

    dialog_start = html.index('<dialog id="memberPickDialog"')
    dialog_end = html.index("</dialog>", dialog_start)
    dialog = html[dialog_start:dialog_end]
    assert 'class="member-pick-dialog"' in dialog
    assert 'aria-labelledby="memberPickTitle"' in dialog
    assert 'class="member-pick-steps"' in dialog
    assert 'class="member-pick-step"' in dialog
    assert 'class="member-pick-account-row"' in dialog
    assert 'class="member-pick-list"' in dialog
    assert "style=" not in dialog

    for selector in (
        ".member-pick-dialog", ".member-pick-steps", ".member-pick-step",
        ".member-pick-account-row", ".member-pick-toolbar", ".member-pick-row",
        ".member-pick-status-badge", ".member-pick-account-empty",
    ):
        assert selector in html
    assert ".member-pick-step .chip.is-unavailable" in html
    assert "function markUnavailableMemberGroupChips()" in html
    assert "member-pick-stat--subscribed" in html
    assert "member-pick-hint-detail" in html

    render_start = html.index("function renderMemberPickItems()")
    render_end = html.index("function renderMemberPickAccountChips", render_start)
    render_fn = html[render_start:render_end]
    assert "style.cssText" not in render_fn
    assert "member-pick-row" in render_fn
    assert "member-pick-status-badge" in render_fn
    assert "member-pick-date--active" in render_fn
    assert "member-pick-date--past" in render_fn

    # 桌面/平板双列，手机单列；账号与拉取按钮在手机端改为上下堆叠。
    assert ".member-pick-steps { display: grid; grid-template-columns: repeat(2" in html
    assert ".member-pick-steps { grid-template-columns: 1fr;" in html
    assert ".member-pick-account-row { align-items: stretch; flex-direction: column;" in html


def test_admin_restart_and_reload_controls_relocated():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")
    theme = (_ROOT / "src" / "webui_static" / "theme.css").read_text(encoding="utf-8")

    # 顶栏 Actions 不再堆砌管理业务按钮，仅保留主题、用户和消息归档
    header_actions = html.split('<div class="header-actions">')[1].split('</header>')[0]
    assert 'id="btnReloadFile"' not in header_actions
    assert 'id="btnRestart"' not in header_actions

    # 重启主程序按钮移至状态卡片头部，且在移动端允许折行、不再被强制隐藏
    assert 'class="status-actions"' in html
    assert 'id="btnRestart"' in html
    assert '#btnRestart { display: none !important; }' not in theme

    # 放弃修改并重新载入按钮移至固底 savebar 操作区
    assert '<div class="savebar-actions">' in html
    assert 'id="btnReloadFile"' in html
    assert "放弃修改并重新载入" in html


def test_archive_message_order_controls_and_route_state():
    script = (_ROOT / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    html = (_ROOT / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    assert 'let messageOrder' in script
    assert 'localStorage.getItem("archive_message_order")' in script
    assert '&order=" + messageOrder' in script
    assert 'p.set("order", messageOrder)' in script
    assert 'setMessageOrder(requestedOrder' in script
    assert 'id="messageOrderToggle"' in html
    assert 'data-order="desc"' in html and 'data-order="asc"' in html


def test_social_handlers_restore_webui_routes():
    assert callable(social_handlers.handle_subscriptions)
    assert callable(social_handlers.handle_subscriptions_sync)
    assert callable(social_handlers.handle_ig_session_status)
    assert callable(social_handlers.handle_ig_session_save)
    assert callable(social_handlers.handle_ig_session_check)
    assert callable(social_handlers.handle_ig_session_clear)


def test_proxy_test_response_matches_webui_contract(monkeypatch):
    class FakeResponse:
        status_code = 204

    class FakeClient:
        seen_urls = []

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def head(self, url):
            self.seen_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr(system_handlers.httpx, "AsyncClient", FakeClient)
    handler = _ResponseHandler()
    system_handlers.handle_proxy_test(handler, {"proxy": "http://proxy.example:7890"})
    payload = json.loads(handler.wfile.getvalue())

    assert payload["ok"] and payload["all_ok"] and payload["any_ok"]
    assert payload["success_count"] == payload["total_count"] == 4
    first = payload["results"][0]
    assert first["name"] == first["target"] == "Google (Gemini)"
    assert first["status_code"] == first["status"] == 204
    assert "https://generativelanguage.googleapis.com/v1beta/models" in FakeClient.seen_urls


def test_proxy_test_frontend_has_legacy_field_fallbacks():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")
    assert "r.name ?? r.target" in html
    assert "r.status_code ?? r.status" in html


def test_proxy_test_classifies_transport_failure(monkeypatch):
    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def head(self, _url):
            raise system_handlers.httpx.ConnectError("simulated failure")

    monkeypatch.setattr(system_handlers.httpx, "AsyncClient", FailingClient)
    handler = _ResponseHandler()
    system_handlers.handle_proxy_test(handler, {})
    payload = json.loads(handler.wfile.getvalue())

    assert not payload["any_ok"] and not payload["all_ok"]
    assert all(item["error_code"] == "network_error" for item in payload["results"])
    assert all(item["name"] and item["target"] for item in payload["results"])


def test_subscription_sync_reports_partial_failure(monkeypatch):
    async def fake_sync(*_args, **_kwargs):
        return {"healthy": 3}, {"expired": "凭证不可用或已过期"}

    import src.member_directory as member_directory
    monkeypatch.setattr(member_directory, "sync_all_accounts_subscriptions", fake_sync)
    monkeypatch.setattr(member_directory, "get_all_subscriptions", lambda: {"healthy:1": {"state": "active"}})
    handler = _ResponseHandler()

    assert social_handlers.handle_subscriptions_sync(handler)
    payload = json.loads(handler.wfile.getvalue())
    assert payload["ok"] and payload["partial"]
    assert payload["warnings"] == {"expired": "凭证不可用或已过期"}


def test_admin_log_view_controls_and_selection_guards():
    html = (_ROOT / "src" / "webui_static" / "index.html").read_text(encoding="utf-8")
    assert 'id="btnPauseLog"' in html
    assert 'id="btnCopyLog"' in html
    assert "hasLogSelection" in html
    assert "isUserBusyWithLog" in html
    assert "appendLiveEntries" in html
    assert "user-select: text" in html


def test_archive_retry_download_validation(monkeypatch):
    from src.webui_modules.archive import messages

    handler = _ResponseHandler()
    handler.path = "/api/archive/retry_download?member=test_member"
    monkeypatch.setattr(messages._archive, "list_members", lambda: ["test_member"])
    sent_resps = []
    monkeypatch.setattr(messages, "_send_json_resp", lambda h, data, code=200: sent_resps.append((data, code)))

    # 1. Non-dict body
    assert messages.handle_messages(handler, "retry_download", lambda **_: True, lambda: [])
    assert sent_resps[-1][1] == 400

    # 2. Invalid year/month
    assert messages.handle_messages(handler, "retry_download", lambda **_: True, lambda: {"id": "1", "year": "bad", "month": 5})
    assert sent_resps[-1][1] == 400

    # 3. Message not found
    monkeypatch.setattr(messages._archive, "load_month", lambda *a: [])
    assert messages.handle_messages(handler, "retry_download", lambda **_: True, lambda: {"id": "999", "year": 2026, "month": 5})
    assert sent_resps[-1][1] == 404


def test_accounts_verify_route(monkeypatch):
    from unittest.mock import MagicMock
    from src.webui import _Handler
    import config.credentials as creds

    handler = _Handler.__new__(_Handler)
    handler.path = "/api/accounts/verify"
    handler.command = "POST"
    handler.headers = {}
    handler._check_host = MagicMock(return_value=True)
    handler._check_origin = MagicMock(return_value=True)
    handler._check_auth = MagicMock(return_value=True)
    sent_resps = []
    handler._send_json = lambda data, code=200: sent_resps.append((data, code))

    # Missing account
    handler._read_body_json = MagicMock(return_value={})
    handler.do_POST()
    assert sent_resps[-1][1] == 400

    # Successful verify
    async def mock_verify(acc):
        return True, "握手成功", {"plan": "active"}

    monkeypatch.setattr(creds, "verify_and_handshake_account", mock_verify)
    handler._read_body_json = MagicMock(return_value={"account": "acc1"})
    handler.do_POST()
    assert sent_resps[-1][0]["ok"] is True
    assert sent_resps[-1][0]["msg"] == "握手成功"


def test_accounts_smart_parse_route(monkeypatch):
    from unittest.mock import MagicMock
    from src.webui import _Handler

    handler = _Handler.__new__(_Handler)
    handler.path = "/api/accounts/smart_parse"
    handler.command = "POST"
    handler.headers = {}
    handler._check_host = MagicMock(return_value=True)
    handler._check_origin = MagicMock(return_value=True)
    handler._check_auth = MagicMock(return_value=True)
    sent_resps = []
    handler._send_json = lambda data, code=200: sent_resps.append((data, code))

    # Missing raw text
    handler._read_body_json = MagicMock(return_value={})
    handler.do_POST()
    assert sent_resps[-1][1] == 400

    # Successful parse
    async def mock_parse(raw, acc=""):
        return {"token": "tok123", "cookie": "c=1", "refresh_token": "rt123", "extracted": []}

    handler._smart_parse_credentials_text = mock_parse
    handler._read_body_json = MagicMock(return_value={"raw": "curl ...", "account": "acc1"})
    handler.do_POST()
    assert sent_resps[-1][0]["ok"] is True
    assert sent_resps[-1][0]["token"] == "tok123"


def test_accounts_rename_route(monkeypatch):
    from unittest.mock import MagicMock
    from src.webui import _Handler
    import config.credentials as creds

    handler = _Handler.__new__(_Handler)
    handler.path = "/api/accounts/rename"
    handler.command = "POST"
    handler.headers = {}
    handler._check_host = MagicMock(return_value=True)
    handler._check_origin = MagicMock(return_value=True)
    handler._check_auth = MagicMock(return_value=True)
    sent_resps = []
    handler._send_json = lambda data, code=200: sent_resps.append((data, code))

    # Missing params
    handler._read_body_json = MagicMock(return_value={"old_id": "a"})
    handler.do_POST()
    assert sent_resps[-1][1] == 400

    # Successful rename
    renamed = []
    monkeypatch.setattr(creds, "rename_account", lambda old_id, new_id: renamed.append((old_id, new_id)))
    handler._read_body_json = MagicMock(return_value={"old_id": "acc_old", "new_id": "acc_new"})
    handler.do_POST()
    assert sent_resps[-1][0]["ok"] is True
    assert renamed == [("acc_old", "acc_new")]


def test_blog_reader_scroll_preservation_contract():
    """验证博客阅读器返回与滚动记忆契约，确保返回列表时不发生重绘或滚动归零。"""
    css_path = _ROOT / "src" / "webui_static" / "archive.css"
    js_path = _ROOT / "src" / "webui_static" / "archive.js"

    css = css_path.read_text(encoding="utf-8")
    js = js_path.read_text(encoding="utf-8")

    # 1. CSS 契约：modal-open 不得强制限制 height: 100%，防止浏览器将 scrollTop 强制归零
    assert "body.modal-open" in css
    assert "overflow: hidden !important;" in css
    # 确认在 modal-open 规则块中不存在 height: 100%
    modal_rule = css.split("body.modal-open")[1].split("}")[0]
    assert "height: 100%" not in modal_rule, "modal-open 不应设置 height: 100% 防止滚动位置重置"

    # 2. JS 契约：滚动位置记录、多级恢复与避免重复全量拉取
    assert "let blogReaderSavedScroll = 0;" in js
    assert "function restoreWindowScroll(pos)" in js
    assert "restoreWindowScroll(savedScroll);" in js
    assert "isAlreadyMatchingBlogList" in js
    assert "isAlreadyMatchingHome" in js
