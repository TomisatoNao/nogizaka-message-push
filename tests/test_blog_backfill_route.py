import io
import threading
from types import SimpleNamespace

import pytest

from src.webui_modules import archive_handlers
from src.webui_modules.archive import blog_backfill


URL = "https://www.nogizaka46.com/s/n46/diary/MEMBER/list?ima=5242&ct=55396"


class Handler:
    path = "/api/archive/blogs/archive_member"
    command = "POST"

    def _send_json(self, payload, code=200):
        self.payload = payload
        self.code = code


def invoke(body, guard=lambda **_: True, command="POST"):
    handler = Handler()
    handler.command = command
    archive_handlers.handle_archive(handler, "blogs/archive_member", guard, lambda: body)
    return handler


@pytest.mark.parametrize("translate", [False, True])
@pytest.mark.parametrize("url", [URL,
    "https://sakurazaka46.com/s/s46/diary/blog/list?ct=59",
    "https://www.hinatazaka46.com/s/official/diary/member/list?ct=12",
])
def test_member_backfill_reaches_existing_tool(monkeypatch, url, translate):
    calls = []
    threads = []
    process = SimpleNamespace(pid=123, stdout=io.StringIO("归档进度\n"), wait=lambda: 0)
    monkeypatch.setattr(blog_backfill.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)) or process)
    monkeypatch.setattr(blog_backfill.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: threads.append(kw)))
    monkeypatch.setattr(blog_backfill, "log_all", lambda *a, **kw: None)
    guard_calls = []
    handler = invoke({"url": url, "translate": translate}, lambda **kw: guard_calls.append(kw) or True)
    assert handler.code == 202
    assert handler.payload["ok"] is True
    assert handler.payload["request_id"]
    assert guard_calls == [{"need_admin": True}]
    command = calls[0][0][0]
    assert command[:2] == [blog_backfill.sys.executable, "-u"]
    assert command[2].endswith("archive_member.py")
    assert command[3] == url
    assert ("--translate" in command) == translate
    assert calls[0][1]["encoding"] == "utf-8"
    assert calls[0][1]["cwd"] == str(blog_backfill._ROOT)
    assert threads[0]["args"] == (process, handler.payload["request_id"])


def test_member_backfill_denied_without_admin(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Denied request must not launch a process or read its body")
    monkeypatch.setattr(blog_backfill.subprocess, "Popen", forbidden)
    handler = Handler()
    archive_handlers.handle_archive(handler, "blogs/archive_member", lambda **_: False, forbidden)
    assert not hasattr(handler, "payload")


@pytest.mark.parametrize("body", [
    {}, [], {"url": 123}, {"url": ""}, {"url": "--help"},
    {"url": "https://nogizaka46.com.attacker.test/s/n46/diary/MEMBER/list?ct=55396"},
    {"url": "https://www.nogizaka46.com/s/n46/diary/MEMBER/list"},
    {"url": "https://www.nogizaka46.com/s/n46/diary/MEMBER/list?ct=bad"},
    {"url": "https://www.nogizaka46.com:8080/s/n46/diary/MEMBER/list?ct=55396"},
    {"url": URL, "translate": "false"},
])
def test_invalid_backfill_input_does_not_start_process(monkeypatch, body):
    monkeypatch.setattr(blog_backfill.subprocess, "Popen", lambda *a, **kw: pytest.fail("Invalid request launched process"))
    assert invoke(body).code == 400


def test_wrong_method_and_unreadable_body_do_not_launch(monkeypatch):
    monkeypatch.setattr(blog_backfill.subprocess, "Popen", lambda *a, **kw: pytest.fail("Unexpected process"))
    assert invoke({"url": URL}, command="GET").code == 405
    assert not hasattr(invoke(None), "payload")


def test_missing_script_and_spawn_error_are_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(blog_backfill, "_ROOT", tmp_path)
    assert invoke({"url": URL}).code == 503
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/archive_member.py").write_text("", encoding="utf-8")
    def fail(*a, **kw):
        raise OSError("test launch failure")
    monkeypatch.setattr(blog_backfill.subprocess, "Popen", fail)
    monkeypatch.setattr(blog_backfill, "log_all", lambda *a, **kw: None)
    handler = invoke({"url": URL})
    assert handler.code == 500
    assert handler.payload["ok"] is False
    assert handler.payload["request_id"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_progress_and_exit_status_reach_system_logs(monkeypatch, exit_code):
    logs = []
    monkeypatch.setattr(blog_backfill, "log_all", lambda text, **kw: logs.append((text, kw)))
    process = SimpleNamespace(stdout=io.StringIO("\n正在获取博客 🌸\n"), wait=lambda: exit_code)
    blog_backfill._collect_output(process, "trace123")
    assert len(logs) == 2
    assert "正在获取博客 🌸" in logs[0][0]
    assert all("request_id=trace123" in text for text, _ in logs)
    assert logs[-1][1]["is_error"] == (exit_code != 0)


def test_real_background_process_forwards_unicode_progress(monkeypatch, tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/archive_member.py").write_text(
        "import sys\nprint('博客进度 🌸', flush=True)\nprint(sys.argv[1:], flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(blog_backfill, "_ROOT", tmp_path)
    finished = threading.Event()
    logs = []
    def capture(text, **kwargs):
        logs.append(text)
        if "exit_code=" in text:
            finished.set()
    monkeypatch.setattr(blog_backfill, "log_all", capture)
    handler = invoke({"url": URL, "translate": True})
    assert handler.code == 202
    assert finished.wait(10), "Background process did not complete"
    assert any("博客进度 🌸" in line for line in logs)
    assert any(URL in line and "--translate" in line for line in logs)
    assert "exit_code=0" in logs[-1]
