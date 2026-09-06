"""启动已有的成员博客归档工具，并把进度转入管理端日志。"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from src.logger import log_all
from src.webui_modules.archive.common import _send_json_resp

_ROOT = Path(__file__).resolve().parents[3]
_OFFICIAL_PATHS = {
    "nogizaka46.com": "/s/n46/diary/",
    "sakurazaka46.com": "/s/s46/diary/",
    "hinatazaka46.com": "/s/official/diary/",
}


def _valid_target(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").removeprefix("www.")
        prefix = _OFFICIAL_PATHS.get(host)
        if (parsed.scheme not in {"http", "https"} or not prefix
                or parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
            return False
        if not parsed.path.startswith(prefix):
            return False
        member = (parse_qs(parsed.query).get("ct") or [""])[0]
        return member.isascii() and member.isdigit()
    except ValueError:
        return False


def _collect_output(process, request_id: str) -> None:
    try:
        if process.stdout is not None:
            with process.stdout:
                for line in process.stdout:
                    if line.strip():
                        log_all(f"[博客回填] request_id={request_id} | {line.strip()}")
        code = process.wait()
        log_all(f"[博客回填] 进程结束 | request_id={request_id} | exit_code={code}", is_error=code != 0)
    except (OSError, ValueError) as exc:
        log_all(f"[博客回填] 日志读取失败 | request_id={request_id} | error={exc}", is_error=True)


def handle_member_backfill(handler, guard_fn, read_body_json_fn) -> None:
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
    url = body.get("url")
    translate = body.get("translate", False)
    if not isinstance(url, str) or len(url) > 2048 or not _valid_target(url.strip()):
        _send_json_resp(handler, {"ok": False, "msg": "请输入三坂官方成员博客列表页链接，需包含数字 ct 成员编号"}, 400)
        return
    if not isinstance(translate, bool):
        _send_json_resp(handler, {"ok": False, "msg": "translate 必须是布尔值"}, 400)
        return

    request_id = uuid.uuid4().hex[:12]
    script = _ROOT / "tools" / "archive_member.py"
    if not script.is_file():
        _send_json_resp(handler, {"ok": False, "msg": "服务器缺少博客归档工具，请检查部署文件"}, 503)
        return
    command = [sys.executable, "-u", str(script), url.strip()]
    if translate:
        command.append("--translate")
    try:
        process = subprocess.Popen(
            command, cwd=str(_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        log_all(f"[博客回填] 启动失败 | request_id={request_id} | error={exc}", is_error=True)
        _send_json_resp(handler, {"ok": False, "msg": "启动归档任务失败，请查看系统日志", "request_id": request_id}, 500)
        return
    log_all(f"[博客回填] 已启动 | request_id={request_id} | pid={process.pid} | translate={translate}")
    threading.Thread(target=_collect_output, args=(process, request_id), daemon=True,
                     name=f"blog-backfill-{request_id}").start()
    _send_json_resp(handler, {
        "ok": True, "msg": "已启动后台博客归档任务，可在管理后台系统日志中查看进度。",
        "request_id": request_id,
    }, 202)
