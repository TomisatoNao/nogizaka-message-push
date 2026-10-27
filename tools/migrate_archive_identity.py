"""Archive message identity and scoped SQLite index migration tool.

Safely repairs cross-member ID collisions, content mismatches, and month drift
without wiping unrelated tables or deleting raw JSON/media archives.

Usage:
    python tools/migrate_archive_identity.py --dry-run
    python tools/migrate_archive_identity.py --apply
    python tools/migrate_archive_identity.py --apply --backup-dir /path/to/backups
"""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src import archive  # noqa: E402
from src.logger import log_all  # noqa: E402


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def plan_identity_migration(archive_dir: Path | None = None) -> dict:
    """Scan all monthly archive JSON files and compare with SQLite messages index.

    Returns a detailed plan dictionary with categorized fixes.
    """
    root = archive_dir or archive.archive_root()
    if not root.is_dir():
        return {
            "ok": False,
            "error": f"归档根目录不存在: {root}",
            "total_json_files": 0,
            "total_json_records": 0,
            "to_insert": [],
            "to_update": [],
            "to_relocate_month": [],
            "isolated_in_db": [],
        }

    conn = archive.init_db()
    if not conn:
        return {
            "ok": False,
            "error": "无法初始化 SQLite 归档数据库",
            "total_json_files": 0,
            "total_json_records": 0,
            "to_insert": [],
            "to_update": [],
            "to_relocate_month": [],
            "isolated_in_db": [],
        }

    # 读取当前数据库全量现有索引行: {(member_dir, msg_id): (row_dict)}
    cur = conn.cursor()
    cur.execute(
        "SELECT id, member_name, member_dir, year, month, type, published_at, updated_at, "
        "text, translation, tags, local_file, raw_json FROM messages;"
    )
    existing_index: dict[tuple[str, str], dict] = {}
    for r in cur.fetchall():
        mid = str(r[0] or "")
        mdir = str(r[2] or "")
        existing_index[(mdir, mid)] = {
            "id": mid,
            "member_name": str(r[1] or ""),
            "member_dir": mdir,
            "year": r[3],
            "month": r[4],
            "type": r[5] or "text",
            "published_at": r[6] or "",
            "updated_at": r[7] or "",
            "text": r[8] or "",
            "translation": r[9] or "",
            "tags": r[10] or "",
            "local_file": r[11] or "",
            "raw_json": r[12] or "",
        }

    to_insert: list[dict] = []
    to_update: list[dict] = []
    to_relocate_month: list[dict] = []
    seen_in_json: set[tuple[str, str]] = set()

    total_json_files = 0
    total_json_records = 0

    for m_path in sorted(root.iterdir()):
        if not m_path.is_dir() or m_path.name.startswith("."):
            continue
        m_name = m_path.name
        m_dirname = archive.member_dir_name(m_name)
        for y_path in sorted(m_path.iterdir()):
            if not y_path.is_dir() or not y_path.name.isdigit():
                continue
            year = int(y_path.name)
            for mo_path in sorted(y_path.iterdir()):
                if not mo_path.is_dir() or not mo_path.name.isdigit():
                    continue
                month = int(mo_path.name)
                json_file = mo_path / "messages.json"
                if not json_file.is_file():
                    continue

                total_json_files += 1
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        msgs = json.load(f)
                    if not isinstance(msgs, list):
                        continue
                except Exception as exc:
                    log_all(f"⚠️ 读取归档 JSON 异常 ({json_file}): {exc}", is_error=True)
                    continue

                for msg in msgs:
                    if not isinstance(msg, dict):
                        continue
                    rid = str(msg.get("id") or "")
                    if not rid:
                        continue
                    total_json_records += 1
                    seen_in_json.add((m_dirname, rid))

                    raw_json_str = json.dumps(msg, ensure_ascii=False)
                    json_digest = _canonical_digest(msg)
                    local_f = str(msg.get("_local_file") or "")
                    txt = str(msg.get("text") or "")
                    trans = str(msg.get("_translation") or "")
                    tags = (str(msg.get("_tags") or "") + " " + str(msg.get("_custom_tags") or "")).strip()
                    pub_at = str(msg.get("published_at") or msg.get("updated_at") or "")
                    upd_at = str(msg.get("updated_at") or pub_at)
                    msg_type = str(msg.get("type") or "text")

                    item_payload = {
                        "id": rid,
                        "member_name": m_name,
                        "member_dir": m_dirname,
                        "year": year,
                        "month": month,
                        "type": msg_type,
                        "published_at": pub_at,
                        "updated_at": upd_at,
                        "text": txt,
                        "translation": trans,
                        "tags": tags,
                        "local_file": local_f,
                        "raw_json": raw_json_str,
                    }

                    db_entry = existing_index.get((m_dirname, rid))
                    if db_entry is None:
                        # 1. 缺失索引项（例如同 ID 被冲刷覆盖或新增）
                        to_insert.append(item_payload)
                    else:
                        # 检查年份月份漂移 (如 62951)
                        if db_entry["year"] != year or db_entry["month"] != month:
                            to_relocate_month.append({
                                **item_payload,
                                "old_year": db_entry["year"],
                                "old_month": db_entry["month"],
                            })
                        else:
                            # 检查内容/摘要是否一致
                            try:
                                db_raw_dict = json.loads(db_entry["raw_json"]) if db_entry["raw_json"] else {}
                                db_digest = _canonical_digest(db_raw_dict)
                            except Exception:
                                db_digest = ""
                            if db_digest != json_digest or db_entry["local_file"] != local_f:
                                to_update.append(item_payload)

    # 4. 检查 SQLite 中存在但 JSON 缺失的孤立记录（如 999999）
    isolated_in_db: list[dict] = []
    for (mdir, mid), entry in existing_index.items():
        if (mdir, mid) not in seen_in_json:
            isolated_in_db.append({
                "id": mid,
                "member_name": entry["member_name"],
                "member_dir": mdir,
                "year": entry["year"],
                "month": entry["month"],
                "text": entry["text"][:60],
            })

    return {
        "ok": True,
        "total_json_files": total_json_files,
        "total_json_records": total_json_records,
        "to_insert": to_insert,
        "to_update": to_update,
        "to_relocate_month": to_relocate_month,
        "isolated_in_db": isolated_in_db,
        "total_pending_fixes": len(to_insert) + len(to_update) + len(to_relocate_month),
    }


def execute_identity_migration(plan: dict, backup_dir: Path | None = None) -> dict:
    """Safely execute the migration plan with backup, global lock, and single transaction."""
    if not plan.get("ok"):
        return {"ok": False, "error": plan.get("error", "无效的迁移计划")}

    to_insert = plan.get("to_insert", [])
    to_update = plan.get("to_update", [])
    to_relocate_month = plan.get("to_relocate_month", [])
    isolated_in_db = plan.get("isolated_in_db", [])
    total_fixes = len(to_insert) + len(to_update) + len(to_relocate_month)

    conn = archive.init_db()
    if not conn:
        return {"ok": False, "error": "无法连接 SQLite 数据库"}

    # 1. 确定备份目录并执行快照备份
    archive_db_path = archive.get_db_path()
    if not archive_db_path.is_file():
        return {"ok": False, "error": f"找不到数据库文件: {archive_db_path}"}

    b_dir = backup_dir or (archive_db_path.parent / ".backups")
    b_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    backup_file = b_dir / f"archive_backup_pre_identity_migration_{ts}.db"

    log_all(f"📦 正在使用 SQLite Backup API 创建全量一致性快照备份: {backup_file}...")
    try:
        backup_conn = sqlite3.connect(str(backup_file))
        with backup_conn:
            conn.backup(backup_conn)
        backup_conn.close()
        log_all("✅ 快照备份创建完成")
    except Exception as exc:
        return {"ok": False, "error": f"创建快照备份失败: {exc}"}

    # 2. 持锁执行迁移事务
    lock_path = archive._messages_index_lock_path()
    try:
        with archive._locked_file(lock_path, timeout=120):
            with conn:
                # 批量插入新项
                if to_insert:
                    insert_rows = [
                        (
                            item["id"], item["member_name"], item["member_dir"],
                            item["year"], item["month"], item["type"],
                            item["published_at"], item["updated_at"],
                            item["text"], item["translation"], item["tags"],
                            item["local_file"], item["raw_json"]
                        )
                        for item in to_insert
                    ]
                    conn.executemany("""
                        INSERT INTO messages (
                            id, member_name, member_dir, year, month, type,
                            published_at, updated_at, text, translation, tags, local_file, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(member_dir, id) DO UPDATE SET
                            member_name=excluded.member_name,
                            year=excluded.year,
                            month=excluded.month,
                            type=excluded.type,
                            published_at=excluded.published_at,
                            updated_at=excluded.updated_at,
                            text=excluded.text,
                            translation=excluded.translation,
                            tags=excluded.tags,
                            local_file=excluded.local_file,
                            raw_json=excluded.raw_json;
                    """, insert_rows)

                # 批量更新不一致项
                if to_update:
                    update_rows = [
                        (
                            item["member_name"], item["year"], item["month"], item["type"],
                            item["published_at"], item["updated_at"],
                            item["text"], item["translation"], item["tags"],
                            item["local_file"], item["raw_json"],
                            item["member_dir"], item["id"]
                        )
                        for item in to_update
                    ]
                    conn.executemany("""
                        UPDATE messages SET
                            member_name = ?, year = ?, month = ?, type = ?,
                            published_at = ?, updated_at = ?,
                            text = ?, translation = ?, tags = ?,
                            local_file = ?, raw_json = ?
                        WHERE member_dir = ? AND id = ?;
                    """, update_rows)

                # 批量处理跨月位置纠正
                if to_relocate_month:
                    relocate_rows = [
                        (
                            item["year"], item["month"], item["member_name"], item["type"],
                            item["published_at"], item["updated_at"],
                            item["text"], item["translation"], item["tags"],
                            item["local_file"], item["raw_json"],
                            item["member_dir"], item["id"]
                        )
                        for item in to_relocate_month
                    ]
                    conn.executemany("""
                        UPDATE messages SET
                            year = ?, month = ?, member_name = ?, type = ?,
                            published_at = ?, updated_at = ?,
                            text = ?, translation = ?, tags = ?,
                            local_file = ?, raw_json = ?
                        WHERE member_dir = ? AND id = ?;
                    """, relocate_rows)

                # 同步更新 FTS
                all_changed = to_insert + to_update + to_relocate_month
                if archive._has_fts5 and all_changed:
                    fts_del_keys = [(item["member_dir"], item["id"]) for item in all_changed]
                    conn.executemany("DELETE FROM messages_fts WHERE member_dir = ? AND id = ?;", fts_del_keys)
                    fts_rows = [
                        (item["id"], item["member_dir"], item["member_name"], item["text"], item["translation"], item["tags"])
                        for item in all_changed
                    ]
                    conn.executemany("""
                        INSERT INTO messages_fts (id, member_dir, member_name, text, translation, tags)
                        VALUES (?, ?, ?, ?, ?, ?);
                    """, fts_rows)

                # 写入版本标识
                conn.execute(
                    "INSERT INTO archive_meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                    ("messages_index_version", str(archive._MESSAGE_INDEX_VERSION)),
                )
    except Exception as exc:
        log_all(f"⚠️ 执行迁移事务失败: {exc}", is_error=True)
        return {"ok": False, "error": f"迁移事务失败: {exc}", "backup_file": str(backup_file)}

    log_all(f"🎉 归档身份迁移成功应用: 插入 {len(to_insert)} 条, 更新 {len(to_update)} 条, 纠偏 {len(to_relocate_month)} 条, 保留孤立记录 {len(isolated_in_db)} 条")
    return {
        "ok": True,
        "backup_file": str(backup_file),
        "inserted_count": len(to_insert),
        "updated_count": len(to_update),
        "relocated_count": len(to_relocate_month),
        "isolated_count": len(isolated_in_db),
        "total_fixes": total_fixes,
        "status": "applied",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="归档消息身份与复合主键索引安全迁移工具")
    parser.add_argument("--dry-run", action="store_true", help="只读执行扫描与计划比对，不修改数据库")
    parser.add_argument("--apply", action="store_true", help="执行数据迁移（带快照备份与全局锁）")
    parser.add_argument("--backup-dir", type=str, default="", help="自定义备份目录路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("错误: 必须指定 --dry-run (只读扫描) 或 --apply (执行迁移)")
        parser.print_help()
        sys.exit(1)

    plan = plan_identity_migration()
    if not plan.get("ok"):
        print(f"扫描失败: {plan.get('error')}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        if args.json:
            # 缩减 payload 避免日志爆炸
            dry_summary = {
                "ok": True,
                "dry_run": True,
                "total_json_files": plan["total_json_files"],
                "total_json_records": plan["total_json_records"],
                "to_insert_count": len(plan["to_insert"]),
                "to_update_count": len(plan["to_update"]),
                "to_relocate_month_count": len(plan["to_relocate_month"]),
                "isolated_in_db_count": len(plan["isolated_in_db"]),
                "total_pending_fixes": plan["total_pending_fixes"],
                "isolated_in_db": plan["isolated_in_db"],
            }
            print(json.dumps(dry_summary, ensure_ascii=False, indent=2))
        else:
            print("=" * 60)
            print("🔍 归档消息身份迁移 —— 只读预演计划 (Dry-Run)")
            print("=" * 60)
            print(f"📂 扫描月度 JSON 文件: {plan['total_json_files']} 个")
            print(f"📄 权威 JSON 消息总数: {plan['total_json_records']} 条")
            print(f"➕ 需补齐索引项 (跨成员同编号/缺失): {len(plan['to_insert'])} 条")
            for item in plan["to_insert"][:10]:
                print(f"   - [{item['member_name']} ({item['member_dir']})] ID: {item['id']} ({item['year']}-{item['month']:02d})")
            if len(plan["to_insert"]) > 10:
                print(f"   ... 及另外 {len(plan['to_insert']) - 10} 条")

            print(f"🔄 需纠偏内容不一致项: {len(plan['to_update'])} 条")
            for item in plan["to_update"][:10]:
                print(f"   - [{item['member_name']} ({item['member_dir']})] ID: {item['id']}")
            if len(plan["to_update"]) > 10:
                print(f"   ... 及另外 {len(plan['to_update']) - 10} 条")

            print(f"📅 需修正月份归属项: {len(plan['to_relocate_month'])} 条")
            for item in plan["to_relocate_month"]:
                print(f"   - [{item['member_name']} ({item['member_dir']})] ID: {item['id']} 从 {item['old_year']}-{item['old_month']:02d} -> {item['year']}-{item['month']:02d}")

            print(f"🛡️ SQLite 孤立历史记录 (保留不删除): {len(plan['isolated_in_db'])} 条")
            for item in plan["isolated_in_db"]:
                print(f"   - [{item['member_name']} ({item['member_dir']})] ID: {item['id']} ({item['year']}-{item['month']:02d})")

            print("-" * 60)
            print(f"🎯 待修复项合计: {plan['total_pending_fixes']} 项")
            print("💡 本次执行为只读预演，未做任何修改。若确认执行，请增加 --apply 参数。")
            print("=" * 60)
        sys.exit(0)

    if args.apply:
        backup_dir = Path(args.backup_dir) if args.backup_dir else None
        res = execute_identity_migration(plan, backup_dir=backup_dir)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            if res.get("ok"):
                print("=" * 60)
                print("🎉 归档消息身份安全迁移完成！")
                print("=" * 60)
                print(f"📦 备份文件: {res.get('backup_file')}")
                print(f"➕ 成功补齐索引项: {res.get('inserted_count')} 条")
                print(f"🔄 成功纠偏内容: {res.get('updated_count')} 条")
                print(f"📅 成功纠正月份: {res.get('relocated_count')} 条")
                print(f"🛡️ 安全保留孤立记录: {res.get('isolated_count')} 条")
                print("=" * 60)
                sys.exit(0)
            else:
                print(f"❌ 迁移失败: {res.get('error')}", file=sys.stderr)
                sys.exit(1)


if __name__ == "__main__":
    main()
