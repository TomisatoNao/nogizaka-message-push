import io
import threading
from types import SimpleNamespace

import pytest

from src.webui_modules import archive_handlers
from src.webui_modules.archive import message_backfill


class Handler:
    path = "/api/archive/messages/backfill"
    command = "POST"

    def _send_json(self, payload, code=200):
        self.payload = payload
        self.code = code


def invoke(body, guard=lambda **_: True, command="POST"):
    handler = Handler()
    handler.command = command
    archive_handlers.handle_archive(handler, "messages/backfill", guard, lambda: body)
    return handler


@pytest.fixture(autouse=True)
def reset_active_backfill():
    with message_backfill._BACKFILL_LOCK:
        message_backfill._ACTIVE_BACKFILL_PROC = None
        message_backfill._ACTIVE_BACKFILL_REQ_ID = None
    yield
    with message_backfill._BACKFILL_LOCK:
        message_backfill._ACTIVE_BACKFILL_PROC = None
        message_backfill._ACTIVE_BACKFILL_REQ_ID = None


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("member, expected_args", [
    ("", []),
    ("冨里 奈央", ["冨里 奈央"]),
    ("冨里 奈央, 菅原 咲月", ["冨里 奈央", "菅原 咲月"]),
    ("冨里 奈央、佐藤 優羽；井上 和", ["冨里 奈央", "佐藤 優羽", "井上 和"]),
])
def test_message_backfill_reaches_existing_tool(monkeypatch, member, expected_args, reset):
    calls = []
    threads = []
    process = SimpleNamespace(pid=123, stdout=io.StringIO("消息回填进度\n"), wait=lambda: 0)
    monkeypatch.setattr(message_backfill.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)) or process)
    monkeypatch.setattr(message_backfill.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: threads.append(kw)))
    monkeypatch.setattr(message_backfill, "log_all", lambda *a, **kw: None)

    guard_calls = []
    handler = invoke({"member": member, "reset": reset}, lambda **kw: guard_calls.append(kw) or True)

    assert handler.code == 202
    assert handler.payload["ok"] is True
    assert handler.payload["request_id"]
    assert guard_calls == [{"need_admin": True}]

    command = calls[0][0][0]
    assert command[:2] == [message_backfill.sys.executable, "-u"]
    assert command[2].endswith("backfill_archive.py")
    assert command[3] == "--force"

    for arg in expected_args:
        assert arg in command
    if reset:
        assert "--reset" in command
    else:
        assert "--reset" not in command

    assert calls[0][1]["encoding"] == "utf-8"
    assert calls[0][1]["cwd"] == str(message_backfill._ROOT)
    assert threads[0]["args"] == (process, handler.payload["request_id"])


def test_message_backfill_denied_without_admin(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Denied request must not launch a process or read its body")
    monkeypatch.setattr(message_backfill.subprocess, "Popen", forbidden)
    handler = Handler()
    archive_handlers.handle_archive(handler, "messages/backfill", lambda **_: False, forbidden)
    assert not hasattr(handler, "payload")


@pytest.mark.parametrize("body", [
    [], {"member": 123}, {"member": "a" * 201}, {"member": "test", "reset": "false"},
    {"from_date": 12345}, {"from_date": "2023-99-99-bad"}, {"from_date": "invalid-date"},
])
def test_invalid_message_backfill_input_does_not_start_process(monkeypatch, body):
    monkeypatch.setattr(message_backfill.subprocess, "Popen", lambda *a, **kw: pytest.fail("Invalid request launched process"))
    assert invoke(body).code == 400


def test_message_backfill_with_from_date(monkeypatch):
    calls = []
    process = SimpleNamespace(pid=456, stdout=io.StringIO(""), wait=lambda: 0)
    monkeypatch.setattr(message_backfill.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)) or process)
    monkeypatch.setattr(message_backfill.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(message_backfill, "log_all", lambda *a, **kw: None)

    handler = invoke({"member": "冨里 奈央", "reset": True, "from_date": "2023-05-01"})
    assert handler.code == 202
    cmd = calls[0][0][0]
    assert "--from" in cmd
    idx = cmd.index("--from")
    assert cmd[idx + 1] == "2023-05-01"
    assert "冨里 奈央" in cmd
    assert "--reset" in cmd


def test_message_backfill_conflict_when_already_running(monkeypatch):
    mock_running_process = SimpleNamespace(pid=999, poll=lambda: None)
    with message_backfill._BACKFILL_LOCK:
        message_backfill._ACTIVE_BACKFILL_PROC = mock_running_process
        message_backfill._ACTIVE_BACKFILL_REQ_ID = "active-req-123"

    monkeypatch.setattr(message_backfill.subprocess, "Popen", lambda *a, **kw: pytest.fail("Should not launch when running"))
    handler = invoke({"member": ""})
    assert handler.code == 409
    assert handler.payload["ok"] is False
    assert "正在执行中" in handler.payload["msg"]
    assert handler.payload["request_id"] == "active-req-123"


def test_wrong_method_and_unreadable_body_do_not_launch(monkeypatch):
    monkeypatch.setattr(message_backfill.subprocess, "Popen", lambda *a, **kw: pytest.fail("Unexpected process"))
    assert invoke({"member": ""}, command="GET").code == 405
    assert not hasattr(invoke(None), "payload")


def test_missing_script_and_spawn_error_are_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(message_backfill, "_ROOT", tmp_path)
    assert invoke({"member": ""}).code == 503

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/backfill_archive.py").write_text("", encoding="utf-8")

    def fail(*a, **kw):
        raise OSError("test launch failure")

    monkeypatch.setattr(message_backfill.subprocess, "Popen", fail)
    monkeypatch.setattr(message_backfill, "log_all", lambda *a, **kw: None)
    handler = invoke({"member": ""})
    assert handler.code == 500
    assert handler.payload["ok"] is False
    assert handler.payload["request_id"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_progress_and_exit_status_reach_system_logs(monkeypatch, exit_code):
    logs = []
    monkeypatch.setattr(message_backfill, "log_all", lambda text, **kw: logs.append((text, kw)))
    process = SimpleNamespace(stdout=io.StringIO("\n正在同步历史消息 💌\n"), wait=lambda: exit_code)
    message_backfill._collect_output(process, "trace456")
    assert len(logs) == 2
    assert "正在同步历史消息 💌" in logs[0][0]
    assert all("request_id=trace456" in text for text, _ in logs)
    assert logs[-1][1]["is_error"] == (exit_code != 0)


def test_real_background_process_forwards_unicode_progress(monkeypatch, tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/backfill_archive.py").write_text(
        "import sys\nprint('消息回填进度 💌', flush=True)\nprint(sys.argv[1:], flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(message_backfill, "_ROOT", tmp_path)
    finished = threading.Event()
    logs = []

    def capture(text, **kwargs):
        logs.append(text)
        if "exit_code=" in text:
            finished.set()

    monkeypatch.setattr(message_backfill, "log_all", capture)
    handler = invoke({"member": "冨里 奈央", "reset": True})
    assert handler.code == 202
    assert finished.wait(10), "Background process did not complete"
    assert any("消息回填进度 💌" in line for line in logs)
    assert any("冨里 奈央" in line and "--reset" in line for line in logs)
    assert "exit_code=0" in logs[-1]
