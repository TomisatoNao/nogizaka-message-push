"""
src/config — 系统配置与凭据管理核心模块
"""
from __future__ import annotations

import importlib as _importlib
import sys as _sys

_sys.modules.setdefault("config", _sys.modules[__name__])


def __getattr__(name: str):
    if name in ("cfg", "config"):
        mod = _importlib.import_module(".config", __name__)
        globals()["cfg"] = mod
        globals()["config"] = mod
        return mod
    if name == "credentials":
        mod = _importlib.import_module(".credentials", __name__)
        globals()["credentials"] = mod
        return mod
    if name == "watcher":
        mod = _importlib.import_module(".watcher", __name__)
        globals()["watcher"] = mod
        return mod
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = ["config", "cfg", "credentials", "watcher"]

