# ============================================================
# dedup.py — 已发送消息 ID 去重（有序列表 + 集合双结构）
# ============================================================
import json
import os
import shutil

# 统一通过 cfg.X 访问，热重载后 sent_ids_max 等标量才能生效
import config.config as cfg
from src.logger import log_all


def _id_file(group_type: str, m_id: str) -> str:
    os.makedirs(cfg.SENT_IDS_DIR, exist_ok=True)
    return os.path.join(cfg.SENT_IDS_DIR, f"sent_{group_type}_{m_id}.json")


def _db_save_sent_id(group_type: str, m_id: str, msg_id: str) -> None:
    from src.archive import init_db
    conn = init_db()
    if not conn:
        return
    import time
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent_ids (group_type, m_id, msg_id, created_at) VALUES (?, ?, ?, ?);",
                (group_type, m_id, msg_id, time.time())
            )
    except Exception as e:
        log_all(f"⚠️ SQLite 保存 sent_id 失败: {e}", is_debug=True)


def load_sent_ids(group_type: str, m_id: str) -> tuple[list[str], set[str]]:
    """
    优先从 SQLite 数据库加载已发送 ID，退回磁盘旧 JSON 文件。
    返回 (有序列表, 快速查找集合)。
    """
    from src.archive import init_db
    conn = init_db()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT msg_id FROM sent_ids WHERE group_type = ? AND m_id = ? ORDER BY created_at ASC",
                (group_type, m_id)
            )
            rows = cursor.fetchall()
            if rows:
                ids = [r[0] for r in rows]
                trimmed = ids[-cfg.SENT_IDS_MAX:]
                return trimmed, set(trimmed)
        except Exception:
            pass

    path = _id_file(group_type, m_id)
    if not os.path.exists(path):
        return [], set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            ids: list[str] = json.load(f)
        trimmed = ids[-cfg.SENT_IDS_MAX:]
        # 迁移旧数据至 SQLite
        if conn and trimmed:
            import time
            now = time.time()
            try:
                with conn:
                    conn.executemany(
                        "INSERT OR IGNORE INTO sent_ids (group_type, m_id, msg_id, created_at) VALUES (?, ?, ?, ?);",
                        [(group_type, m_id, mid, now) for mid in trimmed]
                    )
            except Exception:
                pass
        return trimmed, set(trimmed)
    except Exception:
        bak = path + ".bak"
        try:
            shutil.copy2(path, bak)
            log_all(f"⚠️ 已发送 ID 文件损坏，已备份至 {bak}", is_error=True)
        except OSError:
            pass
        return [], set()


def _do_write_sent_ids(path: str, tmp: str, data: list[str], group_type: str, m_id: str, msg_id: str) -> None:
    _db_save_sent_id(group_type, m_id, msg_id)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        log_all(f"🚨 已发送 ID 写入失败: {e}", is_error=True)
        try:
            os.remove(tmp)
        except OSError:
            pass


def save_sent_id(
    group_type: str,
    m_id: str,
    msg_id: str,
    id_list: list[str],
    id_set: set[str],
) -> None:
    """
    记录新 ID 并持久化（自动双写至 SQLite DB）。
    """
    if msg_id in id_set:
        return

    id_list.append(msg_id)
    id_set.add(msg_id)

    while len(id_list) > cfg.SENT_IDS_MAX:
        old = id_list.pop(0)
        id_set.discard(old)

    path = _id_file(group_type, m_id)
    tmp  = path + ".tmp"
    snapshot = list(id_list)

    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        loop.create_task(asyncio.to_thread(_do_write_sent_ids, path, tmp, snapshot, group_type, m_id, msg_id))
    else:
        _do_write_sent_ids(path, tmp, snapshot, group_type, m_id, msg_id)


