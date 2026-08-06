#!/usr/bin/env python3
# ============================================================
# sync_archive_db.py — 命令行工具：把 data/archive/ 下的所有 JSON 归档同步至 SQLite 数据库
# ============================================================
import sys
import time
from pathlib import Path

# 确保能 import 项目根目录的 src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.archive import get_db_path, init_db, sync_all_to_sqlite


def main() -> None:
    print("=" * 60)
    print("乃木坂/坂道 Message 消息归档 — SQLite 数据库全量同步工具")
    print("=" * 60)

    
    db_path = get_db_path()
    print(f"📍 目标数据库路径: {db_path}")

    start_time = time.time()
    conn = init_db()
    if not conn:
        print("❌ SQLite 数据库初始化失败，请检查文件权限。")
        sys.exit(1)

    print("🚀 开始扫描并同步磁盘现有的全量历史归档数据...")
    total_synced = sync_all_to_sqlite()
    elapsed = time.time() - start_time

    print("-" * 60)
    print("✅ 全量同步完成！")
    print(f"📊 共计同步消息: {total_synced} 条")

    print(f"⏱️ 耗时: {elapsed:.2f} 秒")
    print(f"💾 数据库文件大小: {db_path.stat().st_size / (1024 * 1024):.2f} MB")
    print("=" * 60)


if __name__ == "__main__":
    main()
