"""启动已有消息归档回填工具 (tools/backfill_archive.py)，并把进度转入管理端日志。"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import uuid

from src import archive_tasks
from src.logger import log_all
from src.webui_modules.archive.common import _send_json_resp

_ROOT = Path(__file__).resolve().parents[3]

_BACKFILL_LOCK = threading.Lock()
_ACTIVE_BACKFILL_PROC: subprocess.Popen | None = None
_ACTIVE_BACKFILL_REQ_ID: str | None = None


def _is_active_proc_running() -> bool:
    """Called under _BACKFILL_LOCK; uncertain state must not allow another job."""
    global _ACTIVE_BACKFILL_PROC, _ACTIVE_BACKFILL_REQ_ID
    if _ACTIVE_BACKFILL_PROC is None:
        return False
    poll_fn = getattr(_ACTIVE_BACKFILL_PROC, "poll", None)
    if callable(poll_fn):
        try:
            if poll_fn() is not None:
                _ACTIVE_BACKFILL_PROC = None
                _ACTIVE_BACKFILL_REQ_ID = None
                return False
        except (OSError, ValueError):
            pass
    return True


def _collect_output(process, request_id: str) -> None:
    global _ACTIVE_BACKFILL_PROC, _ACTIVE_BACKFILL_REQ_ID
    finished = False
    try:
        if process.stdout is not None:
            with process.stdout:
                for line in process.stdout:
                    if line.strip():
                        log_all(f"[消息回填] request_id={request_id} | {line.strip()}")
        code = process.wait()
        finished = True
        archive_tasks.finalize_after_process_exit(request_id, code)
        log_all(f"[消息回填] 进程结束 | request_id={request_id} | exit_code={code}", is_error=code != 0)
    except (OSError, ValueError) as exc:
        log_all(f"[消息回填] 日志读取失败 | request_id={request_id} | error={exc}", is_error=True)
    finally:
        with _BACKFILL_LOCK:
            # Pipe errors do not prove that the child exited. Keep ownership;
            # the next request can poll it without allowing overlapping work.
            if finished and _ACTIVE_BACKFILL_PROC is process:
                _ACTIVE_BACKFILL_PROC = None
                _ACTIVE_BACKFILL_REQ_ID = None


def handle_message_backfill(handler, guard_fn, read_body_json_fn) -> None:
    global _ACTIVE_BACKFILL_PROC, _ACTIVE_BACKFILL_REQ_ID
    if not guard_fn(need_admin=True):
        return
    if handler.command != "POST":
        _send_json_resp(handler, {"ok": False, "msg": "请使用 POST 请求"}, 405)
        return
    body = read_body_json_fn()
    if body is None:
        return
    if not isinstance(body, dict):
        _send_json_resp(handler, {"ok": False, "msg": "请求体必须是 JSON 对象"}, 400)
        return

    member = body.get("member", "")
    reset = body.get("reset", False)
    from_date = body.get("from_date") or body.get("from") or ""

    if not isinstance(member, str) or len(member) > 200:
        _send_json_resp(handler, {"ok": False, "msg": "member 必须是 200 字符以内的字符串"}, 400)
        return
    if not isinstance(reset, bool):
        _send_json_resp(handler, {"ok": False, "msg": "reset 必须是布尔值"}, 400)
        return
    if not isinstance(from_date, str):
        _send_json_resp(handler, {"ok": False, "msg": "from_date 必须是字符串"}, 400)
        return
    cleaned_from = from_date.strip()
    if cleaned_from and not re.match(r"^\d{4}-\d{2}-\d{2}$", cleaned_from):
        _send_json_resp(handler, {"ok": False, "msg": "from_date 格式必须为 YYYY-MM-DD（例如 2023-01-01）"}, 400)
        return

    request_id = uuid.uuid4().hex[:12]
    script = _ROOT / "tools" / "backfill_archive.py"
    if not script.is_file():
        _send_json_resp(handler, {"ok": False, "msg": "服务器缺少消息回填工具，请检查部署文件"}, 503)
        return

    # 从 WebUI 启动必须带 --force 跳过主程序运行检测
    command = [sys.executable, "-u", str(script), "--force", "--task-id", request_id]
    if cleaned_from:
        command.extend(["--from", cleaned_from])
    cleaned_member = member.strip()
    if cleaned_member:
        tokens = [t.strip() for t in re.split(r"[,，、;；\n]+", cleaned_member) if t.strip()]
        if tokens:
            command.extend(tokens)
    if reset:
        command.append("--reset")

    # Keep checking, spawning and registration atomic within this WebUI process.
    with _BACKFILL_LOCK:
        if _is_active_proc_running():
            _send_json_resp(
                handler,
                {
                    "ok": False,
                    "msg": "已有消息回填任务正在执行中，请等待其完成或在系统日志中查看进度",
                    "request_id": _ACTIVE_BACKFILL_REQ_ID,
                },
                409,
            )
            return

        try:
            archive_tasks.create_task(
                request_id,
                "message_backfill",
                {"member": member.strip(), "reset": reset, "from_date": cleaned_from},
            )
        except archive_tasks.ActiveTaskExists as exc:
            _send_json_resp(handler, {
                "ok": False,
                "msg": "已有消息回填任务正在执行中，请等待其完成或在系统日志中查看进度",
                "request_id": exc.task_id,
                "task_id": exc.task_id,
            }, 409)
            return
        except (OSError, sqlite3.Error):
            _send_json_resp(handler, {"ok": False, "msg": "归档任务存储暂时不可用，请查看服务端日志"}, 503)
            return

        try:
            process = subprocess.Popen(
                command,
                cwd=str(_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            archive_tasks.fail_to_start(request_id, str(exc))
            log_all(f"[消息回填] 启动失败 | request_id={request_id} | error={exc}", is_error=True)
            _send_json_resp(handler, {"ok": False, "msg": "启动回填任务失败，请查看系统日志", "request_id": request_id, "task_id": request_id}, 500)
            return

        archive_tasks.mark_task_running(request_id, process.pid)
        _ACTIVE_BACKFILL_PROC = process
        _ACTIVE_BACKFILL_REQ_ID = request_id

    log_all(
        f"[消息回填] 已启动 | request_id={request_id} | pid={process.pid} | "
        f"member={cleaned_member or 'ALL'} | reset={reset} | from={cleaned_from or 'auto'}"
    )
    threading.Thread(
        target=_collect_output,
        args=(process, request_id),
        daemon=True,
        name=f"msg-backfill-{request_id}",
    ).start()

    _send_json_resp(
        handler,
        {
            "ok": True,
            "msg": "已成功启动消息归档回填任务，可在归档任务中心查看进度。",
            "request_id": request_id,
            "task_id": request_id,
        },
        202,
    )


def retry_message_task(handler, parent_task_id: str) -> None:
    """Create one child task from failed items and launch the same worker."""
    global _ACTIVE_BACKFILL_PROC, _ACTIVE_BACKFILL_REQ_ID
    script = _ROOT / "tools" / "backfill_archive.py"
    if not script.is_file():
        _send_json_resp(handler, {"ok": False, "msg": "服务器缺少消息回填工具，请检查部署文件"}, 503)
        return
    with _BACKFILL_LOCK:
        if _is_active_proc_running():
            active_id = _ACTIVE_BACKFILL_REQ_ID
            _send_json_resp(handler, {
                "ok": False,
                "msg": "已有消息回填任务正在执行中，请等待其完成后再重试",
                "request_id": active_id,
                "task_id": active_id,
            }, 409)
            return
        task_id = archive_tasks.new_task_id()
        try:
            task, _items = archive_tasks.create_retry_task(parent_task_id, task_id)
        except archive_tasks.TaskNotFound:
            _send_json_resp(handler, {"ok": False, "msg": "归档任务不存在"}, 404)
            return
        except archive_tasks.NoFailedItems:
            _send_json_resp(handler, {"ok": False, "msg": "该任务没有可重试的失败项"}, 409)
            return
        except archive_tasks.RetryAlreadyCreated as exc:
            _send_json_resp(handler, {
                "ok": False,
                "msg": "该任务已创建过重试任务，请打开最新重试任务继续处理失败项",
                "request_id": exc.task_id,
                "task_id": exc.task_id,
            }, 409)
            return
        except archive_tasks.ActiveTaskExists as exc:
            _send_json_resp(handler, {
                "ok": False,
                "msg": "已有消息回填任务正在执行中，请等待其完成后再重试",
                "request_id": exc.task_id,
                "task_id": exc.task_id,
            }, 409)
            return

        params = task.get("params", {})
        command = [sys.executable, "-u", str(script), "--force", "--task-id", task_id]
        from_date = str(params.get("from_date") or "").strip()
        member = str(params.get("member") or "").strip()
        if from_date:
            command.extend(["--from", from_date])
        if member:
            command.extend(token.strip() for token in re.split(r"[,，、;；\n]+", member) if token.strip())
        if params.get("reset") is True:
            command.append("--reset")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            archive_tasks.fail_to_start(task_id, str(exc))
            _send_json_resp(handler, {"ok": False, "msg": "启动回填任务失败，请查看系统日志", "request_id": task_id, "task_id": task_id}, 500)
            return
        _ACTIVE_BACKFILL_PROC = process
        _ACTIVE_BACKFILL_REQ_ID = task_id
        archive_tasks.mark_task_running(task_id, process.pid)
        log_all(f"[消息回填] 已启动失败项重试 | request_id={task_id} | parent={parent_task_id} | pid={process.pid}")
        threading.Thread(
            target=_collect_output,
            args=(process, task_id),
            daemon=True,
            name=f"msg-backfill-{task_id}",
        ).start()
    _send_json_resp(handler, {
        "ok": True,
        "msg": "已启动失败项重试任务。",
        "request_id": task_id,
        "task_id": task_id,
        "parent_id": parent_task_id,
    }, 202)
