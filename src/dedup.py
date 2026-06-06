# ============================================================
# dedup.py — 已发送消息 ID 去重（有序列表 + 集合双结构）
# ============================================================
import json
import os
import shutil

from config.config import SENT_IDS_DIR, SENT_IDS_MAX
from src.logger import log_all


def _id_file(group_type: str, m_id: str) -> str:
    os.makedirs(SENT_IDS_DIR, exist_ok=True)
    return os.path.join(SENT_IDS_DIR, f"sent_{group_type}_{m_id}.json")


def load_sent_ids(group_type: str, m_id: str) -> tuple[list[str], set[str]]:
    """
    从磁盘加载已发送 ID。
    返回 (有序列表, 快速查找集合)：
      - 列表保留插入顺序（旧→新），用于超限时精准淘汰最旧条目
      - 集合用于 O(1) 查重
    """
    path = _id_file(group_type, m_id)
    if not os.path.exists(path):
        return [], set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            ids: list[str] = json.load(f)
        trimmed = ids[-SENT_IDS_MAX:]
        return trimmed, set(trimmed)
    except Exception:
        # 文件损坏时保留备份，避免静默丢弃全部去重历史
        bak = path + ".bak"
        try:
            shutil.copy2(path, bak)
            log_all(f"⚠️ 已发送 ID 文件损坏，已备份至 {bak}", is_error=True)
        except OSError:
            pass
        return [], set()


def save_sent_id(
    group_type: str,
    m_id: str,
    msg_id: str,
    id_list: list[str],
    id_set: set[str],
) -> None:
    """
    记录新 ID 并持久化。
    同步更新传入的 id_list / id_set，超出 SENT_IDS_MAX 时
    从列表头部（最旧）淘汰，保证截断结果始终是最新的 N 条。
    """
    if msg_id in id_set:
        return

    id_list.append(msg_id)
    id_set.add(msg_id)

    while len(id_list) > SENT_IDS_MAX:
        old = id_list.pop(0)
        id_set.discard(old)

    path = _id_file(group_type, m_id)
    tmp  = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(id_list, f)
        os.replace(tmp, path)
    except Exception as e:
        log_all(f"🚨 已发送 ID 写入失败: {e}", is_error=True)
        try:
            os.remove(tmp)
        except OSError:
            pass
