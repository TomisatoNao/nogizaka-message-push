"""启动已有消息归档回填工具 (tools/backfill_archive.py)，并把进度转入管理端日志。"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import uuid

from src.logger import log_all
from src.webui_modules.archive.common import _send_json_resp

_ROOT = Path(__file__).resolve().parents[3]

_BACKFILL_LOCK = threading.Lock()
_ACTIVE_BACKFILL_PROC: subprocess.Popen | None = None
_ACTIVE_BACKFILL_REQ_ID: str | None = None


def _is_active_proc_running() -> bool:
    global _ACTIVE_BACKFILL_PROC
    if _ACTIVE_BACKFILL_PROC is None:
        return False
    poll_fn = getattr(_ACTIVE_BACKFILL_PROC, "poll", None)
    if callable(poll_fn):
        return poll_fn() is None
    return False


def _collect_output(process, request_id: str) -> None:
    global _ACTIVE_BACKFILL_PROC, _ACTIVE_BACKFILL_REQ_ID
    try:
        if process.stdout is not None:
            with process.stdout:
                for line in process.stdout:
                    if line.strip():
                        log_all(f"[消息回填] request_id={request_id} | {line.strip()}")
        code = process.wait()
        log_all(f"[消息回填] 进程结束 | request_id={request_id} | exit_code={code}", is_error=code != 0)
    except (OSError, ValueError) as exc:
        log_all(f"[消息回填] 日志读取失败 | request_id={request_id} | error={exc}", is_error=True)
    finally:
        with _BACKFILL_LOCK:
            if _ACTIVE_BACKFILL_PROC is process:
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

    request_id = uuid.uuid4().hex[:12]
    script = _ROOT / "tools" / "backfill_archive.py"
    if not script.is_file():
        _send_json_resp(handler, {"ok": False, "msg": "服务器缺少消息回填工具，请检查部署文件"}, 503)
        return

    # 从 WebUI 启动必须带 --force 跳过主程序运行检测
    command = [sys.executable, "-u", str(script), "--force"]
    if cleaned_from:
        command.extend(["--from", cleaned_from])
    cleaned_member = member.strip()
    if cleaned_member:
        tokens = [t.strip() for t in re.split(r"[,，、;；\n]+", cleaned_member) if t.strip()]
        if tokens:
            command.extend(tokens)
    if reset:
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
        log_all(f"[消息回填] 启动失败 | request_id={request_id} | error={exc}", is_error=True)
        _send_json_resp(handler, {"ok": False, "msg": "启动回填任务失败，请查看系统日志", "request_id": request_id}, 500)
        return

    with _BACKFILL_LOCK:
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
            "msg": "已成功启动消息归档回填任务，可在管理后台系统日志中查看进度。",
            "request_id": request_id,
        },
        202,
    )
