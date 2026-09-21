"""低成本的前端设计系统门禁。

这些检查不替代浏览器视觉回归，而是阻止资源版本、HTML 结构、主题 token 和
归档关键交互在后续改动中悄悄漂移。测试只读取静态文件，不启动 WebUI。
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "webui_static"


def test_shared_theme_exposes_semantic_tokens_and_states():
    theme = (STATIC / "theme.css").read_text(encoding="utf-8")

    for token in (
        "--space-1", "--space-2", "--space-3", "--space-4", "--space-5", "--space-6",
        "--text-xs", "--text-sm", "--text-md", "--text-lg", "--text-xl", "--text-2xl",
        "--radius-xs", "--radius-md", "--radius-pill", "--focus-ring", "--overlay",
        "--z-header", "--z-float", "--z-popover", "--z-modal", "--z-toast",
        "--z-lightbox", "--z-lightbox-controls", "--motion-fast", "--motion-normal",
        "--motion-ease", "--hover", "--bg-hover", "--border-hover",
    ):
        assert token in theme, f"shared theme token missing: {token}"

    assert ":where(button, a, input, select, textarea, [tabindex]):focus-visible" in theme
    assert ":where(button, input, select, textarea):disabled" in theme
    assert "@media (prefers-reduced-motion: reduce)" in theme
    assert "scroll-behavior: auto !important" in theme
    assert "dialog::backdrop { background: var(--overlay);" in theme


def test_frontend_pages_have_one_html_end_and_shared_theme():
    for page_name in ("index.html", "archive.html", "login.html", "404.html"):
        page = (STATIC / page_name).read_text(encoding="utf-8")
        assert page.count("</body>") == 1, f"{page_name} must have one </body>"
        assert page.count("</html>") == 1, f"{page_name} must have one </html>"
        assert "/static/theme.css" in page, f"{page_name} must load shared theme.css"

    archive = (STATIC / "archive.html").read_text(encoding="utf-8")
    for control_id, label in (
        ("lbClose", "关闭图片预览"),
        ("lbDownloadBtn", "下载高清原图"),
        ("lbPrev", "上一张图片"),
        ("lbNext", "下一张图片"),
    ):
        control_start = archive.index(f'id="{control_id}"')
        control_end = archive.find(">", control_start)
        assert f'aria-label="{label}"' in archive[control_start:control_end]
    for modal_id, label_id in (
        ("customConfirmModal", "cmTitle"),
        ("customPromptModal", "pmTitle"),
        ("backfillMessageModal", "bmTitle"),
        ("archiveToolsSheet", "archiveToolsSheetTitle"),
    ):
        start = archive.index(f'id="{modal_id}"')
        end = archive.find("</div>", start)
        assert 'role="dialog"' in archive[start:end + 6], modal_id
        assert f'aria-labelledby="{label_id}"' in archive[start:end + 6], modal_id


def test_inline_style_debt_does_not_grow():
    """旧页面仍有少量动态显示 inline style；门禁只允许减少，不允许继续增加。"""
    limits = {"index.html": 95, "archive.html": 56, "login.html": 0, "404.html": 0}
    for page_name, limit in limits.items():
        page = (STATIC / page_name).read_text(encoding="utf-8")
        assert page.count("style=") <= limit, (
            f"{page_name} introduced new inline styles; use shared classes/tokens instead"
        )


def test_archive_cache_versions_are_synchronised():
    html = (STATIC / "archive.html").read_text(encoding="utf-8")
    perf = (ROOT / "tools" / "measure_archive_performance.py").read_text(encoding="utf-8")

    for resource in (
        "/static/archive.css?v=20260921_9",
        "/static/archive.js?v=20260921_6",
    ):
        assert html.count(resource) == (2 if resource.endswith("archive.js?v=20260921_6") else 1)
        assert resource in perf


def test_archive_scroll_media_and_lightbox_contracts():
    styles = (STATIC / "archive.css").read_text(encoding="utf-8")
    script = (STATIC / "archive.js").read_text(encoding="utf-8")

    assert "top: calc(var(--header-height) + var(--space-4));" in styles
    assert "scroll-margin-top: calc(var(--header-height) + var(--space-5) + var(--space-1));" in styles
    assert "aspect-ratio: var(--photo-ratio, 3 / 4);" in styles
    assert "contain-intrinsic-size: 240px 320px;" in styles
    assert "z-index: var(--z-lightbox);" in styles
    assert "z-index: var(--z-lightbox-controls);" in styles

    # 只有真实图片或灯箱背景可以关闭，stage 子元素不能把点击传到底层卡片。
    assert "const isBackdrop = e.target === e.currentTarget;" in script
    assert "if (!interactive && (isBackdrop || onImage)) closeLightbox();" in script
    assert "lbStage 本身不接收指针事件" in script
    assert "document.addEventListener(\"keydown\", onKeydown);" in script
    assert "document.addEventListener(\"keydown\", onDocumentKeydown);" in script
    assert "if (opener && opener !== document.body && document.contains(opener)) opener.focus();" in script


def test_message_day_separator_aligns_with_message_cards():
    styles = (STATIC / "archive.css").read_text(encoding="utf-8")
    assert ".day-sep { display: flex; width: 92%; max-width: 100%;" in styles
    assert "    .day-sep { width: 100%; }" in styles


def test_member_popovers_support_shared_collapsible_saka_groups():
    script = (STATIC / "archive.js").read_text(encoding="utf-8")
    styles = (STATIC / "archive.css").read_text(encoding="utf-8")
    assert 'const POPOVER_GROUP_STATE_KEY = "archive_popover_collapsed_groups_v1";' in script
    assert "const collapsedPopoverGroups = loadCollapsedPopoverGroups();" in script
    assert "sessionStorage.getItem(POPOVER_GROUP_STATE_KEY)" in script
    assert "sessionStorage.setItem(POPOVER_GROUP_STATE_KEY" in script
    assert "function expandPopoverGroups(scope)" in script
    assert "function createPopoverGroupHeader(group, count, onToggle)" in script
    assert "event.stopPropagation();" in script[script.index("function createPopoverGroupHeader"):script.index("function renderMemberPopover")]
    for renderer in ("renderMemberPopover", "renderLetterMemberPopover", "renderGalleryMemberPopover"):
        start = script.index("function " + renderer)
        next_start = script.find("function ", start + len(renderer) + 9)
        block = script[start:next_start if next_start >= 0 else len(script)]
        assert "createPopoverGroupHeader" in block
    archive = (STATIC / "archive.html").read_text(encoding="utf-8")
    for scope in ("msg", "letter", "gallery"):
        assert f'data-popover-scope="{scope}"' in archive
    assert ".popover-expand-all[hidden]" in styles
    assert ".popover-group-header[aria-expanded=\"false\"] .pgh-chevron" in styles


def test_sakurazaka_selected_state_has_explicit_dark_theme_contrast():
    theme = (STATIC / "theme.css").read_text(encoding="utf-8")
    styles = (STATIC / "archive.css").read_text(encoding="utf-8")

    assert "--brand-sakurazaka: #f1f4f8;" in theme
    assert ".seg-btn.active[data-key=\"sakurazaka\"]" in styles
    assert "color: #253047 !important;" in styles
