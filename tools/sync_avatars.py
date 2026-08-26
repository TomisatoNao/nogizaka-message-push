#!/usr/bin/env python3
"""tools/sync_avatars.py — 同步并下载三坂官方成员与博客作者头像至本地数据库与 data/avatars/"""
import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.avatar_manager import sync_all_avatars, get_avatar_db
import config.config as cfg


async def main():
    print("=== 开始同步坂道成员官方写真与博客作者头像 ===")
    try:
        cfg._load_env_and_json()
    except Exception:
        pass

    # 1. 同步官方博客与官网成员头像
    res = await sync_all_avatars(force=True)
    print(f"✅ 官方头像抓取完成: 索引 {res['total']} 人, 下载 {res['downloaded']} 张头像")

    # 2. 从本地已订阅的 Message 账号 / member_subscriptions 提取 thumbnail
    conn = get_avatar_db()
    try:
        from src.auth import get_auth_db
        auth_conn = get_auth_db()
        rows = auth_conn.execute("SELECT member_name, account_id FROM member_subscriptions").fetchall()
        print(f"ℹ️ 扫描到 {len(rows)} 条本地 Message 订阅历史记录")
    except Exception as e:
        print(f"⚠️ 读取本地 Message 订阅记录失败: {e}")

    # 打印统计
    total_db = conn.execute("SELECT COUNT(*) FROM member_avatars").fetchone()[0]
    total_cached = conn.execute("SELECT COUNT(*) FROM member_avatars WHERE local_file != ''").fetchone()[0]
    print(f"📊 数据库当前共记录 {total_db} 位成员，其中 {total_cached} 位已就绪本地高清头像！")


if __name__ == "__main__":
    asyncio.run(main())
