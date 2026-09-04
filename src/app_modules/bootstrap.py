"""
src/app_modules/bootstrap.py — 向后兼容别名门面（生命周期与自检逻辑已收敛至 src.app）
"""
from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    import src.app as app
    return getattr(app, name)
