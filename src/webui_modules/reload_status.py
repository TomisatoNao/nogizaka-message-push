"""In-memory status for configuration reload application.

The report describes whether in-process reconfiguration handlers acknowledged
their work. It does not claim that remote services are healthy or that a
message can be delivered.
"""

from __future__ import annotations

from collections import OrderedDict
import copy
import re
import threading
import time
import secrets

RELOAD_CONFIRMATION_TIMEOUT_SECONDS = 15
RELOAD_REPORT_TTL_SECONDS = 60 * 60
MAX_RELOAD_REPORTS = 32

COMPONENT_IDS = (
    "loggers",
    "accounts",
    "telegram_bots",
    "qq_official_bots",
    "social_config",
    "qq_command_listener",
    "napcat_inbound_listener",
)

COMPONENT_STATES = {"pending", "completed", "failed", "not_required", "unconfirmed"}
REASON_CODES = {
    "component_exception",
    "component_reported_failure",
    "listener_start_failed",
    "missing_event_token",
    "no_event_loop",
    "event_loop_closed",
    "confirmation_timeout",
    "callback_unavailable",
    "callback_dispatch_failed",
    "component_confirmation_unavailable",
    "listener_disabled",
    "listener_task_ended",
    "listener_superseded",
}

_lock = threading.RLock()
_reports: OrderedDict[str, dict] = OrderedDict()
_RELOAD_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def _prune_locked(now: float) -> None:
    for report_id, report in list(_reports.items()):
        if now - report["created_at"] > RELOAD_REPORT_TTL_SECONDS:
            _reports.pop(report_id, None)
    while len(_reports) > MAX_RELOAD_REPORTS:
        _reports.popitem(last=False)


def _aggregate_locked(report: dict) -> str:
    if report["config"] == "failed":
        return "not_started"
    if report["config"] != "loaded":
        return "pending"

    states = [item["state"] for item in report["components"].values()]
    if not states and report["callback"] in {"unavailable", "dispatch_failed"}:
        return "not_started"
    if any(state == "failed" for state in states):
        return "partial_failure"
    if any(state == "pending" for state in states):
        return "pending"
    if any(state == "unconfirmed" for state in states):
        return "unconfirmed"
    if states and all(state in {"completed", "not_required"} for state in states):
        return "completed"
    if report["callback"] in {"unavailable", "dispatch_failed"}:
        return "unconfirmed"
    return "unconfirmed"


def _refresh_state_locked(report: dict) -> None:
    report["state"] = _aggregate_locked(report)


def create_reload_report() -> str:
    """Create and return an opaque id for a new reload attempt."""
    now = time.time()
    report_id = secrets.token_hex(32)
    with _lock:
        _prune_locked(now)
        _reports[report_id] = {
            "id": report_id,
            "state": "pending",
            "config": "loading",
            "callback": "pending",
            "started_at": now,
            "updated_at": now,
            "deadline_at": now + RELOAD_CONFIRMATION_TIMEOUT_SECONDS,
            "created_at": now,
            "components": {},
        }
        _prune_locked(now)
    return report_id


def record_config_load(report_id: str, loaded: bool) -> None:
    now = time.time()
    with _lock:
        report = _reports.get(report_id)
        if report is None:
            return
        report["config"] = "loaded" if loaded else "failed"
        report["callback"] = "pending" if loaded else "not_scheduled"
        report["components"] = {}
        report["updated_at"] = now
        _refresh_state_locked(report)


def begin_component_tracking(report_id: str) -> None:
    """Start the component set after the reload callback begins execution."""
    now = time.time()
    with _lock:
        report = _reports.get(report_id)
        if report is None or report["config"] != "loaded":
            return
        if report["components"]:
            return
        report["components"] = {
            component_id: {"state": "pending"}
            for component_id in COMPONENT_IDS
        }
        report["updated_at"] = now
        _refresh_state_locked(report)


def record_reload_callback(report_id: str, status: str, reason_code: str | None = None) -> None:
    now = time.time()
    if status not in {"dispatched", "unavailable", "dispatch_failed", "not_scheduled"}:
        raise ValueError(f"Invalid reload callback status: {status}")
    safe_reason = reason_code if reason_code in REASON_CODES else None
    with _lock:
        report = _reports.get(report_id)
        if report is None:
            return
        report["callback"] = status
        if status == "dispatched":
            # Give scheduled listeners their full confirmation window after
            # the synchronous callback has queued them on the event loop.
            report["deadline_at"] = now + RELOAD_CONFIRMATION_TIMEOUT_SECONDS
        if status in {"unavailable", "dispatch_failed"}:
            default_reason = "callback_unavailable" if status == "unavailable" else "callback_dispatch_failed"
            for component in report["components"].values():
                if component["state"] == "pending":
                    component["state"] = "unconfirmed"
                    component["reason_code"] = safe_reason or default_reason
        report["updated_at"] = now
        _refresh_state_locked(report)


def mark_pending_unconfirmed(report_id: str, *, reason_code: str) -> None:
    """Close pending acknowledgements when no correlated reporter exists."""
    now = time.time()
    safe_reason = reason_code if reason_code in REASON_CODES else "component_confirmation_unavailable"
    with _lock:
        report = _reports.get(report_id)
        if report is None:
            return
        for component in report["components"].values():
            if component["state"] == "pending":
                component["state"] = "unconfirmed"
                component["reason_code"] = safe_reason
        report["updated_at"] = now
        _refresh_state_locked(report)


def record_component(
    report_id: str,
    component_id: str,
    state: str,
    *,
    reason_code: str | None = None,
    progress: dict | None = None,
) -> bool:
    """Update one component without accepting free-form or secret-bearing text."""
    if component_id not in COMPONENT_IDS or state not in COMPONENT_STATES:
        return False
    now = time.time()
    item: dict = {"state": state}
    if reason_code in REASON_CODES:
        item["reason_code"] = reason_code
    if isinstance(progress, dict):
        safe_progress = {}
        for key in ("ready", "expected"):
            value = progress.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe_progress[key] = value
        if safe_progress:
            item["progress"] = safe_progress
    with _lock:
        report = _reports.get(report_id)
        if report is None or report["config"] != "loaded":
            return False
        report["components"][component_id] = item
        report["updated_at"] = now
        _refresh_state_locked(report)
        _prune_locked(now)
        return True


def get_reload_report(report_id: str | None = None) -> dict | None:
    """Return one report or the newest report, applying the confirmation deadline."""
    now = time.time()
    with _lock:
        _prune_locked(now)
        if report_id is None:
            if not _reports:
                return None
            report = next(reversed(_reports.values()))
        else:
            if not _RELOAD_ID_RE.fullmatch(str(report_id)):
                return None
            report = _reports.get(str(report_id))
            if report is None:
                return None

        changed = False
        if (
            report["config"] == "loaded"
            and report["callback"] != "pending"
            and now >= report["deadline_at"]
        ):
            for item in report["components"].values():
                if item["state"] == "pending":
                    item["state"] = "unconfirmed"
                    item["reason_code"] = "confirmation_timeout"
                    changed = True
        if changed:
            report["updated_at"] = now
            _refresh_state_locked(report)
        snapshot = copy.deepcopy(report)
        snapshot.pop("created_at", None)
        return snapshot
