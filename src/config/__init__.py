"""
src/config — 系统配置与凭据管理核心模块
"""
from __future__ import annotations

import sys as _sys
from src.config import config as cfg
from src.config import credentials
from src.config import watcher

_sys.modules.setdefault("config", _sys.modules[__name__])

__all__ = ["cfg", "credentials", "watcher"]
