"""
src/app_modules/process_lock.py — 进程单实例锁与生命周期信号控制
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from src.logger import log_all

PID_FILE = Path("data/app.pid")
STOP_FILE = Path(__file__).resolve().parent.parent.parent / "logs" / "service.stop"


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False


def _is_python_process(pid: int) -> bool:
    """确认目标 PID 是否确属 Python 运行进程，避免机器重启后 PID 循环重用误杀其他无关系统进程。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            process_query_limited_information = 0x1000
            h_proc = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not h_proc:
                return False
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size))
            ctypes.windll.kernel32.CloseHandle(h_proc)
            if ok:
                exe_name = buf.value.lower()
                return "python" in exe_name
            return False
        except Exception:  # nosec B110
            return False
    else:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace").lower()
                return "python" in cmd or "main.py" in cmd
        except Exception:  # nosec B110
            return True


def _kill_pid(pid: int) -> None:
    try:
        if sys.platform == "win32":
            import subprocess  # nosec B404
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)  # nosec B607, B603
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as ex:
        log_all(f"⚠️ 终止进程 {pid} 跳过: {ex}", is_debug=True)


_instance_lock_handle = None
_instance_lock_owner: int | None = None


def _lock_handle(handle) -> None:
    if sys.platform == "win32":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle) -> None:
    if sys.platform == "win32":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_instance_lock() -> None:
    """Hold an OS lock until shutdown; never kill a competing process."""
    global _instance_lock_handle, _instance_lock_owner
    if _instance_lock_handle is not None:
        raise RuntimeError("Application instance lock is already held")
    app_mod = sys.modules.get("src.app")
    pid_file_target = getattr(app_mod, "PID_FILE", PID_FILE) if app_mod else PID_FILE
    pid_path = Path(pid_file_target)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep this inode permanently: unlinking a lock file allows a competing
    # process to lock a different inode while the original owner still runs.
    handle = pid_path.with_suffix(pid_path.suffix + ".lock").open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        _lock_handle(handle)
        locked = True
        # A previous release may only have the legacy PID file. Refuse takeover
        # while it is alive; termination is an explicit operator action.
        if pid_path.exists():
            try:
                old_pid = int(pid_path.read_text(encoding="utf-8").strip())
            except ValueError:
                old_pid = 0
            if old_pid != os.getpid() and _is_pid_running(old_pid):
                raise RuntimeError("Another application instance is still running")
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except BaseException:
        if locked:
            _unlock_handle(handle)
        handle.close()
        raise
    _instance_lock_handle = handle
    _instance_lock_owner = os.getpid()


def _release_instance_lock() -> None:
    """Remove owned metadata before releasing the permanent OS lock."""
    global _instance_lock_handle, _instance_lock_owner
    handle = _instance_lock_handle
    if handle is None or _instance_lock_owner != os.getpid():
        return
    app_mod = sys.modules.get("src.app")
    pid_file_target = getattr(app_mod, "PID_FILE", PID_FILE) if app_mod else PID_FILE
    pid_path = Path(pid_file_target)
    try:
        if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    finally:
        try:
            _unlock_handle(handle)
        finally:
            handle.close()
            _instance_lock_handle = None
            _instance_lock_owner = None


def _stop_requested() -> bool:
    """外部是否请求停止（存在停止信号文件）。"""
    app_mod = sys.modules.get("src.app")
    target = getattr(app_mod, "STOP_FILE", STOP_FILE) if app_mod else STOP_FILE
    try:
        return Path(target).exists()
    except OSError:
        return False


__all__ = [
    "PID_FILE",
    "STOP_FILE",
    "_is_pid_running",
    "_is_python_process",
    "_kill_pid",
    "_acquire_instance_lock",
    "_release_instance_lock",
    "_stop_requested",
]
