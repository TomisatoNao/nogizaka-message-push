"""启动博客归档工具，并把进度转入管理端日志。

博客归档是一个长时间运行的后台任务。成员 URL 补抓和三团全量归档
共享同一把进程锁，避免两个任务同时扫描官方站点、竞争 SQLite 写入或
让管理员误以为同一篇文章被重复处理。
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import sqlite3
from urllib.parse import parse_qs, urlsplit
import uuid

from src import archive_tasks
from src.logger import log_all
from src.webui_modules.archive.common import _send_json_resp

_ROOT = Path(__file__).resolve().parents[3]
_OFFICIAL_PATHS = {
    "nogizaka46.com": "/s/n46/diary/",
    "sakurazaka46.com": "/s/s46/diary/",
    "hinatazaka46.com": "/s/official/diary/",
}
BLOG_GROUP_KEYS = ("nogizaka", "sakurazaka", "hinatazaka")

# WebUI 进程内的博客归档任务锁。真正的实时博客监控不会被这把锁暂停，
# 但两个手动/后台回填任务不能同时启动。
_BACKFILL_LOCK = threading.Lock()
_ACTIVE_BACKFILL_PROC = None
_ACTIVE_BACKFILL_REQ_ID = ""
_ACTIVE_BACKFILL_KIND = ""


def _clear_active_locked() -> None:
    global _ACTIVE_BACKFILL_PROC, _ACTIVE_BACKFILL_REQ_ID, _ACTIVE_BACKFILL_KIND
    _ACTIVE_BACKFILL_PROC = None
    _ACTIVE_BACKFILL_REQ_ID = ""
    _ACTIVE_BACKFILL_KIND = ""


def _active_task_locked() -> tuple[str, str] | None:
    """返回正在运行的任务；发现已结束的伪进程/真实进程时清理状态。"""
    if _ACTIVE_BACKFILL_PROC is None:
        return None
    poll = getattr(_ACTIVE_BACKFILL_PROC, "poll", None)
    if callable(poll):
        try:
            if poll() is not None:
                _clear_active_locked()
                return None
        except (OSError, ValueError):
            # 无法查询状态时保守地认为任务仍在运行，避免并发启动。
            pass
    return _ACTIVE_BACKFILL_REQ_ID, _ACTIVE_BACKFILL_KIND


def _is_active_proc_running() -> bool:
    """兼容其他后台任务模块的状态探测接口。"""
    with _BACKFILL_LOCK:
        return _active_task_locked() is not None


def _normalize_groups(value) -> list[str] | None:
    """规范化三团选择并按固定顺序返回，拒绝空列表和未知键。"""
    if not isinstance(value, list) or not value:
        return None
    selected = set()
    for item in value:
        if not isinstance(item, str) or item not in BLOG_GROUP_KEYS:
            return None
        selected.add(item)
    return [key for key in BLOG_GROUP_KEYS if key in selected]


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
        archive_tasks.finalize_after_process_exit(request_id, code)
        log_all(f"[博客回填] 进程结束 | request_id={request_id} | exit_code={code}", is_error=code != 0)
    except (OSError, ValueError) as exc:
        log_all(f"[博客回填] 日志读取失败 | request_id={request_id} | error={exc}", is_error=True)
    finally:
        with _BACKFILL_LOCK:
            if _ACTIVE_BACKFILL_PROC is process:
                _clear_active_locked()


def _start_process(
    handler,
    command: list[str],
    request_id: str,
    kind: str,
    *,
    label: str,
    params: dict,
    task_precreated: bool = False,
) -> bool:
    """在共享锁内启动后台归档进程并注册清理线程。"""
    global _ACTIVE_BACKFILL_PROC, _ACTIVE_BACKFILL_REQ_ID, _ACTIVE_BACKFILL_KIND
    with _BACKFILL_LOCK:
        active = _active_task_locked()
        if active:
            active_id, active_kind = active
            if task_precreated:
                archive_tasks.fail_to_start(request_id, "已有博客归档进程仍在运行")
            _send_json_resp(handler, {
                "ok": False,
                "msg": "已有博客归档任务正在运行，请等待完成后再试",
                "request_id": active_id,
                "task": active_kind,
            }, 409)
            return False

        script = Path(command[2])
        if not script.is_file():
            if task_precreated:
                archive_tasks.fail_to_start(request_id, "服务器缺少博客归档工具")
            _send_json_resp(handler, {"ok": False, "msg": "服务器缺少博客归档工具，请检查部署文件"}, 503)
            return False
        if not task_precreated:
            try:
                archive_tasks.create_task(request_id, "blog_backfill", params)
            except archive_tasks.ActiveTaskExists as exc:
                _send_json_resp(handler, {
                    "ok": False,
                    "msg": "已有博客归档任务正在运行，请等待完成后再试",
                    "request_id": exc.task_id,
                    "task_id": exc.task_id,
                }, 409)
                return False
            except (OSError, sqlite3.Error):
                _send_json_resp(handler, {"ok": False, "msg": "归档任务存储暂时不可用，请查看服务端日志"}, 503)
                return False
        if "--task-id" not in command:
            task_id_args = ["--task-id", request_id]
            try:
                download_option = command.index("--download-images")
            except ValueError:
                command.extend(task_id_args)
            else:
                command[download_option:download_option] = task_id_args
        try:
            process = subprocess.Popen(
                command, cwd=str(_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            archive_tasks.fail_to_start(request_id, str(exc))
            log_all(f"[博客回填] 启动失败 | request_id={request_id} | error={exc}", is_error=True)
            _send_json_resp(handler, {
                "ok": False, "msg": "启动归档任务失败，请查看系统日志", "request_id": request_id, "task_id": request_id,
            }, 500)
            return False

        _ACTIVE_BACKFILL_PROC = process
        _ACTIVE_BACKFILL_REQ_ID = request_id
        _ACTIVE_BACKFILL_KIND = kind
        archive_tasks.mark_task_running(request_id, process.pid)
        log_all(
            f"[博客回填] 已启动 | request_id={request_id} | task={kind} | "
            f"pid={process.pid} | {label}"
        )
        threading.Thread(
            target=_collect_output, args=(process, request_id), daemon=True,
            name=f"blog-backfill-{request_id}",
        ).start()
    _send_json_resp(handler, {
        "ok": True, "msg": "已启动后台博客归档任务，可在归档任务中心查看进度。",
        "request_id": request_id, "task_id": request_id,
    }, 202)
    return True


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
    command = [sys.executable, "-u", str(script), url.strip()]
    if translate:
        command.append("--translate")
    _start_process(
        handler, command, request_id, "member_url",
        label=f"translate={translate}",
        params={"kind": "member_url", "url": url.strip(), "translate": translate},
    )


def handle_group_backfill(handler, guard_fn, read_body_json_fn) -> None:
    """启动选定团体的全量博客归档（正文 + 本地图片，不自动翻译）。"""
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
    groups = _normalize_groups(body.get("groups"))
    if not groups:
        _send_json_resp(handler, {
            "ok": False,
            "msg": "groups 必须是至少包含一个有效团体的数组",
            "allowed": list(BLOG_GROUP_KEYS),
        }, 400)
        return

    request_id = uuid.uuid4().hex[:12]
    script = _ROOT / "tools" / "backfill_blogs.py"
    command = [sys.executable, "-u", str(script)]
    for group in groups:
        command.extend(["--group", group])
    command.append("--download-images")
    _start_process(
        handler, command, request_id, "groups",
        label=f"groups={','.join(groups)} | translate=false | download_images=true",
        params={"kind": "groups", "groups": groups, "download_images": True},
    )


def retry_blog_task(handler, parent_task_id: str) -> None:
    """Create one child task from failed blog items and relaunch its worker."""
    global _ACTIVE_BACKFILL_PROC, _ACTIVE_BACKFILL_REQ_ID, _ACTIVE_BACKFILL_KIND
    with _BACKFILL_LOCK:
        active = _active_task_locked()
        if active:
            active_id, active_kind = active
            _send_json_resp(handler, {
                "ok": False,
                "msg": "已有博客归档任务正在运行，请等待完成后再重试",
                "request_id": active_id,
                "task_id": active_id,
                "task": active_kind,
            }, 409)
            return
        request_id = archive_tasks.new_task_id()
        try:
            task, _items = archive_tasks.create_retry_task(parent_task_id, request_id)
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
                "msg": "已有博客归档任务正在运行，请等待完成后再重试",
                "request_id": exc.task_id,
                "task_id": exc.task_id,
            }, 409)
            return
        except archive_tasks.TaskNotRetryable:
            _send_json_resp(handler, {"ok": False, "msg": "此任务类型不支持失败项重试"}, 400)
            return

    params = task.get("params", {})
    if params.get("kind") == "member_url":
        script = _ROOT / "tools" / "archive_member.py"
        command = [sys.executable, "-u", str(script), str(params.get("url") or "")]
        if params.get("translate") is True:
            command.append("--translate")
        label = "failed member retry"
        kind = "member_url"
    else:
        script = _ROOT / "tools" / "backfill_blogs.py"
        command = [sys.executable, "-u", str(script)]
        for group in params.get("groups", []):
            if group in BLOG_GROUP_KEYS:
                command.extend(["--group", group])
        if params.get("download_images") is True:
            command.append("--download-images")
        label = f"failed group items | parent={parent_task_id}"
        kind = "groups"
    command.extend(["--task-id", request_id])
    _start_process(
        handler,
        command,
        request_id,
        kind,
        label=label,
        params=params,
        task_precreated=True,
    )
