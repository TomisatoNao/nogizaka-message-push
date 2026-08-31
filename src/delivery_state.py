"""成员消息的按路由投递状态。

成功路由写入 SQLite，失败路由不写入；消息在下一轮出现时只会投递到
尚未成功的路由。该表是对 sent_ids 的补充，不保存凭据或消息正文。
"""
from __future__ import annotations

import sqlite3
import time

from src.logger import log_all


def _conn() -> sqlite3.Connection | None:
    from src.archive import init_db
    conn = init_db()
    if conn is None:
        return None
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS delivered_routes (
                group_type TEXT NOT NULL,
                member_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                delivered_at REAL NOT NULL,
                PRIMARY KEY (group_type, member_id, message_id, route_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_delivered_routes_message
            ON delivered_routes(group_type, member_id, message_id)
        """)
        conn.commit()
        return conn
    except sqlite3.Error as exc:
        log_all(f"⚠️ 初始化路由投递状态失败: {type(exc).__name__}", is_error=True)
        return None


def successful_routes(group_type: str, member_id: str, message_id: str) -> set[str]:
    conn = _conn()
    if conn is None:
        return set()
    try:
        rows = conn.execute(
            "SELECT route_id FROM delivered_routes WHERE group_type=? AND member_id=? AND message_id=?",
            (group_type, member_id, message_id),
        ).fetchall()
        return {str(row[0]) for row in rows}
    except sqlite3.Error as exc:
        log_all(f"⚠️ 读取路由投递状态失败: {type(exc).__name__}", is_error=True)
        return set()


def mark_successful_routes(group_type: str, member_id: str, message_id: str,
                           route_ids: set[str]) -> None:
    if not route_ids:
        return
    conn = _conn()
    if conn is None:
        return
    try:
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO delivered_routes VALUES (?, ?, ?, ?, ?)",
                [(group_type, member_id, message_id, route_id, time.time()) for route_id in route_ids],
            )
    except sqlite3.Error as exc:
        log_all(f"⚠️ 保存路由投递状态失败: {type(exc).__name__}", is_error=True)
