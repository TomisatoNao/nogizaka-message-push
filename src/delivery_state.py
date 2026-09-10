"""成员消息的按路由投递状态。

成功路由写入 SQLite，失败路由进入持久化待投递队列；消息在下一轮或
服务重启后只会投递到尚未成功的路由。路由被停用/删除时，待投递记录会
保留为可恢复的暂停状态，不参与当前重试和积压统计；重新启用同一稳定
路由 ID 后自动恢复。状态表只保存消息/路由标识和时间，不复制凭据、
正文或媒体内容。
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS delivery_pending_routes (
                group_type TEXT NOT NULL,
                member_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                message_time TEXT NOT NULL,
                last_error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                first_seen_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                route_state TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (group_type, member_id, message_id, route_id)
            )
        """)
        # 兼容阶段 4 首版已经创建的待投递表：SQLite 不支持 IF NOT
        # EXISTS 的 ADD COLUMN，因此先检查列再做一次性迁移。
        pending_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(delivery_pending_routes)").fetchall()
        }
        if "route_state" not in pending_columns:
            conn.execute(
                "ALTER TABLE delivery_pending_routes "
                "ADD COLUMN route_state TEXT NOT NULL DEFAULT 'pending'"
            )
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_delivery_pending_member
            ON delivery_pending_routes(group_type, member_id, message_time, route_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_delivery_pending_state
            ON delivery_pending_routes(group_type, member_id, route_state, message_time, route_id)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS delivery_lane_state (
                group_type TEXT NOT NULL,
                member_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                last_success_message_id TEXT,
                last_success_time TEXT,
                blocked_message_id TEXT,
                blocked_message_time TEXT,
                last_error TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (group_type, member_id, route_id)
            )
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


def mark_successful_routes(
    group_type: str,
    member_id: str,
    message_id: str,
    route_ids: set[str],
    message_time: str | None = None,
) -> None:
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
            conn.executemany(
                "DELETE FROM delivery_pending_routes WHERE group_type=? AND member_id=? AND message_id=? AND route_id=?",
                [(group_type, member_id, message_id, route_id) for route_id in route_ids],
            )
            now = time.time()
            conn.executemany(
                """
                INSERT INTO delivery_lane_state (
                    group_type, member_id, route_id, last_success_message_id,
                    last_success_time, blocked_message_id, blocked_message_time,
                    last_error, consecutive_failures, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?)
                ON CONFLICT(group_type, member_id, route_id) DO UPDATE SET
                    last_success_message_id=excluded.last_success_message_id,
                    last_success_time=excluded.last_success_time,
                    blocked_message_id=CASE
                        WHEN delivery_lane_state.blocked_message_id = excluded.last_success_message_id
                        THEN NULL ELSE delivery_lane_state.blocked_message_id END,
                    blocked_message_time=CASE
                        WHEN delivery_lane_state.blocked_message_id = excluded.last_success_message_id
                        THEN NULL ELSE delivery_lane_state.blocked_message_time END,
                    last_error=CASE
                        WHEN delivery_lane_state.blocked_message_id = excluded.last_success_message_id
                        THEN NULL ELSE delivery_lane_state.last_error END,
                    consecutive_failures=CASE
                        WHEN delivery_lane_state.blocked_message_id = excluded.last_success_message_id
                        THEN 0 ELSE delivery_lane_state.consecutive_failures END,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        group_type,
                        member_id,
                        route_id,
                        message_id,
                        str(message_time or ""),
                        now,
                    )
                    for route_id in route_ids
                ],
            )
            # 当前消息成功后，lane 可能仍有更晚的待补偿消息；把阻塞指针
            # 前移到最早 pending，而不是简单清空整个 lane 状态。
            for route_id in route_ids:
                next_pending = conn.execute(
                    """
                    SELECT message_id, message_time, last_error
                    FROM delivery_pending_routes
                    WHERE group_type=? AND member_id=? AND route_id=?
                      AND route_state='pending'
                    ORDER BY message_time ASC, message_id ASC
                    LIMIT 1
                    """,
                    (group_type, member_id, route_id),
                ).fetchone()
                if next_pending:
                    conn.execute(
                        """
                        UPDATE delivery_lane_state
                        SET blocked_message_id=?, blocked_message_time=?,
                            last_error=COALESCE(?, last_error), updated_at=?
                        WHERE group_type=? AND member_id=? AND route_id=?
                        """,
                        (
                            str(next_pending[0]),
                            str(next_pending[1]),
                            next_pending[2],
                            now,
                            group_type,
                            member_id,
                            route_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE delivery_lane_state
                        SET blocked_message_id=NULL, blocked_message_time=NULL,
                            last_error=NULL, consecutive_failures=0, updated_at=?
                        WHERE group_type=? AND member_id=? AND route_id=?
                        """,
                        (now, group_type, member_id, route_id),
                    )
    except sqlite3.Error as exc:
        log_all(f"⚠️ 保存路由投递状态失败: {type(exc).__name__}", is_error=True)


def mark_pending_routes(
    group_type: str,
    member_id: str,
    message_id: str,
    message_time: str,
    route_ids: set[str],
    errors: dict[str, str] | None = None,
    attempted_route_ids: set[str] | None = None,
) -> None:
    """登记尚未完成的路由，并保留每个路由最早的阻塞消息。

    ``attempted_route_ids`` 用于区分真实失败和因前序消息失败而暂时跳过
    的路由；只有真实失败才增加 lane 的连续失败次数。
    """

    if not route_ids:
        return
    conn = _conn()
    if conn is None:
        return
    errors = errors or {}
    attempted_route_ids = attempted_route_ids or set()
    now = time.time()
    try:
        with conn:
            conn.executemany(
                """
                INSERT INTO delivery_pending_routes (
                    group_type, member_id, message_id, route_id, message_time,
                    last_error, attempts, first_seen_at, updated_at, route_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(group_type, member_id, message_id, route_id) DO UPDATE SET
                    message_time=excluded.message_time,
                    last_error=COALESCE(excluded.last_error, delivery_pending_routes.last_error),
                    attempts=delivery_pending_routes.attempts + excluded.attempts,
                    route_state='pending',
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        group_type,
                        member_id,
                        message_id,
                        route_id,
                        str(message_time or ""),
                        errors.get(route_id),
                        1 if route_id in attempted_route_ids else 0,
                        now,
                        now,
                    )
                    for route_id in route_ids
                ],
            )
            for route_id in route_ids:
                attempted = route_id in attempted_route_ids
                error = errors.get(route_id)
                conn.execute(
                    """
                    INSERT INTO delivery_lane_state (
                        group_type, member_id, route_id, last_success_message_id,
                        last_success_time, blocked_message_id, blocked_message_time,
                        last_error, consecutive_failures, updated_at
                    ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_type, member_id, route_id) DO UPDATE SET
                        blocked_message_id=CASE
                            WHEN delivery_lane_state.blocked_message_id IS NULL
                              OR delivery_lane_state.blocked_message_time > excluded.blocked_message_time
                            THEN excluded.blocked_message_id
                            ELSE delivery_lane_state.blocked_message_id END,
                        blocked_message_time=CASE
                            WHEN delivery_lane_state.blocked_message_time IS NULL
                              OR delivery_lane_state.blocked_message_time > excluded.blocked_message_time
                            THEN excluded.blocked_message_time
                            ELSE delivery_lane_state.blocked_message_time END,
                        last_error=CASE WHEN excluded.last_error IS NOT NULL
                            THEN excluded.last_error ELSE delivery_lane_state.last_error END,
                        consecutive_failures=delivery_lane_state.consecutive_failures + excluded.consecutive_failures,
                        updated_at=excluded.updated_at
                    """,
                    (
                        group_type,
                        member_id,
                        route_id,
                        message_id,
                        str(message_time or ""),
                        str(error) if error else None,
                        1 if attempted else 0,
                        now,
                    ),
                )
    except sqlite3.Error as exc:
        log_all(f"⚠️ 保存待投递路由失败: {type(exc).__name__}", is_error=True)


def reconcile_pending_routes(
    group_type: str,
    member_id: str,
    active_route_ids: set[str] | list[str] | tuple[str, ...],
) -> dict[str, int]:
    """同步成员当前有效路由，暂停或恢复其历史待投递记录。

    稳定的 ``route_id`` 是恢复依据：配置暂时关闭、成员过滤变化或路由
    删除时不丢弃历史记录，只把不在当前匹配集合中的记录标记为
    ``suspended``；再次启用同一 ID 时恢复为 ``pending``。这样既不会在
    关闭期间反复回放，也不会把永久保留的失败记录伪装成当前积压。
    """

    conn = _conn()
    if conn is None:
        return {"suspended": 0, "resumed": 0}
    active = {str(route_id) for route_id in active_route_ids if str(route_id)}
    try:
        rows = conn.execute(
            "SELECT route_id, route_state FROM delivery_pending_routes "
            "WHERE group_type=? AND member_id=?",
            (group_type, member_id),
        ).fetchall()
        suspended = 0
        resumed = 0
        with conn:
            for row in rows:
                route_id = str(row[0])
                previous = str(row[1] or "pending")
                desired = "pending" if route_id in active else "suspended"
                if previous == desired:
                    continue
                conn.execute(
                    "UPDATE delivery_pending_routes SET route_state=?, updated_at=? "
                    "WHERE group_type=? AND member_id=? AND route_id=?",
                    (desired, time.time(), group_type, member_id, route_id),
                )
                if desired == "suspended":
                    suspended += 1
                else:
                    resumed += 1
        return {"suspended": suspended, "resumed": resumed}
    except sqlite3.Error as exc:
        log_all(f"⚠️ 同步待投递路由状态失败: {type(exc).__name__}", is_error=True)
        return {"suspended": 0, "resumed": 0}


def pending_routes_for_member(
    group_type: str,
    member_id: str,
    *,
    route_id: str | None = None,
    limit: int = 1000,
    include_suspended: bool = False,
) -> list[dict[str, object]]:
    """按消息时间返回成员待投递路由，供恢复循环回放。

    默认只返回当前可投递的 ``pending`` 记录；诊断/迁移工具可通过
    ``include_suspended`` 查看被停用路由的保留状态。
    """

    conn = _conn()
    if conn is None:
        return []
    try:
        params: list[object] = [group_type, member_id]
        where = "group_type=? AND member_id=?"
        if not include_suspended:
            where += " AND route_state='pending'"
        if route_id:
            where += " AND route_id=?"
            params.append(route_id)
        params.append(max(1, min(10000, int(limit))))
        rows = conn.execute(
            f"""
            SELECT message_id, route_id, message_time, last_error, attempts,
                   first_seen_at, updated_at, route_state
            FROM delivery_pending_routes
            WHERE {where}
            ORDER BY message_time ASC, message_id ASC, route_id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            {
                "message_id": str(row[0]),
                "route_id": str(row[1]),
                "message_time": str(row[2]),
                "last_error": row[3],
                "attempts": int(row[4] or 0),
                "first_seen_at": row[5],
                "updated_at": row[6],
                "route_state": str(row[7] or "pending"),
            }
            for row in rows
        ]
    except (sqlite3.Error, TypeError, ValueError) as exc:
        log_all(f"⚠️ 读取待投递路由失败: {type(exc).__name__}", is_error=True)
        return []


def route_lane_snapshot(
    group_type: str,
    member_id: str,
    *,
    route_id: str | None = None,
) -> list[dict[str, object]]:
    """返回成员各路由水位与阻塞状态，供管理端/诊断使用。"""

    conn = _conn()
    if conn is None:
        return []
    try:
        params: list[object] = [group_type, member_id]
        where = "group_type=? AND member_id=?"
        if route_id:
            where += " AND route_id=?"
            params.append(route_id)
        rows = conn.execute(
            f"""
            SELECT route_id, last_success_message_id, last_success_time,
                   blocked_message_id, blocked_message_time, last_error,
                   consecutive_failures, updated_at,
                   (SELECT COUNT(*) FROM delivery_pending_routes p
                    WHERE p.group_type=delivery_lane_state.group_type
                      AND p.member_id=delivery_lane_state.member_id
                      AND p.route_id=delivery_lane_state.route_id
                      AND p.route_state='pending') AS pending_count,
                   (SELECT COUNT(*) FROM delivery_pending_routes p
                    WHERE p.group_type=delivery_lane_state.group_type
                      AND p.member_id=delivery_lane_state.member_id
                      AND p.route_id=delivery_lane_state.route_id
                      AND p.route_state='suspended') AS suspended_count
            FROM delivery_lane_state
            WHERE {where}
            ORDER BY route_id ASC
            """,
            params,
        ).fetchall()
        return [
            {
                "route_id": str(row[0]),
                "last_success_message_id": row[1],
                "last_success_time": row[2],
                "blocked_message_id": row[3],
                "blocked_message_time": row[4],
                "last_error": row[5],
                "consecutive_failures": int(row[6] or 0),
                "updated_at": row[7],
                "pending_count": int(row[8] or 0),
                "suspended_count": int(row[9] or 0),
            }
            for row in rows
        ]
    except sqlite3.Error as exc:
        log_all(f"⚠️ 读取路由 lane 状态失败: {type(exc).__name__}", is_error=True)
        return []


def pending_route_count(
    group_type: str | None = None,
    member_id: str | None = None,
    *,
    include_suspended: bool = False,
) -> int:
    """统计待投递路由数；参数为空时统计全局。

    默认排除被停用/删除路由的保留记录；诊断工具可显式计入暂停状态。
    """

    conn = _conn()
    if conn is None:
        return 0
    try:
        clauses: list[str] = [] if include_suspended else ["route_state='pending'"]
        params: list[object] = []
        if group_type is not None:
            clauses.append("group_type=?")
            params.append(group_type)
        if member_id is not None:
            clauses.append("member_id=?")
            params.append(member_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = conn.execute(
            f"SELECT COUNT(*) FROM delivery_pending_routes{where}", params
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error as exc:
        log_all(f"⚠️ 统计待投递路由失败: {type(exc).__name__}", is_error=True)
        return 0


def pending_backlog_summary(
    group_type: str | None = None,
    member_id: str | None = None,
) -> dict[str, int]:
    """返回当前待补偿与暂停保留的路由摘要，供状态页/诊断使用。

    ``routes/messages/members`` 只统计会参与下一轮补偿的 pending 记录；
    ``suspended_*`` 记录被关闭或删除路由的保留数据，方便管理员判断是否
    需要恢复原 route_id，而不会把它误报为当前活动积压。
    """

    conn = _conn()
    if conn is None:
        return {
            "routes": 0,
            "messages": 0,
            "members": 0,
            "suspended_routes": 0,
            "suspended_messages": 0,
            "suspended_members": 0,
        }
    try:
        clauses: list[str] = []
        params: list[object] = []
        if group_type is not None:
            clauses.append("group_type=?")
            params.append(group_type)
        if member_id is not None:
            clauses.append("member_id=?")
            params.append(member_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN route_state='pending' THEN 1 ELSE 0 END),
                COUNT(DISTINCT CASE WHEN route_state='pending'
                    THEN group_type || ':' || member_id || ':' || message_id END),
                COUNT(DISTINCT CASE WHEN route_state='pending'
                    THEN group_type || ':' || member_id END),
                SUM(CASE WHEN route_state='suspended' THEN 1 ELSE 0 END),
                COUNT(DISTINCT CASE WHEN route_state='suspended'
                    THEN group_type || ':' || member_id || ':' || message_id END),
                COUNT(DISTINCT CASE WHEN route_state='suspended'
                    THEN group_type || ':' || member_id END)
            FROM delivery_pending_routes{where}
            """,
            params,
        ).fetchone()
        return {
            "routes": int(row[0] or 0) if row else 0,
            "messages": int(row[1] or 0) if row else 0,
            "members": int(row[2] or 0) if row else 0,
            "suspended_routes": int(row[3] or 0) if row else 0,
            "suspended_messages": int(row[4] or 0) if row else 0,
            "suspended_members": int(row[5] or 0) if row else 0,
        }
    except sqlite3.Error as exc:
        log_all(f"⚠️ 统计待补偿摘要失败: {type(exc).__name__}", is_error=True)
        return {
            "routes": 0,
            "messages": 0,
            "members": 0,
            "suspended_routes": 0,
            "suspended_messages": 0,
            "suspended_members": 0,
        }
