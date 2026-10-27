from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from src import archive_tasks
from src.webui_modules.archive.task_routes import authorize_archive_task_route, handle_archive_tasks
from src.webui_modules.archive.task_retry import (
    blog_article_requested,
    blog_page_ids,
    filter_message_targets,
)


@pytest.fixture
def task_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "archive-tasks.sqlite3"
    monkeypatch.setattr(archive_tasks, "_db_path", lambda: path)
    archive_tasks._SCHEMA_READY_PATHS.discard(str(path.resolve()))
    return path


class FakeHandler:
    def __init__(self, method: str, path: str):
        self.command = method
        self.path = path
        self._pending_headers = []
        self.payload = None
        self.status = None

    def _send_json(self, payload, status=200):
        self.payload = payload
        self.status = status


def test_task_transitions_and_payload_survive_store_reinitialization(
    task_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive_tasks, "_process_marker", lambda _pid: "process-start-a")

    queued = archive_tasks.create_task(
        "a1b2c3d4e5f6", "message_backfill", {"member": "Member A", "reset": False},
    )
    assert queued["status"] == "queued"

    archive_tasks.set_item("a1b2c3d4e5f6", "member", "account:member-a", "queued")
    archive_tasks.mark_task_running("a1b2c3d4e5f6", os.getpid())
    running = archive_tasks.get_task("a1b2c3d4e5f6")
    assert running is not None
    assert running["status"] == "running"
    assert running["pid"] == os.getpid()
    assert running["process_marker"] == "process-start-a"
    assert running["heartbeat_at"]

    archive_tasks.set_progress("a1b2c3d4e5f6", "member:Member A", 0, 1)
    archive_tasks.heartbeat_task("a1b2c3d4e5f6", os.getpid())
    archive_tasks.set_item(
        "a1b2c3d4e5f6", "member", "account:member-a", "failed",
        error="HTTP 503 while fetching member archive",
        payload={"member_name": "Member A", "group": "nogizaka"},
    )
    assert archive_tasks.finish_task("a1b2c3d4e5f6") == "failed"

    # A new module instance must read the durable SQLite state as the next WebUI process would.
    archive_tasks._SCHEMA_READY_PATHS.discard(str(task_db.resolve()))
    restored = archive_tasks.get_task("a1b2c3d4e5f6")
    items = archive_tasks.get_items("a1b2c3d4e5f6")
    assert restored is not None
    assert restored["status"] == "failed"
    assert restored["progress"] == {"phase": "failed", "done": 1, "total": 1}
    assert items[0]["error"] == "HTTP 503 while fetching member archive"
    assert items[0]["payload"] == {"member_name": "Member A", "group": "nogizaka"}


def test_stale_task_recovery_uses_persisted_pid_and_process_marker(task_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_tasks.create_task("111111111111", "blog_backfill", {"kind": "groups"})
    archive_tasks.set_item("111111111111", "article", "group:article-1", "completed")
    archive_tasks.set_item("111111111111", "article", "group:article-2", "running")
    with sqlite3.connect(task_db) as conn:
        conn.execute(
            "UPDATE tasks SET pid=?, process_marker=?, heartbeat_at=?, updated_at=? WHERE task_id=?",
            (7654, "prior-process-start", "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", "111111111111"),
        )
    observed = []

    def process_is_alive(pid, marker):
        observed.append((pid, marker))
        return False

    monkeypatch.setattr(archive_tasks, "_process_is_alive", process_is_alive)
    assert archive_tasks.recover_stale_tasks(stale_after_seconds=1) == 1
    assert observed == [(7654, "prior-process-start")]
    task = archive_tasks.get_task("111111111111")
    items = {item["key"]: item for item in archive_tasks.get_items("111111111111")}
    assert task is not None and task["status"] == "interrupted"
    assert task["error"] == "WebUI 重启后发现任务心跳过期且子进程已退出"
    assert items["group:article-1"]["status"] == "completed"
    assert items["group:article-2"]["status"] == "failed"


def test_retries_snapshot_only_failed_items_and_cannot_repeat_parent_retry(
    task_db: Path,
) -> None:
    archive_tasks.create_task("222222222222", "message_backfill", {"member": "A,B,C"})
    for key, status in (("member-a", "completed"), ("member-b", "failed"), ("member-c", "failed")):
        archive_tasks.set_item(
            "222222222222", "member", key, status,
            attempt=1,
            error="HTTP 503" if status == "failed" else "",
            payload={"member_name": key},
        )
    assert archive_tasks.finish_task("222222222222") == "partial"

    retry, retry_items = archive_tasks.create_retry_task("222222222222", "333333333333")
    assert retry["parent_id"] == "222222222222"
    assert {item["key"] for item in retry_items} == {"member-b", "member-c"}
    assert {item["attempt"] for item in retry_items} == {2}
    with pytest.raises(archive_tasks.RetryAlreadyCreated) as duplicate:
        archive_tasks.create_retry_task("222222222222", "444444444444")
    assert duplicate.value.task_id == "333333333333"

    archive_tasks.set_item("333333333333", "member", "member-b", "completed", attempt=2)
    archive_tasks.set_item("333333333333", "member", "member-c", "failed", attempt=2, error="still failing")
    assert archive_tasks.finish_task("333333333333") == "partial"
    retry_again, retry_again_items = archive_tasks.create_retry_task("333333333333", "555555555555")
    assert retry_again["parent_id"] == "333333333333"
    assert [(item["key"], item["attempt"]) for item in retry_again_items] == [("member-c", 3)]
    assert {item["key"] for item in archive_tasks.get_items("333333333333")} == {"member-b", "member-c"}


def test_message_worker_retry_filter_excludes_successful_member_scopes() -> None:
    targets = [
        {"account_id": "a", "group_type": "nogizaka", "m_id": "1"},
        {"account_id": "b", "group_type": "sakurazaka", "m_id": "2"},
        {"account_id": "c", "group_type": "hinatazaka", "m_id": "3"},
    ]

    def member_key(member):
        return f"{member['account_id']}:{member['group_type']}:{member['m_id']}"

    retry_items = [
        {"type": "member", "key": "b:sakurazaka:2", "payload": {"member_key": "b:sakurazaka:2"}},
        {"type": "media", "key": "c:hinatazaka:3:message-9", "payload": {"member_key": "c:hinatazaka:3"}},
    ]
    selected = filter_message_targets(targets, retry_items, member_key)
    assert [member_key(target) for target in selected] == ["b:sakurazaka:2", "c:hinatazaka:3"]
    assert filter_message_targets(targets, [{"type": "task", "key": "execution"}], member_key) == targets


def test_blog_retry_snapshot_skips_completed_articles_even_on_a_failed_page() -> None:
    failed_page = [{
        "type": "page", "key": "nogizaka:0", "payload": {"group": "nogizaka", "page": 0},
    }]
    assert blog_page_ids(failed_page, "nogizaka") == {0}
    completed = [{
        "type": "article",
        "key": "https://example.test/success",
        "status": "completed",
        "payload": {"group": "nogizaka", "page": 0, "url": "https://example.test/success"},
    }]
    assert not blog_article_requested(
        failed_page, "nogizaka", 0, "https://example.test/success", 1, completed_items=completed,
    )
    assert blog_article_requested(
        failed_page, "nogizaka", 0, "https://example.test/not-yet-recorded", 2, completed_items=completed,
    )

    failed_article = [{
        "type": "article",
        "key": "https://example.test/failed",
        "payload": {"group": "nogizaka", "page": 0, "url": "https://example.test/failed"},
    }]
    assert blog_article_requested(failed_article, "nogizaka", 0, "https://example.test/failed", 3)
    assert not blog_article_requested(failed_article, "nogizaka", 0, "https://example.test/other", 4)
    worker_source = (Path(__file__).resolve().parents[1] / "tools" / "backfill_blogs.py").read_text(
        encoding="utf-8",
    )
    assert "task_store.get_completed_retry_ancestor_items(_TASK_ID)" in worker_source
    assert "completed_items=_TASK_COMPLETED_ITEMS" in worker_source


def test_blog_retry_chain_keeps_completed_items_from_all_ancestors(task_db: Path) -> None:
    archive_tasks.create_task("bbbbbbbb0001", "blog_backfill", {"kind": "groups", "groups": ["nogizaka"]})
    archive_tasks.set_item(
        "bbbbbbbb0001", "article", "https://example.test/first-success", "completed",
        payload={"group": "nogizaka", "page": 0, "url": "https://example.test/first-success"},
    )
    archive_tasks.set_item(
        "bbbbbbbb0001", "page", "nogizaka:0", "failed", error="page commit failed",
        payload={"group": "nogizaka", "page": 0},
    )
    archive_tasks.finish_task("bbbbbbbb0001")

    archive_tasks.create_retry_task("bbbbbbbb0001", "bbbbbbbb0002")
    archive_tasks.set_item(
        "bbbbbbbb0002", "article", "https://example.test/second-success", "completed", attempt=2,
        payload={"group": "nogizaka", "page": 0, "url": "https://example.test/second-success"},
    )
    archive_tasks.set_item(
        "bbbbbbbb0002", "page", "nogizaka:0", "failed", attempt=2, error="page still incomplete",
        payload={"group": "nogizaka", "page": 0},
    )
    archive_tasks.finish_task("bbbbbbbb0002")
    _retry, child_items = archive_tasks.create_retry_task("bbbbbbbb0002", "bbbbbbbb0003")

    completed_ancestors = archive_tasks.get_completed_retry_ancestor_items("bbbbbbbb0003")
    completed_keys = {item["key"] for item in completed_ancestors}
    assert completed_keys == {
        "https://example.test/first-success",
        "https://example.test/second-success",
    }
    assert not blog_article_requested(
        child_items, "nogizaka", 0, "https://example.test/first-success", 1,
        completed_items=completed_ancestors,
    )
    assert not blog_article_requested(
        child_items, "nogizaka", 0, "https://example.test/second-success", 2,
        completed_items=completed_ancestors,
    )


def test_retry_routes_launch_children_with_snapshot_task_ids(
    task_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.webui_modules.archive import blog_backfill, message_backfill

    class NoopThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    started = []
    processes = []

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid
            self.stdout = None
            self.exit_code = None

        def poll(self):
            return self.exit_code

    def fake_popen(command, **kwargs):
        started.append((command, kwargs))
        process = FakeProcess(9001 + len(processes))
        processes.append(process)
        return process

    monkeypatch.setattr(message_backfill, "_ACTIVE_BACKFILL_PROC", None)
    monkeypatch.setattr(message_backfill, "_ACTIVE_BACKFILL_REQ_ID", None)
    monkeypatch.setattr(blog_backfill, "_ACTIVE_BACKFILL_PROC", None)
    monkeypatch.setattr(blog_backfill, "_ACTIVE_BACKFILL_REQ_ID", "")
    monkeypatch.setattr(blog_backfill, "_ACTIVE_BACKFILL_KIND", "")
    monkeypatch.setattr(message_backfill.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(blog_backfill.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(message_backfill.threading, "Thread", NoopThread)
    monkeypatch.setattr(blog_backfill.threading, "Thread", NoopThread)

    archive_tasks.create_task(
        "bbbbbbbbbbbb", "message_backfill",
        {"member": "Member A,Member B", "reset": True, "from_date": "2024-04-01"},
    )
    archive_tasks.set_item("bbbbbbbbbbbb", "member", "a:nogizaka:1", "completed")
    archive_tasks.set_item("bbbbbbbbbbbb", "member", "b:sakurazaka:2", "failed", error="HTTP 503")
    assert archive_tasks.finish_task("bbbbbbbbbbbb") == "partial"
    message_response = FakeHandler("POST", "/api/archive/tasks/bbbbbbbbbbbb/retry")
    message_backfill.retry_message_task(message_response, "bbbbbbbbbbbb")
    message_command = started[-1][0]
    message_child_id = message_response.payload["task_id"]
    assert message_response.status == 202
    assert message_command[message_command.index("--task-id") + 1] == message_child_id
    assert message_command[message_command.index("--from") + 1] == "2024-04-01"
    assert message_command[-1] == "--reset"
    assert {item["key"] for item in archive_tasks.get_items(message_child_id)} == {"b:sakurazaka:2"}
    message_targets = [
        {"account_id": "a", "group_type": "nogizaka", "m_id": "1"},
        {"account_id": "b", "group_type": "sakurazaka", "m_id": "2"},
    ]

    def member_key(member):
        return f"{member['account_id']}:{member['group_type']}:{member['m_id']}"

    selected_message_targets = filter_message_targets(
        message_targets, archive_tasks.get_items(message_child_id), member_key,
    )
    assert [member_key(member) for member in selected_message_targets] == ["b:sakurazaka:2"]

    def process_is_alive(_pid, _marker):
        return True

    monkeypatch.setattr(archive_tasks, "_process_is_alive", process_is_alive)
    processes[0].exit_code = 0
    duplicate_message = FakeHandler("POST", "/api/archive/tasks/bbbbbbbbbbbb/retry")
    message_backfill.retry_message_task(duplicate_message, "bbbbbbbbbbbb")
    assert duplicate_message.status == 409
    assert "已创建过重试任务" in duplicate_message.payload["msg"]
    assert duplicate_message.payload["task_id"] == message_child_id
    assert len(started) == 1

    archive_tasks.create_task(
        "cccccccccccc", "blog_backfill",
        {"kind": "groups", "groups": ["nogizaka", "hinatazaka"], "download_images": True},
    )
    archive_tasks.set_item("cccccccccccc", "article", "nogizaka:article-success", "completed")
    archive_tasks.set_item("cccccccccccc", "article", "hinatazaka:article-fail", "failed", error="HTTP 503")
    assert archive_tasks.finish_task("cccccccccccc") == "partial"
    blog_response = FakeHandler("POST", "/api/archive/tasks/cccccccccccc/retry")
    blog_backfill.retry_blog_task(blog_response, "cccccccccccc")
    blog_command = started[-1][0]
    blog_child_id = blog_response.payload["task_id"]
    assert blog_response.status == 202
    assert blog_command.count("--group") == 2
    assert "--download-images" in blog_command
    assert blog_command[blog_command.index("--task-id") + 1] == blog_child_id
    assert {item["key"] for item in archive_tasks.get_items(blog_child_id)} == {"hinatazaka:article-fail"}
    processes[1].exit_code = 0
    duplicate_blog = FakeHandler("POST", "/api/archive/tasks/cccccccccccc/retry")
    blog_backfill.retry_blog_task(duplicate_blog, "cccccccccccc")
    assert duplicate_blog.status == 409
    assert "已创建过重试任务" in duplicate_blog.payload["msg"]
    assert duplicate_blog.payload["task_id"] == blog_child_id
    assert len(started) == 2


def test_integrity_is_outside_the_failure_retry_boundary(task_db: Path) -> None:
    archive_tasks.create_task("666666666666", "archive_integrity", {"read_only": True})
    archive_tasks.set_item("666666666666", "integrity_report", "report", "failed", error="scan failed")
    archive_tasks.finish_task("666666666666")
    with pytest.raises(archive_tasks.TaskNotRetryable):
        archive_tasks.create_retry_task("666666666666", "777777777777")


def test_integrity_overview_is_read_only_and_never_starts_a_worker(
    task_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.webui_modules.archive import integrity
    from src.webui_modules.archive_handlers import handle_archive

    report = integrity._new_report(status="healthy")
    archive_tasks.create_task("666666666667", "archive_integrity", {"read_only": True})
    archive_tasks.set_item(
        "666666666667", "integrity_report", "report", "completed", payload=report,
    )
    archive_tasks.finish_task("666666666667")

    def forbidden_start():
        raise AssertionError("GET /integrity must not create a task or start a worker")

    monkeypatch.setattr(integrity, "start_integrity_check", forbidden_start)
    handler = FakeHandler("GET", "/api/archive/integrity")
    handle_archive(handler, "integrity", lambda **_: True, lambda: None)
    assert handler.status == 200
    assert handler.payload["latest_report"]["status"] == "healthy"
    assert handler.payload["current_task"] is None


def test_archive_task_routes_are_admin_only_and_errors_are_explicit(
    task_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    canaries = (
        "CLIENT_SECRET_CANARY", "APP_SECRET_CANARY", "GENERIC_SECRET_CANARY",
        "OPENID_CANARY", "GROUP_ID_CANARY", "TARGET_CHAT_CANARY",
    )
    archive_tasks.create_task("888888888888", "message_backfill", {"member": "A"})
    archive_tasks.set_item(
        "888888888888", "member",
        "https://example.test/member?token=secret group_id=GROUP_ID_CANARY target_chat=TARGET_CHAT_CANARY",
        "failed",
        error=(
            "HTTP 503 Authorization: Bearer secret-value; "
            "client_secret=CLIENT_SECRET_CANARY; app_secret: APP_SECRET_CANARY; "
            "secret=GENERIC_SECRET_CANARY; openid=OPENID_CANARY"
        ),
    )
    archive_tasks.finish_task("888888888888")
    guards = []

    def admin_guard(*, need_admin):
        guards.append(need_admin)
        return need_admin is True

    handler = FakeHandler("GET", "/api/archive/tasks")
    assert authorize_archive_task_route(handler, "tasks", admin_guard) is True
    handle_archive_tasks(handler, "tasks")
    assert handler.status == 200
    assert handler.payload["tasks"][0]["id"] == "888888888888"
    assert guards == [True]

    from src.webui_modules.archive_handlers import handle_archive

    dispatched = FakeHandler("GET", "/api/archive/tasks")
    handle_archive(dispatched, "tasks", admin_guard, lambda: None)
    assert dispatched.status == 200
    assert dispatched.payload["tasks"][0]["id"] == "888888888888"

    unauthorized = FakeHandler("GET", "/api/archive/tasks")
    assert authorize_archive_task_route(unauthorized, "tasks", lambda **_: False) is False
    assert unauthorized.payload is None

    wrong_method = FakeHandler("POST", "/api/archive/tasks")
    assert authorize_archive_task_route(wrong_method, "tasks", admin_guard) is False
    assert wrong_method.status == 405
    assert wrong_method.payload["msg"] == "请使用 GET 请求"
    assert ("Allow", "GET") in wrong_method._pending_headers

    invalid_id = FakeHandler("GET", "/api/archive/tasks/not-a-task")
    assert authorize_archive_task_route(invalid_id, "tasks/not-a-task", admin_guard) is False
    assert invalid_id.status == 404
    assert invalid_id.payload["msg"] == "任务路径无效"

    detail = FakeHandler("GET", "/api/archive/tasks/888888888888")
    assert authorize_archive_task_route(detail, "tasks/888888888888", admin_guard) is True
    handle_archive_tasks(detail, "tasks/888888888888")
    assert detail.status == 200
    for canary in (*canaries, "secret-value", "token=secret"):
        assert canary not in str(detail.payload)
    assert "HTTP 503" in detail.payload["items"][0]["error_message"]
    assert detail.payload["items"][0]["status"] == "failed"


def test_task_route_rejects_retry_without_failed_items_and_integrity_retry(
    task_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_tasks.create_task("999999999999", "message_backfill", {"member": "A"})
    archive_tasks.finish_task("999999999999")
    archive_tasks.create_task("aaaaaaaaaaaa", "archive_integrity", {"read_only": True})
    archive_tasks.set_item("aaaaaaaaaaaa", "integrity_report", "report", "failed", error="scan failed")
    archive_tasks.finish_task("aaaaaaaaaaaa")

    def guard(**_):
        return True

    no_failures = FakeHandler("POST", "/api/archive/tasks/999999999999/retry")
    assert authorize_archive_task_route(no_failures, "tasks/999999999999/retry", guard) is True
    handle_archive_tasks(no_failures, "tasks/999999999999/retry")
    assert no_failures.status == 409
    assert no_failures.payload["msg"] == "该任务没有可重试的失败项"

    integrity_retry = FakeHandler("POST", "/api/archive/tasks/aaaaaaaaaaaa/retry")
    assert authorize_archive_task_route(integrity_retry, "tasks/aaaaaaaaaaaa/retry", guard) is True
    handle_archive_tasks(integrity_retry, "tasks/aaaaaaaaaaaa/retry")
    assert integrity_retry.status == 400
    assert integrity_retry.payload["msg"] == "此任务类型不支持失败项重试"


def test_task_detail_hides_retry_for_task_failure_after_completed_business_work(
    task_db: Path,
) -> None:
    archive_tasks.create_task("bbbbbbbb1001", "message_backfill", {"member": "A"})
    archive_tasks.set_item("bbbbbbbb1001", "member", "member-a", "completed")
    assert archive_tasks.finish_task("bbbbbbbb1001", error="worker failed after item completion") == "partial"

    detail = FakeHandler("GET", "/api/archive/tasks/bbbbbbbb1001")
    handle_archive_tasks(detail, "tasks/bbbbbbbb1001")

    assert detail.status == 200
    assert detail.payload["task"]["failure_count"] == 1
    assert [item["item_type"] for item in detail.payload["items"]] == ["task"]
    assert detail.payload["retry_supported"] is False
    assert archive_tasks.can_retry_task("bbbbbbbb1001") is False
    with pytest.raises(archive_tasks.NoFailedItems):
        archive_tasks.create_retry_task("bbbbbbbb1001", "bbbbbbbb1002")


def test_task_only_failure_without_completed_work_matches_retry_support(task_db: Path) -> None:
    archive_tasks.create_task("bbbbbbbb2001", "blog_backfill", {"kind": "groups"})
    archive_tasks.fail_to_start("bbbbbbbb2001", "worker could not start")

    detail = FakeHandler("GET", "/api/archive/tasks/bbbbbbbb2001")
    handle_archive_tasks(detail, "tasks/bbbbbbbb2001")

    assert detail.status == 200
    assert detail.payload["items"][0]["item_type"] == "task"
    assert detail.payload["retry_supported"] is True
    assert archive_tasks.can_retry_task("bbbbbbbb2001") is True
    retry, retry_items = archive_tasks.create_retry_task("bbbbbbbb2001", "bbbbbbbb2002")
    assert retry["parent_id"] == "bbbbbbbb2001"
    assert [(item["type"], item["key"]) for item in retry_items] == [("task", "execution")]


def test_task_route_storage_failure_logs_only_safe_code_and_type(
    task_db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_list_tasks(*, limit: int = 10):
        raise OSError("SECRET_CANARY must never be logged")

    monkeypatch.setattr(archive_tasks, "list_tasks", fail_list_tasks)
    handler = FakeHandler("GET", "/api/archive/tasks")
    with caplog.at_level("WARNING", logger="src.webui_modules.archive.task_routes"):
        handle_archive_tasks(handler, "tasks")

    assert handler.status == 503
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "archive_task_storage_failure code=archive_task.list exception_type=OSError",
    ]
    assert "SECRET_CANARY" not in str(caplog.records)
