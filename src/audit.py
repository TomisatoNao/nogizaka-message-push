"""管理端安全审计事件。

审计记录与普通运行日志分离，使用 JSON Lines 格式，便于后续检索、备份
或导入外部日志系统。该模块刻意不记录密码、Cookie、Token 和请求体。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.logger import redact_sensitive

_LOCK = threading.Lock()
_MAX_FILE_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5
_MAX_TEXT_LENGTH = 512
_SENSITIVE_KEYS = frozenset({
    "password", "token", "access_token", "refresh_token", "authorization",
    "cookie", "set-cookie", "secret", "client_secret", "api_key", "key",
})


def _log_path() -> Path:
    import config.config as cfg
    return Path(getattr(cfg, "AUDIT_LOG_FILE", "logs/audit.jsonl"))


def _safe_text(value: Any) -> str:
    return redact_sensitive(str(value))[:_MAX_TEXT_LENGTH]


def _safe_details(details: dict[str, Any] | None) -> dict[str, str]:
    if not details:
        return {}
    safe: dict[str, str] = {}
    for key, value in details.items():
        normalised_key = str(key).strip().lower()
        if normalised_key in _SENSITIVE_KEYS or any(part in normalised_key for part in ("token", "secret", "password", "cookie")):
            safe[str(key)] = "***HIDDEN***"
        else:
            safe[str(key)] = _safe_text(value)
    return safe


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < _MAX_FILE_BYTES:
        return
    for index in range(_BACKUP_COUNT - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        dst = path.with_name(f"{path.name}.{index + 1}")
        if src.exists():
            os.replace(src, dst)
    os.replace(path, path.with_name(f"{path.name}.1"))


def record_event(
    event: str,
    *,
    outcome: str,
    actor: str | None = None,
    source_ip: str | None = None,
    target: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """追加一条已脱敏的审计事件；磁盘故障不影响业务请求。"""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": _safe_text(event),
        "outcome": _safe_text(outcome),
        "actor": _safe_text(actor or "anonymous"),
        "source_ip": _safe_text(source_ip or "unknown"),
        "target": _safe_text(target or ""),
        "details": _safe_details(details),
    }
    try:
        path = _log_path()
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        # 审计功能不能导致认证、归档或配置写入不可用。
        return
