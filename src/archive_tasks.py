"""Persistent task state for long-running archive backfills.

The task database is deliberately separate from both the message and blog
databases. Every operation opens its own SQLite connection so web handlers,
output collectors, and child processes can update task state independently.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

TASK_STATUSES = frozenset({"queued", "running", "completed", "partial", "failed", "interrupted"})
ITEM_STATUSES = frozenset({"queued", "running", "completed", "failed"})
SUPPORTED_TASK_KINDS = frozenset({"message_backfill", "blog_backfill", "archive_integrity"})
RETRYABLE_TASK_KINDS = frozenset({"message_backfill", "blog_backfill"})
STALE_AFTER_SECONDS = 120
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY_PATHS: set[str] = set()


class ActiveTaskExists(RuntimeError):
    """Raised when a task of the same kind is already queued or running."""

    def __init__(self, task_id: str):
        super().__init__(task_id)
        self.task_id = task_id


class TaskNotFound(LookupError):
    """Raised when the requested task does not exist."""


class NoFailedItems(ValueError):
    """Raised when a task has no failed items to retry."""


class RetryAlreadyCreated(RuntimeError):
    """Raised when a task already has a retry child; continue from that child."""

    def __init__(self, task_id: str):
        super().__init__(task_id)
        self.task_id = task_id


class TaskNotRetryable(ValueError):
    """Raised when a task kind is intentionally outside the retry boundary."""


def _db_path() -> Path:
    import src.config.config as cfg

    return Path(cfg.ARCHIVE_DIR) / "tasks.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _initialize(conn: sqlite3.Connection) -> None:
    path_key = str(_db_path().resolve())
    if path_key in _SCHEMA_READY_PATHS:
        return
    with _SCHEMA_LOCK:
        if path_key in _SCHEMA_READY_PATHS:
            return
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT '',
            done INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            pid INTEGER,
            process_marker TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            updated_at TEXT NOT NULL,
            heartbeat_at TEXT,
            finished_at TEXT,
            params_json TEXT NOT NULL DEFAULT '{}',
            parent_id TEXT,
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_archive_tasks_status_kind
            ON tasks(status, kind, created_at DESC);
        CREATE TABLE IF NOT EXISTS task_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            type TEXT NOT NULL,
            key TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            error TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(task_id, type, key)
        );
        CREATE INDEX IF NOT EXISTS idx_archive_task_items_status
            ON task_items(task_id, status, type, key);
            """
        )
        conn.commit()
        _SCHEMA_READY_PATHS.add(path_key)


def _json_load(raw: str | None, default):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value


def _task_dict(row: sqlite3.Row) -> dict:
    task = dict(row)
    task["params"] = _json_load(task.pop("params_json", "{}"), {})
    task["progress"] = {
        "phase": task.get("phase", ""),
        "done": int(task.get("done") or 0),
        "total": int(task.get("total") or 0),
    }
    return task


def _item_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["payload"] = _json_load(item.pop("payload_json", "{}"), {})
    return item


def _process_marker(pid: int) -> str | None:
    """Return a stable process-start marker where the host OS exposes one."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            try:
                created = FILETIME()
                exited = FILETIME()
                kernel = FILETIME()
                user = FILETIME()
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                )
                if not ok:
                    return None
                return f"{created.high}:{created.low}"
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return None

    proc_stat = Path(f"/proc/{int(pid)}/stat")
    try:
        raw = proc_stat.read_text(encoding="ascii")
        close_paren = raw.rfind(")")
        fields = raw[close_paren + 2 :].split()
        return fields[19] if len(fields) > 19 else None
    except (OSError, UnicodeError, ValueError):
        return None


def _process_is_alive(pid: int | None, marker: str | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, int(pid))
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                wait_result = kernel32.WaitForSingleObject(handle, 0)
                if wait_result == 0:
                    return False
                if wait_result != 258:
                    return True
                current_marker = _process_marker(pid)
                return marker is None or current_marker is None or current_marker == marker
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return True
    current_marker = _process_marker(pid)
    if current_marker is not None:
        return marker is None or current_marker == marker
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _ensure_task_failure_item(conn: sqlite3.Connection, task_id: str, error: str, now: str) -> None:
    failed = conn.execute(
        "SELECT 1 FROM task_items WHERE task_id=? AND status='failed' LIMIT 1",
        (task_id,),
    ).fetchone()
    if failed:
        return
    conn.execute(
        "INSERT INTO task_items(task_id,type,key,status,attempt,error,payload_json,created_at,updated_at) "
        "VALUES(?,'task','execution','failed',1,?,'{}',?,?) "
        "ON CONFLICT(task_id,type,key) DO UPDATE SET status='failed',error=excluded.error,updated_at=excluded.updated_at",
        (task_id, str(error)[:4000], now, now),
    )


def _age_seconds(timestamp: str | None) -> float:
    if not timestamp:
        return float("inf")
    try:
        stamp = datetime.fromisoformat(timestamp)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def recover_stale_tasks(*, stale_after_seconds: int = STALE_AFTER_SECONDS) -> int:
    """Mark abandoned queued/running tasks interrupted without stopping live children."""
    changed = 0
    with closing(_connect()) as conn:
        _initialize(conn)
        rows = conn.execute(
            "SELECT task_id, status, pid, process_marker, heartbeat_at, updated_at "
            "FROM tasks WHERE status IN ('queued', 'running')"
        ).fetchall()
        for row in rows:
            heartbeat_age = _age_seconds(row["heartbeat_at"] or row["updated_at"])
            process_known = bool(row["pid"])
            process_alive = _process_is_alive(row["pid"], row["process_marker"])
            if process_alive:
                continue
            if not process_known and heartbeat_age <= stale_after_seconds:
                continue
            now = _now()
            error = "WebUI 重启后发现任务心跳过期且子进程已退出"
            conn.execute(
                "UPDATE task_items SET status='failed', error=?, updated_at=? "
                "WHERE task_id=? AND status IN ('queued', 'running')",
                (error, now, row["task_id"]),
            )
            _ensure_task_failure_item(conn, str(row["task_id"]), error, now)
            counts = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN status IN ('completed','failed') THEN 1 ELSE 0 END) "
                "FROM task_items WHERE task_id=?",
                (row["task_id"],),
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET status='interrupted', phase='interrupted', done=?, total=?, error=?, "
                "updated_at=?, finished_at=? WHERE task_id=? AND status IN ('queued', 'running')",
                (int(counts[1] or 0), int(counts[0] or 0), error, now, now, row["task_id"]),
            )
            changed += 1
        conn.commit()
    return changed


def create_task(
    task_id: str,
    kind: str,
    params: dict | None = None,
    *,
    prevent_active: bool = True,
) -> dict:
    if kind not in SUPPORTED_TASK_KINDS:
        raise ValueError(f"unsupported archive task kind: {kind}")
    recover_stale_tasks()
    now = _now()
    with closing(_connect()) as conn:
        _initialize(conn)
        conn.execute("BEGIN IMMEDIATE")
        if prevent_active:
            active = conn.execute(
                "SELECT task_id FROM tasks WHERE kind=? AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1",
                (kind,),
            ).fetchone()
            if active:
                conn.rollback()
                raise ActiveTaskExists(str(active["task_id"]))
        conn.execute(
            "INSERT INTO tasks(task_id,kind,status,phase,created_at,updated_at,params_json) "
            "VALUES(?,?,'queued','queued',?,?,?)",
            (task_id, kind, now, now, json.dumps(params or {}, ensure_ascii=False, default=str)),
        )
        conn.commit()
    return get_task(task_id) or {}


def create_retry_task(parent_task_id: str, task_id: str) -> tuple[dict, list[dict]]:
    recover_stale_tasks()
    now = _now()
    with closing(_connect()) as conn:
        _initialize(conn)
        conn.execute("BEGIN IMMEDIATE")
        parent = conn.execute("SELECT * FROM tasks WHERE task_id=?", (parent_task_id,)).fetchone()
        if parent is None:
            conn.rollback()
            raise TaskNotFound(parent_task_id)
        if str(parent["kind"]) not in RETRYABLE_TASK_KINDS:
            conn.rollback()
            raise TaskNotRetryable(str(parent["kind"]))
        previous_retry = conn.execute(
            "SELECT task_id FROM tasks WHERE parent_id=? ORDER BY created_at DESC, task_id DESC LIMIT 1",
            (parent_task_id,),
        ).fetchone()
        if previous_retry:
            conn.rollback()
            raise RetryAlreadyCreated(str(previous_retry["task_id"]))
        active = conn.execute(
            "SELECT task_id FROM tasks WHERE kind=? AND status IN ('queued','running') "
            "ORDER BY created_at DESC LIMIT 1",
            (parent["kind"],),
        ).fetchone()
        if active:
            conn.rollback()
            raise ActiveTaskExists(str(active["task_id"]))
        failed = _retryable_failed_items(conn, parent_task_id)
        if not failed:
            conn.rollback()
            raise NoFailedItems(parent_task_id)
        conn.execute(
            "INSERT INTO tasks(task_id,kind,status,phase,total,created_at,updated_at,"
            "params_json,parent_id) VALUES(?,?,'queued','retry_queued',?,?,?, ?,?)",
            (
                task_id,
                parent["kind"],
                len(failed),
                now,
                now,
                parent["params_json"],
                parent_task_id,
            ),
        )
        for item in failed:
            conn.execute(
                "INSERT INTO task_items(task_id,type,key,status,attempt,error,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,'queued',?,?,?,?,?)",
                (
                    task_id,
                    item["type"],
                    item["key"],
                    int(item["attempt"] or 1) + 1,
                    "",
                    item["payload_json"],
                    now,
                    now,
                ),
            )
        conn.commit()
    return get_task(task_id) or {}, get_items(task_id)


def _retryable_failed_items(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    failed = conn.execute(
        "SELECT type,key,attempt,error,payload_json FROM task_items "
        "WHERE task_id=? AND status='failed' ORDER BY type,key",
        (task_id,),
    ).fetchall()
    completed_work = conn.execute(
        "SELECT 1 FROM task_items WHERE task_id=? AND type<>'task' AND status='completed' LIMIT 1",
        (task_id,),
    ).fetchone()
    if completed_work:
        failed = [item for item in failed if item["type"] != "task"]
    return list(failed)


def can_retry_task(task_id: str) -> bool:
    """Return whether the current task snapshot can create a retry child.

    ``create_retry_task`` repeats all checks transactionally, so this value is
    advisory for the UI and can become stale before a retry request arrives.
    """
    recover_stale_tasks()
    with closing(_connect()) as conn:
        _initialize(conn)
        task = conn.execute(
            "SELECT kind FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if task is None or str(task["kind"]) not in RETRYABLE_TASK_KINDS:
            return False
        previous_retry = conn.execute(
            "SELECT 1 FROM tasks WHERE parent_id=? LIMIT 1",
            (task_id,),
        ).fetchone()
        active = conn.execute(
            "SELECT 1 FROM tasks WHERE kind=? AND status IN ('queued','running') LIMIT 1",
            (task["kind"],),
        ).fetchone()
        return not previous_retry and not active and bool(_retryable_failed_items(conn, task_id))


def mark_task_running(task_id: str, pid: int | None = None) -> None:
    now = _now()
    pid = int(pid or os.getpid())
    marker = _process_marker(pid)
    with closing(_connect()) as conn:
        _initialize(conn)
        conn.execute(
            "UPDATE tasks SET status='running', phase=CASE WHEN phase='queued' THEN 'starting' ELSE phase END, "
            "pid=?, process_marker=?, started_at=COALESCE(started_at,?), heartbeat_at=?, updated_at=? "
            "WHERE task_id=? AND status IN ('queued','running')",
            (pid, marker, now, now, now, task_id),
        )
        conn.commit()


def heartbeat_task(task_id: str, pid: int | None = None) -> None:
    now = _now()
    with closing(_connect()) as conn:
        _initialize(conn)
        conn.execute(
            "UPDATE tasks SET heartbeat_at=?, updated_at=?, pid=COALESCE(pid,?) "
            "WHERE task_id=? AND status IN ('queued','running')",
            (now, now, int(pid or os.getpid()), task_id),
        )
        conn.commit()


def start_heartbeat(task_id: str, *, interval_seconds: int = 15) -> threading.Event:
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval_seconds):
            try:
                heartbeat_task(task_id)
            except sqlite3.Error:
                # The next beat retries; the child process remains authoritative.
                continue

    threading.Thread(target=beat, name=f"archive-task-heartbeat-{task_id}", daemon=True).start()
    return stop


def set_progress(task_id: str, phase: str, done: int | None = None, total: int | None = None) -> None:
    now = _now()
    assignments = ["phase=?", "heartbeat_at=?", "updated_at=?", "status='running'"]
    values: list[object] = [str(phase or "")[:200], now, now]
    if done is not None:
        assignments.append("done=?")
        values.append(max(0, int(done)))
    if total is not None:
        assignments.append("total=?")
        values.append(max(0, int(total)))
    values.append(task_id)
    with closing(_connect()) as conn:
        _initialize(conn)
        conn.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id=? AND status IN ('queued','running')",
            values,
        )
        conn.commit()


def set_item(
    task_id: str,
    item_type: str,
    key: str,
    status: str,
    *,
    attempt: int | None = None,
    error: str = "",
    payload: dict | None = None,
) -> None:
    if status not in ITEM_STATUSES:
        raise ValueError(f"invalid task item status: {status}")
    now = _now()
    payload_json = json.dumps(payload or {}, ensure_ascii=False, default=str)
    with closing(_connect()) as conn:
        _initialize(conn)
        row = conn.execute(
            "SELECT attempt FROM task_items WHERE task_id=? AND type=? AND key=?",
            (task_id, item_type, key),
        ).fetchone()
        resolved_attempt = max(1, int(attempt or (row["attempt"] if row else 1)))
        conn.execute(
            "INSERT INTO task_items(task_id,type,key,status,attempt,error,payload_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id,type,key) DO UPDATE SET "
            "status=excluded.status,attempt=MAX(task_items.attempt,excluded.attempt),error=excluded.error, "
            "payload_json=CASE WHEN excluded.payload_json='{}' THEN task_items.payload_json ELSE excluded.payload_json END, "
            "updated_at=excluded.updated_at",
            (
                task_id,
                str(item_type)[:80],
                str(key)[:2000],
                status,
                resolved_attempt,
                str(error or "")[:4000],
                payload_json,
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE tasks SET heartbeat_at=?,updated_at=?,status=CASE WHEN status='queued' THEN 'running' ELSE status END "
            "WHERE task_id=? AND status IN ('queued','running')",
            (now, now, task_id),
        )
        counts = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status IN ('completed','failed') THEN 1 ELSE 0 END) "
            "FROM task_items WHERE task_id=?",
            (task_id,),
        ).fetchone()
        conn.execute(
            "UPDATE tasks SET total=?,done=? WHERE task_id=? AND status IN ('queued','running')",
            (int(counts[0] or 0), int(counts[1] or 0), task_id),
        )
        conn.commit()


def update_item_payload(task_id: str, item_type: str, key: str, payload: dict) -> bool:
    """Update report metadata without recalculating task progress counters."""
    now = _now()
    payload_json = json.dumps(payload or {}, ensure_ascii=False, default=str)
    with closing(_connect()) as conn:
        _initialize(conn)
        cursor = conn.execute(
            "UPDATE task_items SET payload_json=?,updated_at=? WHERE task_id=? AND type=? AND key=?",
            (payload_json, now, task_id, str(item_type)[:80], str(key)[:2000]),
        )
        conn.commit()
    return cursor.rowcount > 0


def finish_task(task_id: str, *, error: str = "", preserve_progress: bool = False) -> str | None:
    now = _now()
    with closing(_connect()) as conn:
        _initialize(conn)
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT status,done,total FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None or task["status"] not in {"queued", "running"}:
            conn.rollback()
            return str(task["status"]) if task else None
        if error:
            conn.execute(
                "UPDATE task_items SET status='failed',error=?,updated_at=? "
                "WHERE task_id=? AND status IN ('queued','running')",
                (str(error)[:4000], now, task_id),
            )
        else:
            conn.execute(
                "UPDATE task_items SET status='failed',error='任务结束前项目未完成',updated_at=? "
                "WHERE task_id=? AND status IN ('queued','running')",
                (now, task_id),
            )
        if error:
            _ensure_task_failure_item(conn, task_id, str(error), now)
        counts = conn.execute(
            "SELECT SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), COUNT(*) "
            "FROM task_items WHERE task_id=?",
            (task_id,),
        ).fetchone()
        completed, failed, total = (int(value or 0) for value in counts)
        if error:
            status = "partial" if completed else "failed"
        elif failed and completed:
            status = "partial"
        elif failed:
            status = "failed"
        else:
            status = "completed"
        task_error = str(error or "")[:4000]
        done_value = int(task["done"] or 0) if preserve_progress else completed + failed
        total_value = int(task["total"] or 0) if preserve_progress else total
        conn.execute(
            "UPDATE tasks SET status=?,phase=?,done=?,total=?,error=?,heartbeat_at=?,updated_at=?,finished_at=? "
            "WHERE task_id=? AND status IN ('queued','running')",
            (status, status, done_value, total_value, task_error, now, now, now, task_id),
        )
        conn.commit()
    return status


def interrupt_task(task_id: str, error: str) -> bool:
    """Mark one confirmed-abandoned task interrupted without changing progress."""
    now = _now()
    with closing(_connect()) as conn:
        _initialize(conn)
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute(
            "SELECT status FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if task is None or task["status"] not in {"queued", "running"}:
            conn.rollback()
            return False
        cursor = conn.execute(
            "UPDATE tasks SET status='interrupted',phase='interrupted',error=?,heartbeat_at=?,updated_at=?,finished_at=? "
            "WHERE task_id=? AND status IN ('queued','running')",
            (str(error or "")[:4000], now, now, now, task_id),
        )
        conn.commit()
    return cursor.rowcount > 0


def fail_to_start(task_id: str, error: str) -> None:
    now = _now()
    with closing(_connect()) as conn:
        _initialize(conn)
        _ensure_task_failure_item(conn, task_id, str(error), now)
        conn.execute(
            "UPDATE tasks SET status='failed',phase='failed',error=?,updated_at=?,finished_at=?,heartbeat_at=? "
            "WHERE task_id=? AND status IN ('queued','running')",
            (str(error)[:4000], now, now, now, task_id),
        )
        conn.commit()


def finalize_after_process_exit(task_id: str, exit_code: int | None) -> str | None:
    """Reconcile a child that exited without its explicit final status write."""
    error = "子进程退出但未写入完成状态"
    if exit_code not in (None, 0):
        error = f"{error}（exit_code={exit_code}）"
    return finish_task(task_id, error=error)


def get_task(task_id: str) -> dict | None:
    with closing(_connect()) as conn:
        _initialize(conn)
        row = conn.execute(
            "SELECT tasks.*, (SELECT COUNT(*) FROM task_items WHERE task_items.task_id=tasks.task_id "
            "AND task_items.status='failed') AS failure_count FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    return _task_dict(row) if row else None


def get_task_readonly(task_id: str) -> dict | None:
    """Return task metadata without schema setup or stale-task recovery."""
    path = _db_path()
    if not path.is_file():
        return None
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(
            "SELECT tasks.*, (SELECT COUNT(*) FROM task_items WHERE task_items.task_id=tasks.task_id "
            "AND task_items.status='failed') AS failure_count FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    return _task_dict(row) if row else None


def get_items(task_id: str, *, status: str | None = None) -> list[dict]:
    with closing(_connect()) as conn:
        _initialize(conn)
        if status:
            rows = conn.execute(
                "SELECT * FROM task_items WHERE task_id=? AND status=? ORDER BY type,key",
                (task_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM task_items WHERE task_id=? ORDER BY type,key",
                (task_id,),
            ).fetchall()
    return [_item_dict(row) for row in rows]


def get_completed_retry_ancestor_items(task_id: str) -> list[dict]:
    """Return completed work from every ancestor so chained retries can skip it."""
    task = get_task(task_id)
    parent_id = str(task.get("parent_id") or "") if task else ""
    seen = {str(task_id)}
    completed: list[dict] = []
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        completed.extend(get_items(parent_id, status="completed"))
        parent = get_task(parent_id)
        parent_id = str(parent.get("parent_id") or "") if parent else ""
    return completed


def get_items_readonly(task_id: str) -> list[dict]:
    """Read task items without schema initialization or stale-task recovery."""
    path = _db_path()
    if not path.is_file():
        return []
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT * FROM task_items WHERE task_id=? ORDER BY type,key",
            (task_id,),
        ).fetchall()
    return [_item_dict(row) for row in rows]


def list_tasks(*, limit: int = 50) -> list[dict]:
    recover_stale_tasks()
    bounded = min(200, max(1, int(limit)))
    with closing(_connect()) as conn:
        _initialize(conn)
        rows = conn.execute(
            "SELECT tasks.*, (SELECT COUNT(*) FROM task_items WHERE task_items.task_id=tasks.task_id "
            "AND task_items.status='failed') AS failure_count "
            "FROM tasks ORDER BY created_at DESC, task_id DESC LIMIT ?",
            (bounded,),
        ).fetchall()
    return [_task_dict(row) for row in rows]


def get_latest_task(
    kind: str,
    *,
    statuses: tuple[str, ...] | None = None,
    recover: bool = True,
) -> dict | None:
    """Return the newest task of one kind using a read-only task DB connection."""
    path = _db_path()
    if recover and path.is_file():
        recover_stale_tasks()
    if not path.is_file():
        return None
    query = "SELECT tasks.*, (SELECT COUNT(*) FROM task_items WHERE task_items.task_id=tasks.task_id " \
        "AND task_items.status='failed') AS failure_count FROM tasks WHERE kind=?"
    params: list[object] = [str(kind)]
    if statuses:
        safe_statuses = tuple(str(status) for status in statuses if str(status) in TASK_STATUSES)
        if not safe_statuses:
            return None
        placeholders = ",".join("?" for _ in safe_statuses)
        query += f" AND status IN ({placeholders})"
        params.extend(safe_statuses)
    query += " ORDER BY created_at DESC, task_id DESC LIMIT 1"
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30.0)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(query, params).fetchone()
    return _task_dict(row) if row else None


def get_task_with_items(task_id: str, *, item_status: str | None = None) -> tuple[dict | None, list[dict]]:
    recover_stale_tasks()
    task = get_task(task_id)
    return task, get_items(task_id, status=item_status) if task else []


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]
