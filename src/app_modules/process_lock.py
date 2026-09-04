"""
src/app_modules/process_lock.py — 进程单实例锁与生命周期信号控制
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

from src.logger import log_all

PID_FILE = Path("data/app.pid")
STOP_FILE = Path(__file__).resolve().parent.parent.parent / "logs" / "service.stop"


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        process_query_information = 0x0400
        handle = ctypes.windll.kernel32.OpenProcess(process_query_information, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
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


def _acquire_instance_lock() -> None:
    """确保全机只有一个主程序实例在运行，若存在历史遗留孤儿进程则自动清理接管。"""
    app_mod = sys.modules.get("src.app")
    pid_file_target = getattr(app_mod, "PID_FILE", PID_FILE) if app_mod else PID_FILE
    pid_path = Path(pid_file_target)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text(encoding="utf-8").strip())
            if old_pid != my_pid and _is_pid_running(old_pid):
                if _is_python_process(old_pid):
                    log_all(f"⚠️ 检测到已存在运行中的主程序旧实例 (PID: {old_pid})，正在接管并终止旧实例...")
                    _kill_pid(old_pid)
                    time.sleep(1.0)
                else:
                    log_all(f"ℹ️ 检测到历史 PID 文件记录 ({old_pid}) 已失效（非 Python 进程），自动接管覆盖。")
        except (OSError, ValueError) as ex:
            log_all(f"⚠️ 读取旧 PID 文件异常: {ex}", is_debug=True)
    try:
        pid_path.write_text(str(my_pid), encoding="utf-8")
    except OSError as ex:
        log_all(f"⚠️ 写入当前 PID 文件异常: {ex}", is_debug=True)


def _release_instance_lock() -> None:
    """释放当前进程持有的 PID 锁文件。"""
    app_mod = sys.modules.get("src.app")
    pid_file_target = getattr(app_mod, "PID_FILE", PID_FILE) if app_mod else PID_FILE
    pid_path = Path(pid_file_target)
    try:
        if pid_path.exists():
            content = pid_path.read_text(encoding="utf-8").strip()
            if content == str(os.getpid()):
                pid_path.unlink(missing_ok=True)
    except OSError:
        pass


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
