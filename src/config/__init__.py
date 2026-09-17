"""
src/config — 系统配置与凭据管理核心模块
"""
from __future__ import annotations

import sys as _sys

_sys.modules.setdefault("config", _sys.modules[__name__])


def __getattr__(name: str):
    if name in ("cfg", "config"):
        from src.config import config as _cfg
        return _cfg
    if name == "credentials":
        from src.config import credentials as _creds
        return _creds
    if name == "watcher":
        from src.config import watcher as _watcher
        return _watcher
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = ["config", "cfg", "credentials", "watcher"]

