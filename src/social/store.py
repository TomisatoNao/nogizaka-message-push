"""
social/store.py — 社交平台去重与直播会话状态（SQLite / data/social_state.db）

与既有 data/sync_state.json **并存**，分工明确：

  * SocialStore（本模块）—— fetch 阶段就跳过「已发送」的项，避免重复下载媒体；
    同时记录直播会话状态，实现「同一场直播只录一次」和「程序重启后恢复监控」。
  * SyncState（既有 sync_manager）—— 发送层去重，保持原样不动。

两者都只在「成功转发」后打标，所以失败项下轮会重新处理，语义一致。

表结构：
    seen_items    (platform, item_id) 主键；seen_at 首次看到、sent_at 成功推送
    live_sessions session_key 主键；记录录制进程 pid / 状态 / 输出目录
"""

import logging
import os
import sqlite3
import threading
import time

from src.social.sqlite_utils import connect as sqlite_connect

log = logging.getLogger("collink")

SOCIAL_DB_PATH = os.path.join("data", "social_state.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    platform   TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    account    TEXT DEFAULT '',
    kind       TEXT DEFAULT '',
    seen_at    REAL DEFAULT 0,
    sent_at    REAL DEFAULT 0,
    PRIMARY KEY (platform, item_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_platform_account
    ON seen_items (platform, account);

CREATE TABLE IF NOT EXISTS live_sessions (
    session_key    TEXT PRIMARY KEY,
    platform       TEXT DEFAULT '',
    account        TEXT DEFAULT '',
    room_id        TEXT DEFAULT '',
    title          TEXT DEFAULT '',
    status         TEXT DEFAULT '',   -- recording | finished | crashed
    started_at     REAL DEFAULT 0,
    ended_at       REAL DEFAULT 0,
    output_dir     TEXT DEFAULT '',
    pid            INTEGER DEFAULT 0,
    notified_start INTEGER DEFAULT 0,
    notified_end   INTEGER DEFAULT 0,
    delivery_attempts INTEGER DEFAULT 0,
    updated_at     REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS account_bootstrap (
    platform    TEXT NOT NULL,
    account     TEXT NOT NULL,
    kind        TEXT DEFAULT '',
    done_at     REAL DEFAULT 0,
    PRIMARY KEY (platform, account, kind)
);

CREATE TABLE IF NOT EXISTS delivery_routes (
    platform TEXT NOT NULL,
    item_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    delivered_at REAL DEFAULT 0,
    last_error TEXT DEFAULT '',
    PRIMARY KEY (platform, item_id, route_id)
);
"""


def _pid_alive(pid: int) -> bool:
    """跨平台判断 pid 是否仍在运行（用于识别崩溃遗留的录制会话）。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # Windows：用 tasklist 过滤 PID，避免引入额外依赖
        import subprocess  # nosec B404
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],  # nosec B607, B603
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class SocialStore:
    """线程安全的 SQLite 状态存储（社交平台专用）。"""

    def __init__(self, path: str = SOCIAL_DB_PATH):
        self._path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # check_same_thread=False + 自带锁：允许多个平台线程共用一个连接
        self._conn = sqlite_connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(_SCHEMA)
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(live_sessions)")
            }
            if "delivery_attempts" not in columns:
                self._conn.execute(
                    "ALTER TABLE live_sessions "
                    "ADD COLUMN delivery_attempts INTEGER DEFAULT 0"
                )
            # WAL 提升并发读写表现，长期运行更稳
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            self._conn.commit()
        log.debug("[social:store] SQLite 就绪: %s", path)

    # ── 动态 / Story 去重 ────────────────────────────────

    def is_sent(self, platform: str, item_id: str) -> bool:
        """该条动态 / Story 是否已成功推送过。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT sent_at FROM seen_items WHERE platform=? AND item_id=?",
                (platform, item_id),
            ).fetchone()
        return bool(row and row["sent_at"])

    def is_seen(self, platform: str, item_id: str) -> bool:
        """该条是否曾被抓取过（不论是否推送成功）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_items WHERE platform=? AND item_id=?",
                (platform, item_id),
            ).fetchone()
        return row is not None

    def mark_seen(self, platform: str, item_id: str,
                  account: str = "", kind: str = "") -> None:
        """标记「已抓取」，不影响 sent_at（失败项下轮仍会重试推送）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO seen_items (platform, item_id, account, kind, seen_at, sent_at) "
                "VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(platform, item_id) DO NOTHING",
                (platform, item_id, account, kind, time.time()),
            )
            self._conn.commit()

    def mark_sent(self, platform: str, item_id: str,
                  account: str = "", kind: str = "") -> None:
        """标记「已成功推送」—— 之后 fetch 阶段会直接跳过，不再下载媒体。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO seen_items (platform, item_id, account, kind, seen_at, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(platform, item_id) DO UPDATE SET sent_at=excluded.sent_at",
                (platform, item_id, account, kind, now, now),
            )
            self._conn.commit()

    def count(self, platform: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM seen_items WHERE platform=?", (platform,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def delivered_routes(self, platform: str, item_id: str) -> set[str]:
        """返回已成功的路由；失败路由保持为空以供下轮单独补发。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT route_id FROM delivery_routes WHERE platform=? AND item_id=? AND delivered_at>0",
                (platform, item_id),
            ).fetchall()
        return {str(row["route_id"]) for row in rows}

    def mark_route_result(self, platform: str, item_id: str, route_id: str,
                          ok: bool, error: str = "") -> None:
        now = time.time() if ok else 0
        with self._lock:
            self._conn.execute(
                "INSERT INTO delivery_routes (platform,item_id,route_id,delivered_at,last_error) VALUES (?,?,?,?,?) "
                "ON CONFLICT(platform,item_id,route_id) DO UPDATE SET "
                "delivered_at=CASE WHEN excluded.delivered_at>0 THEN excluded.delivered_at ELSE delivery_routes.delivered_at END, "
                "last_error=CASE WHEN excluded.delivered_at>0 THEN '' ELSE excluded.last_error END",
                (platform, item_id, route_id, now, error[:120]),
            )
            self._conn.commit()

    # ── 首次运行标记（避免历史内容刷屏）───────────────────

    def is_bootstrapped(self, platform: str, account: str, kind: str = "") -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM account_bootstrap WHERE platform=? AND account=? AND kind=?",
                (platform, account, kind),
            ).fetchone()
        return row is not None

    def mark_bootstrapped(self, platform: str, account: str, kind: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO account_bootstrap (platform, account, kind, done_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (platform, account, kind, time.time()),
            )
            self._conn.commit()

    def get_bootstrap_time(self, platform: str, account: str, kind: str = "") -> float:
        """返回该账号首次监控的时间戳，未 bootstrap 则返回 0。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT done_at FROM account_bootstrap WHERE platform=? AND account=? AND kind=?",
                (platform, account, kind),
            ).fetchone()
        return float(row["done_at"]) if row else 0

    # ── 直播会话状态 ─────────────────────────────────────

    def get_live_session(self, session_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM live_sessions WHERE session_key=?", (session_key,),
            ).fetchone()
        return dict(row) if row else None

    def is_recording(self, session_key: str) -> bool:
        """该场直播是否正在录制中（且录制进程确实存活）。

        程序重启后用它判断是否需要恢复：
          status=recording 且 pid 存活 → 别的进程在录，不要重复开
          status=recording 但 pid 已死 → 上次崩溃遗留，标记 crashed 后允许续录
        """
        sess = self.get_live_session(session_key)
        if not sess or sess.get("status") != "recording":
            return False
        pid = int(sess.get("pid") or 0)
        if pid and pid != os.getpid() and _pid_alive(pid):
            return True
        if pid == os.getpid():
            return True
        # pid 已不存在 → 上次异常退出留下的脏状态
        self.update_live_session(session_key, status="crashed")
        log.warning("[social:store] 检测到崩溃遗留的录制会话 %s（pid=%s 已不存在）",
                    session_key, pid)
        return False

    def begin_live_session(self, session_key: str, *, platform: str, account: str,
                           room_id: str, title: str, output_dir: str,
                           started_at: float | None = None) -> dict:
        """创建或复用一场直播会话（同 session_key 幂等，实现防重复录制）。"""
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM live_sessions WHERE session_key=?", (session_key,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO live_sessions (session_key, platform, account, room_id, "
                    "title, status, started_at, ended_at, output_dir, pid, "
                    "notified_start, notified_end, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'recording', ?, 0, ?, ?, 0, 0, ?)",
                    (session_key, platform, account, room_id, title,
                     started_at or now, output_dir, os.getpid(), now),
                )
            else:
                # 续录：保留原始开播时间与通知标记，只更新状态与 pid
                self._conn.execute(
                    "UPDATE live_sessions SET status='recording', pid=?, output_dir=?, "
                    "title=?, updated_at=? WHERE session_key=?",
                    (os.getpid(), output_dir or existing["output_dir"],
                     title or existing["title"], now, session_key),
                )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM live_sessions WHERE session_key=?", (session_key,),
            ).fetchone()
        return dict(row)

    def update_live_session(self, session_key: str, **fields) -> None:
        """更新会话字段（白名单校验，避免 SQL 注入与拼错列名）。"""
        allowed = {"status", "ended_at", "output_dir", "pid", "title",
                   "notified_start", "notified_end", "delivery_attempts",
                   "room_id", "started_at"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        sets.append("updated_at=?")
        vals.append(time.time())
        vals.append(session_key)
        with self._lock:
            self._conn.execute(
                f"UPDATE live_sessions SET {', '.join(sets)} WHERE session_key=?", vals,  # nosec B608
            )
            self._conn.commit()

    def finish_live_session(self, session_key: str, ended_at: float | None = None) -> None:
        self.update_live_session(
            session_key, status="finished", ended_at=ended_at or time.time(), pid=0,
        )

    def stale_recording_sessions(self, platform: str = "tiktok_live") -> list[dict]:
        """返回状态为 recording 但进程已死的会话（程序重启后用于恢复）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM live_sessions WHERE platform=? AND status='recording'",
                (platform,),
            ).fetchall()
        out = []
        for r in rows:
            pid = int(r["pid"] or 0)
            if pid != os.getpid() and not _pid_alive(pid):
                out.append(dict(r))
        return out

    def pending_finished_sessions(self, platform: str = "tiktok_live") -> list[dict]:
        """返回录制已完成但完成通知尚未成功送达的会话。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM live_sessions WHERE platform=? AND status='finished' "
                "AND notified_end=0 AND COALESCE(delivery_attempts, 0) < 3 "
                "ORDER BY ended_at",
                (platform,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
