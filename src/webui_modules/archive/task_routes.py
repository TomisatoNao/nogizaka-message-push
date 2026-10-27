"""Administrator-only API for persistent message and blog backfill tasks."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from src import archive_tasks
from src.webui_modules.archive.common import _send_json_resp

_CREDENTIAL_RE = re.compile(
    r"(?ix)"
    r"(?P<key_quote>[\"']?)"
    r"(?P<key>\b(?:authorization|proxy-authorization|access[_-]?token|refresh[_-]?token|token|cookie|api[_-]?key|password|(?:app|client)[_-]?secret|secret|open[_-]?id|group[_-]?(?:id|open[_-]?id)|target[_-]?chat(?:[_-]?id)?)\b)"
    r"(?P=key_quote)(?P<separator>\s*[:=]\s*)"
    r"(?:"
    r"(?P<value_quote>[\"'])(?P<quoted_value>(?:\\.|(?!(?P=value_quote)).)*)(?P=value_quote)"
    r"|(?P<plain_value>[^,\r\n;}]+)"
    r")"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_RE = re.compile(r"https?://[^\s\]\[(){}<>\"']+")
_TASK_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_CREDENTIAL_PATH_LABELS = frozenset({
    "authorization", "proxy-authorization", "access_token", "access-token",
    "refresh_token", "refresh-token", "token", "cookie", "api_key", "api-key", "password",
    "app_secret", "app-secret", "appsecret", "client_secret", "client-secret", "clientsecret",
    "secret", "openid", "open_id", "open-id", "group_id", "group-id", "groupid",
    "group_openid", "group-openid", "groupopenid", "target_chat", "target-chat",
    "target_chat_id", "target-chat-id", "targetchat", "targetchatid",
})
_CREDENTIAL_PATH_PREFIX_RE = re.compile(
    r"(?i)^(?:authorization|proxy-authorization|access[_-]?token|refresh[_-]?token|token|cookie|api[_-]?key|password|(?:app|client)[_-]?secret|secret|open[_-]?id|group[_-]?(?:id|open[_-]?id)|target[_-]?chat(?:[_-]?id)?)(?:$|[=._-])"
)
_LOGGER = logging.getLogger(__name__)


def _log_storage_failure(error_code: str, exc: OSError | sqlite3.Error) -> None:
    """Log a stable storage error code without exposing exception messages."""
    _LOGGER.warning(
        "archive_task_storage_failure code=%s exception_type=%s",
        error_code,
        type(exc).__name__,
    )


def _redact_credential(match: re.Match) -> str:
    key = f"{match.group('key_quote')}{match.group('key')}{match.group('key_quote')}"
    value_quote = match.group("value_quote") or ""
    return f"{key}{match.group('separator')}{value_quote}[redacted]{value_quote}"


def _safe_path(path: str) -> str:
    segments = unquote(path).split("/")
    for index, segment in enumerate(segments):
        if segment.casefold() in _CREDENTIAL_PATH_LABELS:
            if index + 1 < len(segments) and segments[index + 1]:
                segments[index + 1] = "[redacted]"
        elif _CREDENTIAL_PATH_PREFIX_RE.match(segment):
            segments[index] = "[redacted]"
        else:
            segments[index] = _CREDENTIAL_RE.sub(_redact_credential, segment)
    return "/".join(segments)


def _safe_url(value: str, *, keep_path: bool) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return value
    safe_netloc = parts.netloc.rsplit("@", 1)[-1]
    safe_path = _safe_path(parts.path) if keep_path else ""
    return urlunsplit((parts.scheme, safe_netloc, safe_path, "", ""))


def _safe_text(value: object) -> str:
    text = str(value or "")
    text = _CREDENTIAL_RE.sub(_redact_credential, text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    return _URL_RE.sub(lambda match: _safe_url(match.group(0), keep_path=False), text)[:4000]


def _safe_item_key(value: object) -> str:
    text = _CREDENTIAL_RE.sub(_redact_credential, str(value or ""))
    return _URL_RE.sub(lambda match: _safe_url(match.group(0), keep_path=True), text)[:4000]


def _heartbeat_stale(task: dict) -> bool:
    if task.get("status") not in {"queued", "running"}:
        return False
    heartbeat = task.get("heartbeat_at") or task.get("updated_at")
    if not heartbeat:
        return True
    try:
        stamp = datetime.fromisoformat(str(heartbeat))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() > 120
    except (TypeError, ValueError):
        return True


def _scope(task: dict) -> str:
    params = task.get("params", {})
    if task.get("kind") == "archive_integrity":
        return "archive"
    if task.get("kind") == "message_backfill":
        return "members" if params.get("member") else "all_members"
    return "member_url" if params.get("kind") == "member_url" else "groups"


def _public_task(task: dict) -> dict:
    return {
        "id": task.get("task_id", ""),
        "kind": task.get("kind", ""),
        "status": task.get("status", ""),
        "scope": _scope(task),
        "phase": task.get("phase", ""),
        "done": int(task.get("done") or 0),
        "total": int(task.get("total") or 0),
        "failure_count": int(task.get("failure_count") or 0),
        "error": _safe_text(task.get("error", "")),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "updated_at": task.get("updated_at"),
        "heartbeat_at": task.get("heartbeat_at"),
        "heartbeat_stale": _heartbeat_stale(task),
        "finished_at": task.get("finished_at"),
        "parent_id": task.get("parent_id"),
    }


def _public_item(item: dict) -> dict:
    item_key = _safe_item_key(item.get("key", ""))
    return {
        "item_type": item.get("type", ""),
        "item_key": item_key,
        "status": item.get("status", ""),
        "attempts": int(item.get("attempt") or 1),
        "error_code": None,
        "error_message": _safe_text(item.get("error", "")),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _send_error(handler, msg: str, status: int) -> None:
    _send_json_resp(handler, {"ok": False, "msg": msg}, status)


def authorize_archive_task_route(handler, sub: str, guard_fn) -> bool | None:
    """Authorize the task-center routes; return None for unrelated archive paths."""
    if sub == "tasks":
        method = "GET"
    elif sub.startswith("tasks/"):
        parts = sub.split("/")
        if len(parts) == 2 and _TASK_ID_RE.fullmatch(parts[1]):
            method = "GET"
        elif len(parts) == 3 and _TASK_ID_RE.fullmatch(parts[1]) and parts[2] == "retry":
            method = "POST"
        else:
            _send_error(handler, "任务路径无效", 404)
            return False
    else:
        return None

    if not guard_fn(need_admin=True):
        return False
    if handler.command != method:
        handler._pending_headers = [*getattr(handler, "_pending_headers", []), ("Allow", method)]
        _send_error(handler, f"请使用 {method} 请求", 405)
        return False
    return True


def handle_archive_tasks(handler, sub: str) -> None:
    """Serve list, detail, and retry task routes after the admin guard passed."""
    if sub == "tasks":
        query = parse_qs(handler.path.partition("?")[2])
        try:
            limit = int((query.get("limit") or [10])[0])
        except (TypeError, ValueError):
            _send_error(handler, "limit 必须是整数", 400)
            return
        try:
            tasks = archive_tasks.list_tasks(limit=limit)
        except (OSError, sqlite3.Error) as exc:
            _log_storage_failure("archive_task.list", exc)
            _send_error(handler, "归档任务存储暂时不可用，请查看服务端日志", 503)
            return
        _send_json_resp(handler, {"ok": True, "tasks": [_public_task(task) for task in tasks]})
        return

    parts = sub.split("/")
    if len(parts) not in {2, 3} or parts[0] != "tasks" or not _TASK_ID_RE.fullmatch(parts[1]):
        _send_error(handler, "任务路径无效", 404)
        return
    task_id = parts[1]
    if len(parts) == 3:
        if parts[2] != "retry":
            _send_error(handler, "任务路径无效", 404)
            return
        try:
            task = archive_tasks.get_task(task_id)
        except (OSError, sqlite3.Error) as exc:
            _log_storage_failure("archive_task.retry_lookup", exc)
            _send_error(handler, "归档任务存储暂时不可用，请查看服务端日志", 503)
            return
        if task is None:
            _send_error(handler, "归档任务不存在", 404)
            return
        if task.get("kind") == "message_backfill":
            from src.webui_modules.archive.message_backfill import retry_message_task

            try:
                retry_message_task(handler, task_id)
            except (OSError, sqlite3.Error) as exc:
                _log_storage_failure("archive_task.message_retry", exc)
                _send_error(handler, "无法读取或写入归档任务状态，请查看服务端日志", 503)
        elif task.get("kind") == "blog_backfill":
            from src.webui_modules.archive.blog_backfill import retry_blog_task

            try:
                retry_blog_task(handler, task_id)
            except (OSError, sqlite3.Error) as exc:
                _log_storage_failure("archive_task.blog_retry", exc)
                _send_error(handler, "无法读取或写入归档任务状态，请查看服务端日志", 503)
        else:
            _send_error(handler, "此任务类型不支持失败项重试", 400)
        return

    try:
        task = archive_tasks.get_task_readonly(task_id)
    except (OSError, sqlite3.Error) as exc:
        _log_storage_failure("archive_task.detail_lookup", exc)
        _send_error(handler, "归档任务存储暂时不可用，请查看服务端日志", 503)
        return
    if task is not None and task.get("kind") == "archive_integrity":
        from src.webui_modules.archive.integrity import report_for_task

        _send_json_resp(handler, {
            "ok": True,
            "task": _public_task(task),
            "items": [],
            "report": report_for_task(task_id, task=task),
            "retry_supported": False,
        })
        return

    try:
        task, items = archive_tasks.get_task_with_items(task_id, item_status="failed")
        retry_supported = archive_tasks.can_retry_task(task_id) if task else False
    except (OSError, sqlite3.Error) as exc:
        _log_storage_failure("archive_task.detail_items", exc)
        _send_error(handler, "归档任务存储暂时不可用，请查看服务端日志", 503)
        return
    if task is None:
        _send_error(handler, "归档任务不存在", 404)
        return
    public_items = [_public_item(item) for item in items]
    _send_json_resp(handler, {
        "ok": True,
        "task": _public_task(task),
        "items": public_items,
        "retry_supported": retry_supported,
    })
