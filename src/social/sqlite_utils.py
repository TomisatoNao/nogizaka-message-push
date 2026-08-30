"""Small, dependency-free SQLite resilience helpers.

SQLite WAL still permits only one writer. A short retry with jitter handles
ordinary writer hand-off without hiding permanent schema or programming errors.
"""
from __future__ import annotations

import logging
import random
import sqlite3
import time

log = logging.getLogger("collink")

BUSY_TIMEOUT_MS = 30_000
MAX_BUSY_RETRIES = 5


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text


class RetryingConnection(sqlite3.Connection):
    """Connection that retries only transient SQLite busy/locked failures."""

    def _with_busy_retry(self, operation, *args, **kwargs):
        for attempt in range(MAX_BUSY_RETRIES + 1):
            try:
                return operation(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt >= MAX_BUSY_RETRIES:
                    raise
                delay = min(1.5, 0.05 * (2 ** attempt)) * random.uniform(0.8, 1.2)  # nosec B311
                log.warning("[sqlite] busy/locked; retry %s/%s in %.2fs",
                            attempt + 1, MAX_BUSY_RETRIES, delay)
                time.sleep(delay)

    def execute(self, sql, parameters=(), /):
        return self._with_busy_retry(super().execute, sql, parameters)

    def executemany(self, sql, parameters, /):
        return self._with_busy_retry(super().executemany, sql, parameters)

    def executescript(self, sql_script, /):
        return self._with_busy_retry(super().executescript, sql_script)

    def commit(self):
        return self._with_busy_retry(super().commit)


def connect(path: str, *, timeout: float = 30, **kwargs) -> RetryingConnection:
    """Open a retrying connection with a consistent busy timeout."""
    conn = sqlite3.connect(path, timeout=timeout, factory=RetryingConnection,
                           **kwargs)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn
