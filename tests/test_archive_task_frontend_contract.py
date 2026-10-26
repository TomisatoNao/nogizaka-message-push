from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "webui_static"


def test_admin_task_center_resumes_persisted_tasks_after_page_reload():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "admin_tasks.js").read_text(encoding="utf-8")

    # The admin tab and panel remain addressable after reload, and the page loads
    # the task-center controller exactly once.
    assert 'id="tabTasks"' in page and 'data-tab="tasks"' in page
    assert 'id="tab-tasks"' in page
    assert page.count('/static/admin_tasks.js?v=20261026_2') == 1
    assert '"tasks"' in page[page.index("const validTabs") : page.index("const validTabs") + 180]

    # Auth controls the entry point, while the task controller waits for the
    # auth-ready event before enabling requests.
    assert 'if ($("tabTasks")) $("tabTasks").hidden = !isAdmin;' in page
    assert 'if ($("tab-tasks")) $("tab-tasks").hidden = !isAdmin;' in page
    assert 'window.addEventListener("webadminauthready", initializeForRole' in script
    assert 'window.syncArchiveTaskPolling?.(targetTab === "tasks")' in page

    # Re-entering the task tab, page restore, or becoming visible reloads the
    # server-backed list with no-store caching; the rendered card shows status,
    # progress, and failure count from that response.
    assert 'requestWithTimeout("/api/archive/tasks", controller)' in script
    assert 'cache: "no-store"' in script
    assert 'window.addEventListener("pageshow"' in script
    assert 'card.badge.textContent = meta.label;' in script
    assert 'card.progress.textContent = `${done} / ${total} · 失败 ${failures}`;' in script
    for label in ("排队中", "执行中", "已完成", "部分完成", "失败", "已中断"):
        assert label in script

    # Expanding a persisted task fetches details and renders failed-item reasons
    # as text. Retry visibility follows the server's current, filtered snapshot.
    assert '`/api/archive/tasks/${encodeURIComponent(id)}`' in script
    assert 'message.textContent = item.error_message ? String(item.error_message) : "未提供明确错误原因。";' in script
    assert 'for (const item of failedItems) card.items.append(makeErrorItem(item));' in script
    assert 'retryButton.textContent = "只重试失败项";' in script
    assert 'typeof data.retry_supported !== "boolean"' in script
    assert 'state.retrySupported = data.retry_supported;' in script
    assert 'const canRetry = isTerminalTask(task) && state.retrySupported === true;' in script
    assert '|| state.retrySupported !== true' in script
    assert '当前没有可安全重试的项' in script
