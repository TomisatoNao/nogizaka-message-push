"""Unit tests for archive identity migration tool (tools/migrate_archive_identity.py)."""

import json
from pathlib import Path
import pytest

import src.config.config as cfg
from src import archive
from tools import migrate_archive_identity


@pytest.fixture(autouse=True)
def cleanup_dbs():
    archive.close_db()
    yield
    archive.close_db()


def test_migration_dry_run_and_apply_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    test_archive_dir = tmp_path / "archive"
    test_archive_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(test_archive_dir))

    # 1. 创建权威磁盘归档 JSON:
    # 成员 A (冨里奈央): 2024-10 (104441, 102577) + 2023-10 (62951)
    # 成员 B (マネダコ): 2026-03 (104441 - 同编号冲突)
    # 成员 C (松田好花): 2026-01 (102577 - 同编号但内容不同)

    nao_dir = test_archive_dir / "冨里奈央" / "2024" / "10"
    nao_dir.mkdir(parents=True, exist_ok=True)
    nao_msgs_10 = [
        {"id": "104441", "type": "text", "text": "奈央的消息 104441", "published_at": "2024-10-15T12:00:00Z"},
        {"id": "102577", "type": "text", "text": "奈央的消息 102577 真实内容", "published_at": "2024-10-20T12:00:00Z"},
    ]
    (nao_dir / "messages.json").write_text(json.dumps(nao_msgs_10, ensure_ascii=False), encoding="utf-8")

    nao_dir_old = test_archive_dir / "冨里奈央" / "2023" / "10"
    nao_dir_old.mkdir(parents=True, exist_ok=True)
    nao_msgs_old = [
        {"id": "62951", "type": "text", "text": "奈央跨月消息 62951", "published_at": "2023-10-01T00:05:00Z"}
    ]
    (nao_dir_old / "messages.json").write_text(json.dumps(nao_msgs_old, ensure_ascii=False), encoding="utf-8")

    manedako_dir = test_archive_dir / "マネダコ" / "2026" / "03"
    manedako_dir.mkdir(parents=True, exist_ok=True)
    manedako_msgs = [
        {"id": "104441", "type": "text", "text": "マネダコ的消息 104441", "published_at": "2026-03-20T18:00:00Z"}
    ]
    (manedako_dir / "messages.json").write_text(json.dumps(manedako_msgs, ensure_ascii=False), encoding="utf-8")

    konoka_dir = test_archive_dir / "松田好花" / "2026" / "01"
    konoka_dir.mkdir(parents=True, exist_ok=True)
    konoka_msgs = [
        {"id": "102577", "type": "text", "text": "松田好花的消息 102577", "published_at": "2026-01-10T10:00:00Z"}
    ]
    (konoka_dir / "messages.json").write_text(json.dumps(konoka_msgs, ensure_ascii=False), encoding="utf-8")

    # 2. 模拟旧版数据库存在 4 类异常：
    # - 奈央 104441 在 DB 中（但マネダコ 104441 缺失）
    # - 奈央 102577 在 DB 中，但 raw_json 被松田好花的内容混用覆盖
    # - 奈央 62951 在 DB 中，但 year/month 记录为 2023-09
    # - 奈央 999999 在 DB 中，但 JSON 已无对应项
    conn = archive.init_db()
    assert conn is not None

    nao_mdir = archive.member_dir_name("冨里奈央")
    manedako_mdir = archive.member_dir_name("マネダコ")
    konoka_mdir = archive.member_dir_name("松田好花")

    with conn:
        conn.execute("""
            INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, updated_at, text, translation, tags, local_file, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            "104441", "冨里奈央", nao_mdir, 2024, 10, "text",
            "2024-10-15T12:00:00Z", "2024-10-15T12:00:00Z",
            "奈央的消息 104441", "", "", "", json.dumps(nao_msgs_10[0], ensure_ascii=False)
        ))
        conn.execute("""
            INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, updated_at, text, translation, tags, local_file, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            "102577", "冨里奈央", nao_mdir, 2024, 10, "text",
            "2026-01-10T10:00:00Z", "2026-01-10T10:00:00Z",
            "松田好花的消息 102577", "", "", "", json.dumps(konoka_msgs[0], ensure_ascii=False)  # 混用了松田的内容
        ))
        conn.execute("""
            INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, updated_at, text, translation, tags, local_file, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            "62951", "冨里奈央", nao_mdir, 2023, 9, "text",  # 错误的 9 月
            "2023-10-01T00:05:00Z", "2023-10-01T00:05:00Z",
            "奈央跨月消息 62951", "", "", "", json.dumps(nao_msgs_old[0], ensure_ascii=False)
        ))
        conn.execute("""
            INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, updated_at, text, translation, tags, local_file, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            "999999", "冨里奈央", nao_mdir, 2026, 8, "text",
            "2026-08-01T12:00:00Z", "2026-08-01T12:00:00Z",
            "孤立消息 999999", "", "", "", '{"id": "999999", "text": "孤立消息"}'
        ))

    # 3. 执行 Plan (Dry-Run 阶段)
    plan = migrate_archive_identity.plan_identity_migration(test_archive_dir)
    assert plan["ok"] is True
    assert plan["total_json_files"] == 4
    assert plan["total_json_records"] == 5

    # 验证分类：
    # to_insert 应包含：マネダコ 104441 与 松田好花 102577
    insert_keys = {(item["member_dir"], item["id"]) for item in plan["to_insert"]}
    assert (manedako_mdir, "104441") in insert_keys
    assert (konoka_mdir, "102577") in insert_keys

    # to_update 应包含：冨里奈央 102577 (内容混用被识别)
    update_keys = {(item["member_dir"], item["id"]) for item in plan["to_update"]}
    assert (nao_mdir, "102577") in update_keys

    # to_relocate_month 应包含：冨里奈央 62951 (从 2023-09 识别需纠正到 2023-10)
    relocate_keys = {(item["member_dir"], item["id"]) for item in plan["to_relocate_month"]}
    assert (nao_mdir, "62951") in relocate_keys

    # isolated_in_db 应包含：冨里奈央 999999
    isolated_keys = {(item["member_dir"], item["id"]) for item in plan["isolated_in_db"]}
    assert (nao_mdir, "999999") in isolated_keys

    # 验证此时数据库没有任何变动 (Dry-run 无副作用)
    cur = conn.cursor()
    cur.execute("SELECT month FROM messages WHERE member_dir=? AND id='62951';", (nao_mdir,))
    assert cur.fetchone()[0] == 9

    # 4. 执行 Apply 阶段
    backup_dir = tmp_path / "custom_backups"
    res = migrate_archive_identity.execute_identity_migration(plan, backup_dir=backup_dir)
    assert res["ok"] is True
    assert res["status"] == "applied"

    # 验证备份快照文件存在
    backup_file = Path(res["backup_file"])
    assert backup_file.is_file()
    assert backup_file.stat().st_size > 0

    # 5. 验证数据库修复后的状态
    # 5.1 マネダコ 104441 与 冨里奈央 104441 独立共存
    rows_104441 = conn.execute("SELECT member_dir, text FROM messages WHERE id='104441' ORDER BY member_dir;").fetchall()
    assert len(rows_104441) == 2
    assert (manedako_mdir, "マネダコ的消息 104441") in rows_104441
    assert (nao_mdir, "奈央的消息 104441") in rows_104441

    # 5.2 冨里奈央 102577 内容被正确还原
    row_102577_nao = conn.execute("SELECT text FROM messages WHERE member_dir=? AND id='102577';", (nao_mdir,)).fetchone()
    assert row_102577_nao[0] == "奈央的消息 102577 真实内容"

    # 松田好花 102577 也已入库
    row_102577_kono = conn.execute("SELECT text FROM messages WHERE member_dir=? AND id='102577';", (konoka_mdir,)).fetchone()
    assert row_102577_kono[0] == "松田好花的消息 102577"

    # 5.3 冨里奈央 62951 月份已被纠正到 10
    row_62951 = conn.execute("SELECT year, month FROM messages WHERE member_dir=? AND id='62951';", (nao_mdir,)).fetchone()
    assert row_62951 == (2023, 10)

    # 5.4 孤立消息 999999 安全保留未被删除
    row_999999 = conn.execute("SELECT text FROM messages WHERE member_dir=? AND id='999999';", (nao_mdir,)).fetchone()
    assert row_999999[0] == "孤立消息 999999"

    # 6. 验证幂等性：再次执行 plan，待修复项应为 0
    re_plan = migrate_archive_identity.plan_identity_migration(test_archive_dir)
    assert re_plan["total_pending_fixes"] == 0
    assert len(re_plan["to_insert"]) == 0
    assert len(re_plan["to_update"]) == 0
    assert len(re_plan["to_relocate_month"]) == 0
    # 孤立项仍被安全识别保留
    assert len(re_plan["isolated_in_db"]) == 1


def test_migration_tool_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    test_archive_dir = tmp_path / "archive_cli"
    test_archive_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(test_archive_dir))

    # 空归档下运行 dry-run json
    monkeypatch.setattr("sys.argv", ["migrate_archive_identity.py", "--dry-run", "--json"])
    with pytest.raises(SystemExit) as exc:
        migrate_archive_identity.main()
    assert exc.value.code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["ok"] is True
    assert data["dry_run"] is True
    assert data["total_pending_fixes"] == 0
