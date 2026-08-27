import json
import pytest

import config.config as cfg
from src import archive


@pytest.mark.asyncio
async def test_letter_archiving_and_queries(tmp_path):
    orig_dir = cfg.ARCHIVE_DIR
    orig_db = archive._sqlite_conn
    archive._sqlite_conn = None
    cfg.ARCHIVE_DIR = str(tmp_path)

    try:
        # 1. 初始化数据库
        conn = archive.init_db()
        assert conn is not None

        # 2. 构造测试信件数据
        member_name = "冨里 奈央"
        m_dir = archive.member_dir_name(member_name)

        # 准备本地伪造信纸图片
        letter_img_dir = tmp_path / m_dir / "letters"
        letter_img_dir.mkdir(parents=True, exist_ok=True)
        fake_card = letter_img_dir / "20260425_152512_1404518.jpg"
        fake_card.write_bytes(b"FAKE_LETTER_CARD_BYTES_12345")

        test_letters = [
            {
                "id": 1404518,
                "group_id": 55,
                "member_id": 55,
                "text": "奈央ちゃんへ、今日のミーグリ本当にお疲れ様でした！",
                "file": "https://djznowbmqickg.cloudfront.net/private/letters/files/1404518-20260425-062511.jpg",
                "thumbnail": "https://djznowbmqickg.cloudfront.net/private/letters/thumbnails/1404518-20260425-062511.jpg",
                "thumbnail_width": 341,
                "thumbnail_height": 512,
                "is_favorite": True,
                "created_at": "2026-04-25T06:25:12Z",
                "updated_at": "2026-04-26T04:51:13Z",
            },
            {
                "id": 1235103,
                "group_id": 55,
                "member_id": 55,
                "text": "好き好き大好き！",
                "file": "https://djznowbmqickg.cloudfront.net/private/letters/files/1235103-20251128-120423.jpg",
                "thumbnail": "https://djznowbmqickg.cloudfront.net/private/letters/thumbnails/1235103-20251128-120423.jpg",
                "thumbnail_width": 340,
                "thumbnail_height": 512,
                "is_favorite": False,
                "created_at": "2025-11-28T12:04:24Z",
                "updated_at": "2025-11-29T08:41:09Z",
            }
        ]

        # 3. 批量归档
        archived = await archive.archive_letters_batch(member_name, test_letters)
        assert len(archived) == 2
        assert archived[0]["id"] == 1404518
        assert "local_file" in archived[0]

        # 4. 从 SQLite 查询
        letters_db = archive.get_archive_letters(m_dir)
        assert len(letters_db) == 2
        assert letters_db[0]["id"] == 1404518
        assert letters_db[0]["is_favorite"] is True
        assert "今日のミーグリ" in letters_db[0]["text"]

        # 5. 计数测试
        count = archive.get_letters_count(m_dir)
        assert count == 2

        # 6. letters.json 磁盘文件校验
        json_file = letter_img_dir / "letters.json"
        assert json_file.exists()
        disk_data = json.loads(json_file.read_text(encoding="utf-8"))
        assert len(disk_data) == 2

    finally:
        cfg.ARCHIVE_DIR = orig_dir
        if archive._sqlite_conn:
            archive._sqlite_conn.close()
        archive._sqlite_conn = orig_db


def test_webui_letters_api_routing():
    from src.webui import _Handler
    from unittest.mock import MagicMock

    handler = _Handler.__new__(_Handler)
    handler.headers = {"Content-Length": "25"}
    handler.rfile = MagicMock()
    handler.rfile.read.return_value = json.dumps({"member": "冨里 奈央"}).encode("utf-8")
    handler.path = "/api/archive/letters?member=%E5%86%A8%E9%87%8C%E5%A5%88%E5%A4%AE"
    handler.command = "GET"
    handler._send_json = MagicMock()
    handler._guard = MagicMock(return_value=True)

    handler._handle_archive("letters")
    assert handler._send_json.called
    data = handler._send_json.call_args[0][0]
    assert data.get("ok") is True


def test_webui_letters_sync_api_routing(monkeypatch):
    from src.webui import _Handler
    from unittest.mock import MagicMock
    import tools.archive_letters as al

    async def mock_sync(target_mem, client):
        return (3, 1)

    monkeypatch.setattr(al, "sync_letters_for_member", mock_sync)

    handler = _Handler.__new__(_Handler)
    handler.headers = {"Content-Length": "25"}
    handler.rfile = MagicMock()
    handler.rfile.read.return_value = json.dumps({"member": "冨里 奈央"}).encode("utf-8")
    handler.path = "/api/archive/letters_sync"
    handler.command = "POST"
    handler._send_json = MagicMock()
    handler._guard = MagicMock(return_value=True)
    handler._read_body_json = MagicMock(return_value={"member": "冨里 奈央"})

    handler._handle_archive("letters_sync")
    assert handler._send_json.called
    data = handler._send_json.call_args[0][0]
    assert data.get("ok") is True
    assert data.get("total") == 3
    assert data.get("new") == 1
