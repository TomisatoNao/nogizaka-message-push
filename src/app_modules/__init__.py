"""
src/app_modules — 主应用解耦子模块包
"""
from typing import Any

from src.app_modules.daily_summary import (
    DISK_WARN_BYTES,
    SUMMARY_MAX_ATTEMPTS,
    SUMMARY_RETRY_SECONDS,
    _build_daily_summary,
    _daily_summary_loop,
    _dir_size,
    _get_jst_now,
    _send_summary_with_retry,
    _storage_line,
    _to_jst_date,
)
from src.app_modules.message_worker import (
    _MemberCycleResult,
    _calc_sleep_seconds,
    _message_cycle_summary,
    _next_interval,
    _run_cycle,
    _run_loop,
    _wait_or_trigger,
)
from src.app_modules.process_lock import (
    PID_FILE,
    STOP_FILE,
    _acquire_instance_lock,
    _is_pid_running,
    _is_python_process,
    _kill_pid,
    _release_instance_lock,
    _stop_requested,
)


def __getattr__(name: str) -> Any:
    import src.app as app
    if hasattr(app, name):
        return getattr(app, name)
    raise AttributeError(f"module 'src.app_modules' has no attribute '{name}'")


__all__ = [
    # process_lock
    "PID_FILE",
    "STOP_FILE",
    "_is_pid_running",
    "_is_python_process",
    "_kill_pid",
    "_acquire_instance_lock",
    "_release_instance_lock",
    "_stop_requested",
    # daily_summary
    "DISK_WARN_BYTES",
    "SUMMARY_MAX_ATTEMPTS",
    "SUMMARY_RETRY_SECONDS",
    "_get_jst_now",
    "_to_jst_date",
    "_dir_size",
    "_storage_line",
    "_build_daily_summary",
    "_send_summary_with_retry",
    "_daily_summary_loop",
    # message_worker
    "_MemberCycleResult",
    "_message_cycle_summary",
    "_calc_sleep_seconds",
    "_next_interval",
    "_wait_or_trigger",
    "_run_cycle",
    "_run_loop",
]
