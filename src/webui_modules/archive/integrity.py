"""Read-only archive integrity checks for administrator initiated tasks."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import threading

from src import archive_tasks

TASK_KIND = "archive_integrity"
_REPORT_ITEM_TYPE = "integrity_report"
_REPORT_ITEM_KEY = "report"
_MAX_ISSUES = 500
_PROGRESS_INTERVAL = 500
_SCAN_FAILURE_CODES = frozenset({
    "archive_scan_failed",
    "archive_symlink_directory_skipped",
    "message_json_read_failed",
    "message_json_symlink_skipped",
    "message_json_not_regular",
})
_ALLOWED_STATUSES = frozenset({
    "not_checked", "healthy", "needs_repair", "check_failed", "unavailable",
})
_WORKER_LOCK = threading.RLock()
_WORKERS_BY_TASK: dict[str, threading.Thread] = {}
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_COVERAGE = [
    "消息月 JSON 结构与只读 archive.db messages 索引对照",
    "archive.db 与 blogs.db 的 SQLite quick_check",
    "消息 _local_file 与博客 image_paths_json 的本地文件引用",
    "归档索引 dirty marker",
]
_NOT_COVERED = [
    "远程媒体 URL 可访问性、文件内容哈希与媒体格式有效性",
    "FTS 索引内容正确性",
    "任何自动修复或归档数据写入",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_identifier(value: object, *, limit: int = 120) -> str | None:
    text = str(value or "")
    if not text or len(text) > limit:
        return None
    if any(not (char.isalnum() or char in "._- ") for char in text):
        return None
    return text


def _safe_year_month(year: object, month: object) -> tuple[int | None, int | None]:
    try:
        safe_year = int(year)
    except (TypeError, ValueError, OverflowError):
        safe_year = None
    try:
        safe_month = int(month)
    except (TypeError, ValueError, OverflowError):
        safe_month = None
    return safe_year, safe_month


def _new_report(*, status: str = "not_checked") -> dict:
    return {
        "status": status,
        "started_at": None,
        "finished_at": None,
        "progress": {"phase": "queued", "done": 0, "total": 0},
        "checks": [],
        "issue_count": 0,
        "omitted_issue_count": 0,
        "_scan_failure_count": 0,
        "issues": [],
        "coverage": list(_COVERAGE),
        "not_covered": list(_NOT_COVERED),
    }


def _add_issue(report: dict, code: str, **location: object) -> None:
    issue: dict[str, object] = {"code": code}
    member = _safe_identifier(location.get("member"))
    if member is not None:
        issue["member"] = member
    year, month = _safe_year_month(location.get("year"), location.get("month"))
    if year is not None:
        issue["year"] = year
    if month is not None:
        issue["month"] = month
    msg_id = _safe_identifier(location.get("msg_id"))
    if msg_id is not None:
        issue["msg_id"] = msg_id
    try:
        blog_id = int(location.get("blog_id"))
    except (TypeError, ValueError, OverflowError):
        blog_id = None
    if blog_id is not None and blog_id >= 0:
        issue["blog_id"] = blog_id

    report["issue_count"] += 1
    if code in _SCAN_FAILURE_CODES:
        report["_scan_failure_count"] = int(report.get("_scan_failure_count") or 0) + 1
    if len(report["issues"]) < _MAX_ISSUES:
        report["issues"].append(issue)
    else:
        report["omitted_issue_count"] += 1


def _set_check(report: dict, code: str, status: str, **counts: int) -> None:
    entry = {"code": code, "status": status}
    entry.update({key: max(0, int(value)) for key, value in counts.items()})
    report["checks"] = [item for item in report["checks"] if item["code"] != code]
    report["checks"].append(entry)


def _report_status(report: dict) -> str:
    states = {str(check.get("status")) for check in report.get("checks", [])}
    if "check_failed" in states:
        return "check_failed"
    if int(report.get("issue_count") or 0):
        return "needs_repair"
    if "unavailable" in states:
        return "unavailable"
    return "healthy"


def _sanitize_report(raw: object) -> dict:
    """Return the narrow public report contract, never arbitrary stored content."""
    if not isinstance(raw, dict):
        return _new_report()
    raw_status = raw.get("status")
    result = _new_report(status=raw_status if isinstance(raw_status, str) and raw_status in _ALLOWED_STATUSES else "not_checked")
    result["started_at"] = raw.get("started_at") if isinstance(raw.get("started_at"), str) else None
    result["finished_at"] = raw.get("finished_at") if isinstance(raw.get("finished_at"), str) else None
    progress = raw.get("progress") if isinstance(raw.get("progress"), dict) else {}
    result["progress"] = {
        "phase": _safe_identifier(progress.get("phase"), limit=80) or "",
        "done": max(0, int(progress.get("done") or 0)),
        "total": max(0, int(progress.get("total") or 0)),
    }
    checks = []
    for check in raw.get("checks", []):
        if not isinstance(check, dict):
            continue
        code = _safe_identifier(check.get("code"), limit=80)
        status = check.get("status")
        if code and isinstance(status, str) and status in _ALLOWED_STATUSES:
            checks.append({
                "code": code,
                "status": status,
                **{
                    key: max(0, int(check.get(key) or 0))
                    for key in ("checked", "missing", "mismatched", "invalid", "unavailable")
                    if key in check
                },
            })
    result["checks"] = checks
    result["issue_count"] = max(0, int(raw.get("issue_count") or 0))
    result["omitted_issue_count"] = max(0, int(raw.get("omitted_issue_count") or 0))
    issues = []
    for issue in raw.get("issues", []):
        if not isinstance(issue, dict):
            continue
        code = _safe_identifier(issue.get("code"), limit=80)
        if not code:
            continue
        clean: dict[str, object] = {"code": code}
        member = _safe_identifier(issue.get("member"))
        if member is not None:
            clean["member"] = member
        year, month = _safe_year_month(issue.get("year"), issue.get("month"))
        if year is not None:
            clean["year"] = year
        if month is not None:
            clean["month"] = month
        msg_id = _safe_identifier(issue.get("msg_id"))
        if msg_id is not None:
            clean["msg_id"] = msg_id
        try:
            blog_id = int(issue.get("blog_id"))
        except (TypeError, ValueError, OverflowError):
            blog_id = None
        if blog_id is not None and blog_id >= 0:
            clean["blog_id"] = blog_id
        issues.append(clean)
    result["issues"] = issues[:_MAX_ISSUES]
    return result


def _persist_report(task_id: str, report: dict, *, item_status: str | None = None) -> None:
    task = archive_tasks.get_task(task_id)
    if task:
        report["progress"] = {
            "phase": str(task.get("phase") or ""),
            "done": max(0, int(task.get("done") or 0)),
            "total": max(0, int(task.get("total") or 0)),
        }
    safe_report = _sanitize_report(report)
    if item_status is None:
        archive_tasks.update_item_payload(task_id, _REPORT_ITEM_TYPE, _REPORT_ITEM_KEY, safe_report)
    else:
        archive_tasks.set_item(task_id, _REPORT_ITEM_TYPE, _REPORT_ITEM_KEY, item_status, payload=safe_report)


def _stored_report(task_id: str) -> dict:
    for item in archive_tasks.get_items_readonly(task_id):
        if item.get("type") == _REPORT_ITEM_TYPE and item.get("key") == _REPORT_ITEM_KEY:
            return _sanitize_report(item.get("payload"))
    return _new_report()


def _public_task(task: dict | None, *, include_report: bool = False) -> dict | None:
    if not task:
        return None
    public = {
        "id": _safe_identifier(task.get("task_id"), limit=12) or "",
        "kind": TASK_KIND,
        "scope": "archive",
        "status": _safe_identifier(task.get("status"), limit=40) or "",
        "phase": _safe_identifier(task.get("phase"), limit=80) or "",
        "done": max(0, int(task.get("done") or 0)),
        "total": max(0, int(task.get("total") or 0)),
        "progress": {
            "phase": _safe_identifier(task.get("phase"), limit=80) or "",
            "done": max(0, int(task.get("done") or 0)),
            "total": max(0, int(task.get("total") or 0)),
        },
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "updated_at": task.get("updated_at"),
        "heartbeat_at": task.get("heartbeat_at"),
        "finished_at": task.get("finished_at"),
    }
    if include_report:
        public["report"] = _report_for_task(task)
    return public


def _report_for_task(task: dict) -> dict:
    """Project terminal worker failures into the report without mutating task data."""
    task_id = str(task.get("task_id") or "")
    report = _stored_report(task_id)
    status = task.get("status")
    if status not in {"failed", "interrupted"}:
        return report

    issue_code = "integrity_worker_interrupted" if status == "interrupted" else "integrity_worker_failed"
    _set_check(report, "integrity_worker", "check_failed", checked=0, invalid=1)
    worker_issue_codes = {
        "integrity_worker_failed",
        "integrity_worker_interrupted",
        "integrity_worker_failed_to_start",
    }
    if not any(issue.get("code") in worker_issue_codes for issue in report["issues"]):
        _add_issue(report, issue_code)
    report["status"] = "check_failed"
    report["finished_at"] = (
        task.get("finished_at") if isinstance(task.get("finished_at"), str)
        else task.get("updated_at") if isinstance(task.get("updated_at"), str)
        else report.get("finished_at")
    )
    return report


def _heartbeat_is_stale(task: dict) -> bool:
    timestamp = task.get("heartbeat_at") or task.get("updated_at") or task.get("created_at")
    if not isinstance(timestamp, str) or not timestamp:
        return True
    try:
        stamp = datetime.fromisoformat(timestamp)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return True
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    return age > archive_tasks.STALE_AFTER_SECONDS


def _is_current_process_task(task: dict) -> bool:
    try:
        pid = int(task.get("pid") or 0)
        marker = task.get("process_marker")
        current_marker = archive_tasks._process_marker(os.getpid())
    except (TypeError, ValueError, OSError):
        return False
    return pid == os.getpid() and bool(marker) and bool(current_marker) and marker == current_marker


def _worker_thread_for(task_id: str) -> threading.Thread | None:
    with _WORKER_LOCK:
        thread = _WORKERS_BY_TASK.get(task_id)
        if thread is not None:
            return thread
        expected_name = f"archive-integrity-{task_id}"
        return next((item for item in threading.enumerate() if item.name == expected_name), None)


def _active_worker_state(task: dict) -> dict[str, object]:
    stale = _heartbeat_is_stale(task)
    if not stale:
        state = "running" if task.get("status") == "running" else "starting"
        return {
            "heartbeat_stale": False,
            "worker_state": state,
            "restart_blocked": False,
            "restart_reason": "",
        }

    task_id = str(task.get("task_id") or "")
    thread = _worker_thread_for(task_id)
    if thread is not None and thread.is_alive():
        return {
            "heartbeat_stale": True,
            "worker_state": "alive_stale",
            "restart_blocked": True,
            "restart_reason": "heartbeat_stale_worker_alive",
        }
    if _is_current_process_task(task):
        return {
            "heartbeat_stale": True,
            "worker_state": "exited",
            "restart_blocked": False,
            "restart_reason": "heartbeat_stale_worker_exited",
        }

    raw_pid = task.get("pid")
    if raw_pid:
        try:
            pid = int(raw_pid)
            alive = archive_tasks._process_is_alive(pid, task.get("process_marker"))
        except (TypeError, ValueError, OSError):
            alive = True
        if alive:
            return {
                "heartbeat_stale": True,
                "worker_state": "alive_or_owner_uncertain",
                "restart_blocked": True,
                "restart_reason": "worker_alive_or_owner_uncertain",
            }
        return {
            "heartbeat_stale": True,
            "worker_state": "process_exited",
            "restart_blocked": False,
            "restart_reason": "worker_process_exited",
        }

    if task.get("status") == "queued" and thread is None:
        return {
            "heartbeat_stale": True,
            "worker_state": "not_started",
            "restart_blocked": False,
            "restart_reason": "queued_worker_not_started",
        }
    return {
        "heartbeat_stale": True,
        "worker_state": "unknown",
        "restart_blocked": True,
        "restart_reason": "worker_owner_uncertain",
    }


def _public_active_task(task: dict | None) -> dict | None:
    public = _public_task(task, include_report=True)
    if public is not None and task is not None:
        public.update(_active_worker_state(task))
    return public


def _interrupt_abandoned_task(task: dict) -> bool:
    task_id = str(task.get("task_id") or "")
    if not task_id or not archive_tasks.interrupt_task(task_id, "integrity_worker_interrupted"):
        return False
    report = _stored_report(task_id)
    _set_check(report, "integrity_worker", "check_failed", checked=0, invalid=1)
    if not any(issue.get("code") == "integrity_worker_interrupted" for issue in report["issues"]):
        _add_issue(report, "integrity_worker_interrupted")
    report["status"] = "check_failed"
    report["finished_at"] = _now()
    _persist_report(task_id, report, item_status="completed")
    return True


def get_integrity_overview() -> dict:
    """Return the latest completed report and active check without starting work."""
    latest_report_task = archive_tasks.get_latest_task(
        TASK_KIND,
        statuses=("completed", "partial", "failed", "interrupted"),
        recover=False,
    )
    active = archive_tasks.get_latest_task(TASK_KIND, statuses=("queued", "running"), recover=False)
    latest_report = None
    if latest_report_task:
        latest_report = _report_for_task(latest_report_task)
    return {
        "ok": True,
        "latest_report": latest_report,
        "current_task": _public_active_task(active),
    }


def _readonly_connect(path: Path) -> sqlite3.Connection:
    uri = path.absolute().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _quick_check(conn: sqlite3.Connection) -> tuple[bool, int]:
    rows = conn.execute("PRAGMA quick_check").fetchall()
    ok = bool(rows) and all(str(row[0]).casefold() == "ok" for row in rows)
    return ok, len(rows)


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _relative_entry_state(root: Path, relative: str) -> str:
    """Inspect a root-relative path component-by-component without following links."""
    normalized = relative.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or re.match(r"^[a-zA-Z]:", normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
        or any(":" in part for part in normalized.split("/"))
        or "\x00" in normalized
    ):
        return "unsafe"
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "error"
    root_mode = root_info.st_mode
    if stat.S_ISLNK(root_mode) or _is_reparse_point(root_info):
        return "unsafe"
    if not stat.S_ISDIR(root_mode):
        return "error"
    candidate = root
    parts = normalized.split("/")
    for index, part in enumerate(parts):
        candidate = candidate / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "error"
        mode = info.st_mode
        if stat.S_ISLNK(mode) or _is_reparse_point(info):
            return "unsafe"
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            return "missing"
        if index == len(parts) - 1:
            if stat.S_ISREG(mode):
                return "file"
            if stat.S_ISDIR(mode):
                return "directory"
            return "invalid"
    return "missing"


def _stat_signature(info: os.stat_result) -> tuple[object, ...]:
    mode = info.st_mode
    if stat.S_ISLNK(mode) or _is_reparse_point(info):
        kind = "symlink"
    elif stat.S_ISDIR(mode):
        kind = "directory"
    elif stat.S_ISREG(mode):
        kind = "file"
    else:
        kind = "invalid"
    return (
        kind,
        info.st_mtime_ns,
        info.st_size,
        getattr(info, "st_dev", 0),
        getattr(info, "st_ino", 0),
    )


def _snapshot_path(
    snapshot: dict[tuple[str, ...], tuple[object, ...]],
    key: tuple[str, ...],
    path: Path,
) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        snapshot[key] = ("missing",)
        return None
    except OSError:
        snapshot[key] = ("error",)
        return None
    snapshot[key] = _stat_signature(info)
    return info


def _archive_month_files(
    root: Path,
    report: dict,
    snapshot: dict[tuple[str, ...], tuple[object, ...]],
) -> list[tuple[str, int, int, Path]]:
    files: list[tuple[str, int, int, Path]] = []
    root_info = _snapshot_path(snapshot, ("root",), root)
    if root_info is None:
        _add_issue(report, "archive_scan_failed")
        _set_check(report, "message_json_scan", "check_failed", checked=0, invalid=1)
        return files
    if stat.S_ISLNK(root_info.st_mode) or _is_reparse_point(root_info):
        _add_issue(report, "archive_symlink_directory_skipped")
        _set_check(report, "message_json_scan", "check_failed", checked=0, invalid=1)
        return files
    if not stat.S_ISDIR(root_info.st_mode):
        _add_issue(report, "archive_scan_failed")
        _set_check(report, "message_json_scan", "check_failed", checked=0, invalid=1)
        return files
    try:
        members = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        _add_issue(report, "archive_scan_failed")
        _set_check(report, "message_json_scan", "check_failed", checked=0, invalid=1)
        return files
    for member_dir in members:
        if member_dir.name.startswith("."):
            continue
        member_key = ("member", member_dir.name)
        try:
            member_info = member_dir.lstat()
        except FileNotFoundError:
            snapshot[member_key] = ("missing",)
            _add_issue(report, "archive_scan_failed", member=member_dir.name)
            continue
        except OSError:
            snapshot[member_key] = ("error",)
            _add_issue(report, "archive_scan_failed", member=member_dir.name)
            continue
        mode = member_info.st_mode
        if stat.S_ISLNK(mode) or _is_reparse_point(member_info):
            snapshot[member_key] = _stat_signature(member_info)
            _add_issue(report, "archive_symlink_directory_skipped", member=member_dir.name)
            continue
        if not stat.S_ISDIR(mode):
            continue
        snapshot[member_key] = _stat_signature(member_info)
        member = member_dir.name
        try:
            year_dirs = sorted(member_dir.iterdir(), key=lambda item: item.name)
        except OSError:
            _add_issue(report, "archive_scan_failed", member=member)
            continue
        for year_dir in year_dirs:
            if not year_dir.name.isdigit():
                continue
            year_info = _snapshot_path(snapshot, ("year", member, year_dir.name), year_dir)
            if (
                year_info is None
                or stat.S_ISLNK(year_info.st_mode)
                or _is_reparse_point(year_info)
                or not stat.S_ISDIR(year_info.st_mode)
            ):
                _add_issue(report, "archive_symlink_directory_skipped", member=member)
                continue
            try:
                year = int(year_dir.name)
                month_dirs = sorted(year_dir.iterdir(), key=lambda item: item.name)
            except (OSError, ValueError, OverflowError):
                _add_issue(report, "archive_scan_failed", member=member)
                continue
            for month_dir in month_dirs:
                if not month_dir.name.isdigit():
                    continue
                month_info = _snapshot_path(snapshot, ("month", member, year_dir.name, month_dir.name), month_dir)
                if (
                    month_info is None
                    or stat.S_ISLNK(month_info.st_mode)
                    or _is_reparse_point(month_info)
                    or not stat.S_ISDIR(month_info.st_mode)
                ):
                    _add_issue(report, "archive_symlink_directory_skipped", member=member, year=year)
                    continue
                try:
                    month = int(month_dir.name)
                except (ValueError, OverflowError):
                    _add_issue(report, "archive_scan_failed", member=member, year=year)
                    continue
                if not 1 <= month <= 12:
                    _add_issue(report, "archive_month_out_of_range", member=member, year=year, month=month)
                json_path = month_dir / "messages.json"
                json_key = ("json", member, year_dir.name, month_dir.name)
                json_info = _snapshot_path(snapshot, json_key, json_path)
                if json_info is None:
                    if snapshot[json_key][0] != "missing":
                        _add_issue(report, "message_json_read_failed", member=member, year=year, month=month)
                    continue
                file_mode = json_info.st_mode
                if stat.S_ISLNK(file_mode) or _is_reparse_point(json_info):
                    _add_issue(report, "message_json_symlink_skipped", member=member, year=year, month=month)
                    continue
                if stat.S_ISREG(file_mode):
                    files.append((member, year, month, json_path))
                else:
                    _add_issue(report, "message_json_not_regular", member=member, year=year, month=month)
    return files


def _archive_layout_changed(
    before: dict[tuple[str, ...], tuple[object, ...]],
    after: dict[tuple[str, ...], tuple[object, ...]],
) -> bool:
    if before.keys() != after.keys():
        return True
    for key, previous in before.items():
        current = after[key]
        if previous[0] != current[0]:
            return True
        if key == ("root",):
            if len(previous) >= 5 and len(current) >= 5 and previous[3:5] != current[3:5]:
                return True
            continue
        if key[0] == "json" and previous[0] == "file" and previous[1:] != current[1:]:
            return True
        if previous[0] in {"directory", "symlink"} and previous[1:] != current[1:]:
            return True
        if len(previous) >= 5 and len(current) >= 5 and previous[3:5] != current[3:5]:
            return True
    return False


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reference_state(root: Path, relative: object) -> str:
    """Check one relative local reference without following symlinks or escaping root."""
    if not isinstance(relative, str) or not relative.strip():
        return "invalid"
    state = _relative_entry_state(root, relative)
    if state == "file":
        return "present"
    if state == "directory" or state == "invalid":
        return "missing"
    return state


def _scan_messages(root: Path, report: dict, task_id: str) -> None:
    """Scan monthly JSON and compare each month to the index using bounded memory."""
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        _set_check(report, "message_json_scan", "unavailable", checked=0, invalid=0)
        _set_check(report, "archive_db_quick_check", "unavailable", checked=0, unavailable=1)
        _set_check(report, "message_index_comparison", "unavailable", checked=0, missing=0, mismatched=0)
        return
    except OSError:
        _add_issue(report, "archive_scan_failed")
        _set_check(report, "message_json_scan", "check_failed", checked=0, invalid=1)
        _set_check(report, "archive_db_quick_check", "unavailable", checked=0, unavailable=1)
        _set_check(report, "message_index_comparison", "unavailable", checked=0, missing=0, mismatched=0)
        return
    if stat.S_ISLNK(root_info.st_mode) or _is_reparse_point(root_info):
        _add_issue(report, "archive_symlink_directory_skipped")
        _set_check(report, "message_json_scan", "check_failed", checked=0, invalid=1)
        _set_check(report, "archive_db_quick_check", "unavailable", checked=0, unavailable=1)
        _set_check(report, "message_index_comparison", "unavailable", checked=0, missing=0, mismatched=0)
        return
    if not stat.S_ISDIR(root_info.st_mode):
        _set_check(report, "message_json_scan", "unavailable", checked=0, invalid=0)
        _set_check(report, "archive_db_quick_check", "unavailable", checked=0, unavailable=1)
        _set_check(report, "message_index_comparison", "unavailable", checked=0, missing=0, mismatched=0)
        return

    scan_failure_start = int(report.get("_scan_failure_count") or 0)
    layout_before: dict[tuple[str, ...], tuple[object, ...]] = {}
    files = _archive_month_files(root, report, layout_before)
    json_months: set[tuple[str, int, int]] = set()
    json_fingerprints: list[tuple[str, int, int, Path, tuple[int, int, int]]] = []
    checked_messages = 0
    json_invalid = 0
    media_missing = 0
    media_unsafe = 0
    media_failed = 0
    index_missing = 0
    index_mismatched = 0
    index_checked = 0
    month_compared = 0
    index_state = "unavailable"
    conn = None
    index_count = 0
    archive_data_version: int | None = None
    json_changed_during_check = False

    db_state = _relative_entry_state(root, "archive.db")
    if db_state == "file":
        try:
            conn = _readonly_connect(root / "archive.db")
            archive_data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
            quick_ok, quick_rows = _quick_check(conn)
            if quick_ok:
                _set_check(report, "archive_db_quick_check", "healthy", checked=quick_rows)
            else:
                _set_check(report, "archive_db_quick_check", "check_failed", checked=quick_rows, invalid=1)
                _add_issue(report, "archive_db_quick_check_failed")
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "messages" in tables:
                index_count = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] or 0)
                index_state = "available"
            else:
                _set_check(report, "message_index_comparison", "unavailable", checked=0, missing=0, mismatched=0)
        except (sqlite3.Error, OSError):
            _set_check(report, "archive_db_quick_check", "check_failed", checked=0, invalid=1)
            _set_check(report, "message_index_comparison", "check_failed", checked=0, missing=0, mismatched=0)
            _add_issue(report, "archive_db_read_failed")
            index_state = "failed"
            if conn is not None:
                conn.close()
                conn = None
    elif db_state == "missing":
        _set_check(report, "archive_db_quick_check", "unavailable", checked=0, unavailable=1)
    else:
        _set_check(report, "archive_db_quick_check", "check_failed", checked=0, invalid=1)
        _add_issue(report, "archive_db_path_uncheckable")
        index_state = "failed"

    try:
        for member, year, month, json_path in files:
            month_key = (member, year, month)
            json_months.add(month_key)
            try:
                before = json_path.lstat()
                if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before):
                    json_invalid += 1
                    json_changed_during_check = True
                    _add_issue(report, "message_json_symlink_skipped", member=member, year=year, month=month)
                    continue
                if not stat.S_ISREG(before.st_mode):
                    json_invalid += 1
                    json_changed_during_check = True
                    _add_issue(report, "message_json_not_regular", member=member, year=year, month=month)
                    continue
                records = json.loads(json_path.read_text(encoding="utf-8"))
                after = json_path.lstat()
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                json_invalid += 1
                _add_issue(report, "message_json_invalid", member=member, year=year, month=month)
                continue
            if (
                stat.S_ISLNK(after.st_mode)
                or _is_reparse_point(after)
                or not stat.S_ISREG(after.st_mode)
                or (before.st_mtime_ns, before.st_size, getattr(before, "st_ino", 0)) != (
                after.st_mtime_ns, after.st_size, getattr(after, "st_ino", 0),
                )
            ):
                json_invalid += 1
                json_changed_during_check = True
                _add_issue(report, "message_json_changed_during_check", member=member, year=year, month=month)
                continue
            json_fingerprints.append((
                member,
                year,
                month,
                json_path,
                (after.st_mtime_ns, after.st_size, getattr(after, "st_ino", 0)),
            ))
            if not isinstance(records, list):
                json_invalid += 1
                _add_issue(report, "message_json_invalid", member=member, year=year, month=month)
                continue

            expected: dict[str, tuple[str, str]] = {}
            for record in records:
                checked_messages += 1
                if not isinstance(record, dict) or record.get("id") is None or not str(record.get("id")):
                    json_invalid += 1
                    _add_issue(report, "message_json_record_invalid", member=member, year=year, month=month)
                    continue
                msg_id = str(record.get("id"))
                if msg_id in expected:
                    json_invalid += 1
                    _add_issue(report, "duplicate_message_id", member=member, year=year, month=month, msg_id=msg_id)
                    continue
                try:
                    digest = _canonical_digest(record)
                except (TypeError, ValueError, UnicodeError):
                    digest = ""
                    json_invalid += 1
                    _add_issue(report, "message_json_record_invalid", member=member, year=year, month=month, msg_id=msg_id)
                local_file = record.get("_local_file") or ""
                if not isinstance(local_file, str):
                    local_file = ""
                    json_invalid += 1
                    _add_issue(report, "message_local_file_invalid", member=member, year=year, month=month, msg_id=msg_id)
                expected[msg_id] = (digest, local_file)

                if not local_file and record.get("_download_failed") is True:
                    media_failed += 1
                    _add_issue(report, "message_media_download_failed", member=member, year=year, month=month, msg_id=msg_id)
                if local_file:
                    state = _reference_state(root, f"{member}/{local_file}")
                    if state == "missing":
                        media_missing += 1
                        _add_issue(report, "message_media_missing", member=member, year=year, month=month, msg_id=msg_id)
                    elif state == "unsafe":
                        media_unsafe += 1
                        _add_issue(report, "message_media_reference_unsafe", member=member, year=year, month=month, msg_id=msg_id)
                    elif state in {"invalid", "error"}:
                        json_invalid += 1
                        _add_issue(report, "message_media_reference_uncheckable", member=member, year=year, month=month, msg_id=msg_id)

                if checked_messages % _PROGRESS_INTERVAL == 0:
                    archive_tasks.set_progress(task_id, "message_json_scan", checked_messages, 0)

            if index_state == "available" and conn is not None:
                try:
                    rows = conn.execute(
                        "SELECT id, member_dir, year, month, local_file, raw_json FROM messages "
                        "WHERE member_dir=? AND year=? AND month=?",
                        (member, year, month),
                    ).fetchall()
                except sqlite3.Error:
                    index_state = "failed"
                    _set_check(report, "message_index_comparison", "check_failed", checked=index_checked, missing=index_missing, mismatched=index_mismatched)
                    _add_issue(report, "archive_db_read_failed")
                    continue
                by_id = {str(row["id"] or ""): row for row in rows}
                for msg_id, (digest, local_file) in expected.items():
                    row = by_id.pop(msg_id, None)
                    if row is None:
                        index_missing += 1
                        month_compared += 1
                        _add_issue(report, "message_missing_from_index", member=member, year=year, month=month, msg_id=msg_id)
                        continue
                    month_compared += 1
                    try:
                        indexed_record = json.loads(row["raw_json"] or "")
                        content_matches = isinstance(indexed_record, dict) and _canonical_digest(indexed_record) == digest
                    except (TypeError, ValueError, json.JSONDecodeError, UnicodeError):
                        content_matches = False
                    if str(row["local_file"] or "") != local_file or not content_matches:
                        index_mismatched += 1
                        _add_issue(report, "message_index_entry_mismatch", member=member, year=year, month=month, msg_id=msg_id)
                for msg_id in by_id:
                    index_missing += 1
                    month_compared += 1
                    _add_issue(report, "message_index_entry_without_json", member=member, year=year, month=month, msg_id=msg_id)
                if month_compared % _PROGRESS_INTERVAL == 0:
                    archive_tasks.set_progress(task_id, "message_index_month_comparison", month_compared, 0)

        if index_state == "available" and conn is not None:
            try:
                rows = conn.execute("SELECT id, member_dir, year, month FROM messages")
                for row in rows:
                    index_checked += 1
                    member = str(row["member_dir"] or "")
                    year, month = _safe_year_month(row["year"], row["month"])
                    if year is not None and month is not None and (member, year, month) not in json_months:
                        index_missing += 1
                        _add_issue(
                            report,
                            "message_index_entry_without_json",
                            member=member,
                            year=year,
                            month=month,
                            msg_id=row["id"],
                        )
                    if index_checked % _PROGRESS_INTERVAL == 0:
                        archive_tasks.set_progress(task_id, "message_index_orphans", index_checked, index_count)
            except sqlite3.Error:
                index_state = "failed"
                _add_issue(report, "archive_db_read_failed")

        for member, year, month, json_path, fingerprint in json_fingerprints:
            try:
                current = json_path.lstat()
                mode = current.st_mode
                current_fingerprint = (
                    current.st_mtime_ns,
                    current.st_size,
                    getattr(current, "st_ino", 0),
                )
                if (
                    stat.S_ISLNK(mode)
                    or _is_reparse_point(current)
                    or not stat.S_ISREG(mode)
                    or current_fingerprint != fingerprint
                ):
                    raise OSError("archive file changed")
            except OSError:
                json_invalid += 1
                json_changed_during_check = True
                _add_issue(report, "message_json_changed_during_check", member=member, year=year, month=month)

        if json_changed_during_check and index_state == "available":
            index_state = "failed"

        layout_after: dict[tuple[str, ...], tuple[object, ...]] = {}
        final_scan_report = _new_report()
        _archive_month_files(root, final_scan_report, layout_after)
        layout_changed = _archive_layout_changed(layout_before, layout_after)
        final_scan_failed = int(final_scan_report.get("_scan_failure_count") or 0) > 0
        initial_scan_failed = int(report.get("_scan_failure_count") or 0) > scan_failure_start
        if layout_changed or (final_scan_failed and not initial_scan_failed):
            json_invalid += 1
            json_changed_during_check = True
            _add_issue(report, "message_json_file_set_changed_during_check")

        if conn is not None and archive_data_version is not None:
            try:
                current_data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
                if current_data_version != archive_data_version:
                    _add_issue(report, "archive_db_changed_during_check")
                    _set_check(report, "archive_db_quick_check", "check_failed", checked=index_count, invalid=1)
                    index_state = "failed"
            except sqlite3.Error:
                _add_issue(report, "archive_db_read_failed")
                _set_check(report, "archive_db_quick_check", "check_failed", checked=index_count, invalid=1)
                index_state = "failed"

        scan_failure = int(report.get("_scan_failure_count") or 0) > scan_failure_start
        json_status = (
            "check_failed" if json_invalid or scan_failure
            else "needs_repair" if media_missing or media_unsafe or media_failed
            else "healthy"
        )
        _set_check(
            report,
            "message_json_scan",
            json_status,
            checked=checked_messages,
            invalid=json_invalid,
            missing=media_missing + media_unsafe + media_failed,
        )
        if index_state == "available":
            index_status = "needs_repair" if index_missing or index_mismatched else "healthy"
            _set_check(
                report,
                "message_index_comparison",
                index_status,
                checked=index_count,
                missing=index_missing,
                mismatched=index_mismatched,
            )
        elif index_state == "failed":
            _set_check(
                report,
                "message_index_comparison",
                "check_failed",
                checked=index_checked,
                missing=index_missing,
                mismatched=index_mismatched,
            )
        elif not any(check.get("code") == "message_index_comparison" for check in report["checks"]):
            _set_check(report, "message_index_comparison", "unavailable", checked=0, missing=0, mismatched=0)
    finally:
        if conn is not None:
            conn.close()


def _scan_blog_media(report: dict, task_id: str) -> None:
    project_root = Path(__file__).resolve().parents[3]
    db_path = project_root / "data" / "archive" / "blogs.db"
    db_state = _relative_entry_state(project_root, "data/archive/blogs.db")
    if db_state == "missing":
        _set_check(report, "blog_db_quick_check", "unavailable", checked=0, unavailable=1)
        _set_check(report, "blog_media_references", "unavailable", checked=0, missing=0, invalid=0)
        return
    if db_state != "file":
        _set_check(report, "blog_db_quick_check", "check_failed", checked=0, invalid=1)
        _set_check(report, "blog_media_references", "check_failed", checked=0, missing=0, invalid=1)
        _add_issue(report, "blog_db_path_uncheckable")
        return
    conn = None
    blog_data_version: int | None = None
    try:
        conn = _readonly_connect(db_path)
        blog_data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        quick_ok, quick_rows = _quick_check(conn)
        if quick_ok:
            _set_check(report, "blog_db_quick_check", "healthy", checked=quick_rows)
        else:
            _set_check(report, "blog_db_quick_check", "check_failed", checked=quick_rows, invalid=1)
            _add_issue(report, "blog_db_quick_check_failed")
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "blog_posts" not in tables:
            _set_check(report, "blog_media_references", "unavailable", checked=0, missing=0, invalid=0, unavailable=1)
            if int(conn.execute("PRAGMA data_version").fetchone()[0]) != blog_data_version:
                _add_issue(report, "blog_db_changed_during_check")
                _set_check(report, "blog_db_quick_check", "check_failed", checked=quick_rows, invalid=1)
            return
        cursor = conn.execute("SELECT id, image_paths_json FROM blog_posts")
        image_root = project_root
        checked = 0
        missing = 0
        invalid = 0
        for row in cursor:
            blog_id = row["id"]
            raw_paths = row["image_paths_json"]
            if not raw_paths:
                continue
            try:
                paths = json.loads(raw_paths) if isinstance(raw_paths, str) else raw_paths
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid += 1
                _add_issue(report, "blog_image_paths_invalid", blog_id=blog_id)
                continue
            if not isinstance(paths, list):
                invalid += 1
                _add_issue(report, "blog_image_paths_invalid", blog_id=blog_id)
                continue
            for image_path in paths:
                if not image_path:
                    continue
                if not isinstance(image_path, str):
                    invalid += 1
                    _add_issue(report, "blog_image_path_invalid", blog_id=blog_id)
                    continue
                checked += 1
                state = _reference_state(image_root, f"data/blog_images/{image_path}")
                if state == "missing":
                    missing += 1
                    _add_issue(report, "blog_media_missing", blog_id=blog_id)
                elif state == "unsafe":
                    invalid += 1
                    _add_issue(report, "blog_media_reference_unsafe", blog_id=blog_id)
                elif state in {"invalid", "error"}:
                    invalid += 1
                    _add_issue(report, "blog_media_reference_uncheckable", blog_id=blog_id)
                if checked % _PROGRESS_INTERVAL == 0:
                    archive_tasks.set_progress(task_id, "blog_media_references", checked, 0)
        blog_changed = False
        if blog_data_version is not None:
            current_data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
            if current_data_version != blog_data_version:
                blog_changed = True
                _add_issue(report, "blog_db_changed_during_check")
                _set_check(report, "blog_db_quick_check", "check_failed", checked=quick_rows, invalid=1)
        check_status = "check_failed" if invalid or blog_changed else ("needs_repair" if missing else "healthy")
        _set_check(report, "blog_media_references", check_status, checked=checked, missing=missing, invalid=invalid)
    except sqlite3.Error:
        _set_check(report, "blog_db_quick_check", "check_failed", checked=0, invalid=1)
        _set_check(report, "blog_media_references", "check_failed", checked=0, missing=0, invalid=1)
        _add_issue(report, "blog_db_read_failed")
    except OSError:
        _set_check(report, "blog_db_quick_check", "check_failed", checked=0, invalid=1)
        _set_check(report, "blog_media_references", "check_failed", checked=0, missing=0, invalid=1)
        _add_issue(report, "blog_db_read_failed")
    finally:
        if conn is not None:
            conn.close()


def _run_check(task_id: str) -> None:
    report = _new_report()
    report["started_at"] = _now()
    import src.config.config as cfg

    root = Path(cfg.ARCHIVE_DIR).absolute()
    try:
        archive_tasks.mark_task_running(task_id)
        heartbeat = archive_tasks.start_heartbeat(task_id)
        try:
            archive_tasks.set_progress(task_id, "dirty_marker", 0, 4)
            dirty_state = _relative_entry_state(root, ".messages-index-dirty")
            if dirty_state == "file":
                _set_check(report, "dirty_marker", "needs_repair", checked=1, invalid=1)
                _add_issue(report, "message_index_dirty_marker_present")
            elif dirty_state == "missing":
                _set_check(report, "dirty_marker", "healthy", checked=1)
            else:
                _set_check(report, "dirty_marker", "check_failed", checked=0, invalid=1)
                _add_issue(report, "dirty_marker_uncheckable")

            archive_tasks.set_progress(task_id, "message_json_scan", 0, 0)
            _scan_messages(root, report, task_id)
            _persist_report(task_id, report)

            archive_tasks.set_progress(task_id, "blog_media_references", 0, 4)
            _scan_blog_media(report, task_id)
            _persist_report(task_id, report)

            report["status"] = _report_status(report)
            report["finished_at"] = _now()
            report["progress"] = {"phase": "completed", "done": 4, "total": 4}
            archive_tasks.set_progress(task_id, "completed", 4, 4)
            _persist_report(task_id, report, item_status="completed")
            archive_tasks.set_progress(task_id, "completed", 4, 4)
            archive_tasks.finish_task(task_id, preserve_progress=True)
        finally:
            heartbeat.set()
    except Exception:
        report["checks"].append({"code": "integrity_worker", "status": "check_failed", "invalid": 1})
        _add_issue(report, "integrity_worker_failed")
        report["status"] = "check_failed"
        report["finished_at"] = _now()
        report["progress"] = {"phase": "failed", "done": 0, "total": 4}
        try:
            _persist_report(task_id, report, item_status="completed")
            archive_tasks.fail_to_start(task_id, "integrity_worker_failed")
        except Exception:
            archive_tasks.fail_to_start(task_id, "integrity_check_failed")


def _worker_entry(task_id: str) -> None:
    try:
        _run_check(task_id)
    finally:
        with _WORKER_LOCK:
            if _WORKERS_BY_TASK.get(task_id) is threading.current_thread():
                _WORKERS_BY_TASK.pop(task_id, None)


def start_integrity_check() -> tuple[dict, bool]:
    """Create/reuse one check, restarting stale work only when its worker is confirmed gone."""
    with _WORKER_LOCK:
        active = archive_tasks.get_latest_task(
            TASK_KIND,
            statuses=("queued", "running"),
            recover=False,
        )
        if active:
            worker_state = _active_worker_state(active)
            if not worker_state["heartbeat_stale"] or worker_state["restart_blocked"]:
                return _public_active_task(active) or {}, False
            if not _interrupt_abandoned_task(active):
                refreshed = archive_tasks.get_latest_task(
                    TASK_KIND,
                    statuses=("queued", "running"),
                    recover=False,
                )
                if refreshed:
                    return _public_active_task(refreshed) or {}, False

        task_id = archive_tasks.new_task_id()
        try:
            task = archive_tasks.create_task(task_id, TASK_KIND, {"read_only": True})
        except archive_tasks.ActiveTaskExists as exc:
            existing = archive_tasks.get_task(exc.task_id)
            if existing and existing.get("kind") == TASK_KIND:
                return _public_active_task(existing) or {}, False
            return {
                "id": str(exc.task_id),
                "kind": TASK_KIND,
                "scope": "archive",
                "status": "blocked",
                "worker_state": "another_task_active",
                "heartbeat_stale": False,
                "restart_blocked": True,
                "restart_reason": "another_archive_task_active",
                "report": None,
            }, False
        report = _new_report()
        report["started_at"] = _now()
        thread: threading.Thread | None = None
        try:
            _persist_report(task_id, report, item_status="queued")
            thread = threading.Thread(
                target=_worker_entry,
                args=(task_id,),
                name=f"archive-integrity-{task_id}",
                daemon=True,
            )
            _WORKERS_BY_TASK[task_id] = thread
            thread.start()
        except Exception:
            if thread is not None and _WORKERS_BY_TASK.get(task_id) is thread:
                _WORKERS_BY_TASK.pop(task_id, None)
            _set_check(report, "integrity_worker", "check_failed", checked=0, invalid=1)
            _add_issue(report, "integrity_worker_failed_to_start")
            report["status"] = "check_failed"
            report["finished_at"] = _now()
            report["progress"] = {"phase": "failed", "done": 0, "total": 0}
            try:
                _persist_report(task_id, report, item_status="completed")
            finally:
                archive_tasks.fail_to_start(task_id, "integrity_check_failed_to_start")
            raise
        current = archive_tasks.get_task(task_id) or task
        return _public_active_task(current) or {}, True


__all__ = [
    "TASK_KIND",
    "get_integrity_overview",
    "report_for_task",
    "start_integrity_check",
]


def report_for_task(task_id: str, *, task: dict | None = None) -> dict:
    task = task or archive_tasks.get_task_readonly(task_id)
    if task is None:
        return _stored_report(task_id)
    return _report_for_task(task)
