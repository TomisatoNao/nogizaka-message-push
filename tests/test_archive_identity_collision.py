"""Tests for cross-member message ID collision and scoped identity isolation."""

import sqlite3
from pathlib import Path
import pytest

import src.config.config as cfg
from src import archive, auth


@pytest.fixture(autouse=True)
def cleanup_dbs():
    archive.close_db()
    auth._auth_conn = None
    yield
    archive.close_db()
    auth._auth_conn = None


def test_cross_member_same_id_coexists_in_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """验证当两位不同成员拥有相同消息编号（如 104441）时，SQLite 能够独立存储双方，互不覆盖冲刷。"""
    test_archive_dir = tmp_path / "archive"
    test_archive_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(test_archive_dir))

    conn = archive.init_db()
    assert conn is not None

    # 1. 模拟成员 A (冨里奈央, 2024-10) 的消息 104441
    msg_a = {
        "id": "104441",
        "type": "text",
        "text": "冨里奈央的消息内容",
        "published_at": "2024-10-15T12:00:00Z",
        "updated_at": "2024-10-15T12:00:00Z",
    }
    # 2. 模拟成员 B (マネダコ, 2026-03) 的同编号消息 104441
    msg_b = {
        "id": "104441",
        "type": "text",
        "text": "マネダコ的消息内容",
        "published_at": "2026-03-20T18:00:00Z",
        "updated_at": "2026-03-20T18:00:00Z",
    }

    ok_a = archive._save_msgs_to_sqlite("冨里奈央", 2024, 10, [msg_a])
    assert ok_a is True

    ok_b = archive._save_msgs_to_sqlite("マネダコ", 2026, 3, [msg_b])
    assert ok_b is True

    # 验证数据库中必须同时存在 2 条记录，且各自归属正确
    rows = conn.execute("SELECT id, member_name, member_dir, text FROM messages ORDER BY member_dir;").fetchall()
    assert len(rows) == 2, f"期望 2 条记录，但实际仅存 {len(rows)} 条（发生了同 ID 覆盖）"

    # 验证通过 member_dir 查询各自的消息
    res_a = archive.load_messages_by_ids("冨里奈央", ["104441"])
    assert len(res_a) == 1
    assert res_a[0]["text"] == "冨里奈央的消息内容"

    res_b = archive.load_messages_by_ids("マネダコ", ["104441"])
    assert len(res_b) == 1
    assert res_b[0]["text"] == "マネダコ的消息内容"


def test_user_interaction_scoped_to_member_for_same_msg_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """验证当两位成员拥有相同消息编号时，同一用户的收藏/点赞交互按成员隔离，互不串项。"""
    auth_db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth, "AUTH_DB_PATH", auth_db_path)

    conn = auth.get_auth_db()
    assert conn is not None

    # 用户 admin 分别收藏冨里奈央的 104441 和取消收藏マネダコ的 104441
    auth.set_user_message_interaction("admin", "104441", member_dir="tomisatonao", member_name="冨里奈央", action="favorite", value=True)
    auth.set_user_message_interaction("admin", "104441", member_dir="manedako", member_name="マネダコ", action="favorite", value=False)

    # 查冨里奈央：必须是 favorite=True
    favs_a = auth.get_user_favorite_message_ids("admin", member_dir="tomisatonao")
    assert "104441" in favs_a, "冨里奈央的收藏状态被マネダコ覆盖"

    # 查マネダコ：必须是 favorite=False
    favs_b = auth.get_user_favorite_message_ids("admin", member_dir="manedako")
    assert "104441" not in favs_b

    # 验证批量获取交互状态按 member_dir 隔离
    inter_a = auth.get_user_message_interactions("admin", ["104441"], member_dir="tomisatonao")
    assert inter_a.get("104441", {}).get("is_favorite") is True

    inter_b = auth.get_user_message_interactions("admin", ["104441"], member_dir="manedako")
    assert inter_b.get("104441", {}).get("is_favorite") is False


def test_archive_db_schema_self_heal_from_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """验证旧版 messages 表（单列 id 主键）在启动 init_db() 时能静默、无损升级为 (member_dir, id) 复合主键。"""
    test_archive_dir = tmp_path / "legacy_archive"
    test_archive_dir.mkdir(parents=True, exist_ok=True)
    db_file = test_archive_dir / "archive.db"

    # 手动建立旧版 messages 表
    raw_conn = sqlite3.connect(str(db_file))
    raw_conn.execute("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            member_name TEXT NOT NULL,
            member_dir TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            type TEXT,
            published_at TEXT,
            updated_at TEXT,
            text TEXT,
            translation TEXT,
            tags TEXT,
            local_file TEXT,
            raw_json TEXT NOT NULL
        );
    """)
    raw_conn.execute("""
        INSERT INTO messages VALUES (
            '104441', '冨里奈央', 'tomisatonao', 2024, 10, 'text',
            '2024-10-15T12:00:00Z', '2024-10-15T12:00:00Z',
            '旧版存量消息', '', '', '', '{"id": "104441", "text": "旧版存量消息"}'
        );
    """)
    raw_conn.commit()
    raw_conn.close()

    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(test_archive_dir))

    # 启动 init_db()，触发自愈迁移
    conn = archive.init_db()
    assert conn is not None

    # 检查表结构 PRAGMA table_info
    cols = conn.execute("PRAGMA table_info(messages);").fetchall()
    pk_cols = [c[1] for c in cols if c[5] > 0]
    assert "member_dir" in pk_cols and "id" in pk_cols, f"主键未升级为复合主键: {pk_cols}"

    # 验证旧存量数据完整保留
    row = conn.execute("SELECT member_name, text FROM messages WHERE member_dir='tomisatonao' AND id='104441';").fetchone()
    assert row is not None
    assert row[0] == "冨里奈央"
    assert row[1] == "旧版存量消息"

    # 验证现在可以成功插入同 ID 但不同 member_dir 的新记录
    msg_b = {
        "id": "104441",
        "type": "text",
        "text": "同ID新成员消息",
        "published_at": "2026-03-20T18:00:00Z",
        "updated_at": "2026-03-20T18:00:00Z",
    }
    assert archive._save_msgs_to_sqlite("マネダコ", 2026, 3, [msg_b]) is True

    # 验证两者共存
    all_rows = conn.execute("SELECT member_dir, text FROM messages WHERE id='104441' ORDER BY member_dir;").fetchall()
    assert len(all_rows) == 2


def test_auth_db_schema_self_heal_from_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """验证旧版 user_message_interactions 表在启动 get_auth_db() 时能静默自愈升级为复合主键。"""
    auth_db_path = tmp_path / "legacy_auth.db"

    # 手动建立旧版 user_message_interactions 表
    raw_conn = sqlite3.connect(str(auth_db_path))
    raw_conn.execute("""
        CREATE TABLE user_message_interactions (
            username TEXT NOT NULL,
            message_id TEXT NOT NULL,
            member_dir TEXT NOT NULL DEFAULT '',
            member_name TEXT NOT NULL DEFAULT '',
            is_favorite INTEGER NOT NULL DEFAULT 0,
            is_liked INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (username, message_id)
        );
    """)
    raw_conn.execute("""
        INSERT INTO user_message_interactions VALUES (
            'admin', '104441', 'tomisatonao', '冨里奈央', 1, 0, 1000.0, 1000.0
        );
    """)
    raw_conn.commit()
    raw_conn.close()

    monkeypatch.setattr(auth, "AUTH_DB_PATH", auth_db_path)

    conn = auth.get_auth_db()
    assert conn is not None

    cols = conn.execute("PRAGMA table_info(user_message_interactions);").fetchall()
    pk_cols = [c[1] for c in cols if c[5] > 0]
    assert "member_dir" in pk_cols and "message_id" in pk_cols and "username" in pk_cols

    # 旧数据必须完好
    row = conn.execute("SELECT member_dir, is_favorite FROM user_message_interactions WHERE username='admin' AND message_id='104441';").fetchone()
    assert row is not None
    assert row[0] == "tomisatonao"
    assert row[1] == 1

    # 插入同 ID 的另一成员交互，互不影响
    auth.set_user_message_interaction("admin", "104441", member_dir="manedako", member_name="マネダコ", action="like", value=True)
    rows = conn.execute("SELECT member_dir, is_favorite, is_liked FROM user_message_interactions WHERE username='admin' AND message_id='104441' ORDER BY member_dir;").fetchall()
    assert len(rows) == 2
