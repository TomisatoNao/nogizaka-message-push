import asyncio
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config.config as cfg
from src import archive


@pytest.mark.asyncio
async def test_letter_archiving_and_queries(tmp_path):
    orig_dir = cfg.ARCHIVE_DIR
    archive._sqlite_conn = None
    cfg.ARCHIVE_DIR = str(tmp_path)

    try:
        # 1. 初始化数据库
        conn = archive.init_db()
        assert conn is not None

        # 2. 构造测试信件数据
        member_name = "冨里 奈央"
        m_dir = archive.member_dir_name(member_name)

        # 准备本地伪造信纸图片（避免单元测试触发真实外网下载）
        letter_img_dir = tmp_path / m_dir / "letters"
        letter_img_dir.mkdir(parents=True, exist_ok=True)
        fake_card1 = letter_img_dir / "20260425_152512_1404518.jpg"
        fake_card1.write_bytes(b"FAKE_LETTER_CARD_BYTES_12345")
        fake_card2 = letter_img_dir / "20251128_210424_1235103.jpg"
        fake_card2.write_bytes(b"FAKE_LETTER_CARD_BYTES_67890")

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
        archive.close_db()


@pytest.mark.asyncio
async def test_fetch_member_letters_pagination():
    import tools.archive_letters as al
    from unittest.mock import AsyncMock, MagicMock

    member = {"name": "冨里 奈央", "m_name": "冨里 奈央", "m_id": "55", "account_id": "test_acc"}
    al.ACCOUNT_CREDS["test_acc"] = {"access_token": "fake_token"}
    cfg.ACCOUNTS["test_acc"] = {"group_type": "nogizaka46"}

    # 模拟 API 分页返回 25 封信件（第一页 10 封，第二页 10 封，第三页 5 封）
    page1 = [{"id": 100 + i, "created_at": f"2026-05-{25-i:02d}T10:00:00Z"} for i in range(10)]
    page2 = [{"id": 200 + i, "created_at": f"2026-04-{25-i:02d}T10:00:00Z"} for i in range(10)]
    page3 = [{"id": 300 + i, "created_at": f"2026-03-{25-i:02d}T10:00:00Z"} for i in range(5)]

    client = AsyncMock()

    def mock_get(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "created_to=2026-04" in url or "offset=20" in url:
            resp.json.return_value = {"letters": page3}
        elif "created_to=2026-05" in url or "offset=10" in url:
            resp.json.return_value = {"letters": page2}
        elif "count=100" in url and "order=asc" not in url:
            resp.json.return_value = {"letters": page1}
        else:
            resp.json.return_value = {"letters": []}
        return resp

    client.get.side_effect = mock_get

    res = await al.fetch_member_letters(member, client)
    assert len(res) == 25
    assert res[0]["id"] == 100
    assert res[-1]["id"] == 304


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


def test_webui_letters_sync_api_routing(monkeypatch=None):
    from src.webui import _Handler
    from unittest.mock import MagicMock
    import tools.archive_letters as al
    import src.archive as arc

    async def mock_sync(target_mem, client):
        return (3, 1)

    orig_sync = getattr(al, "sync_letters_for_member", None)
    orig_init = getattr(arc, "initialize", None)

    if monkeypatch is not None:
        monkeypatch.setattr(al, "sync_letters_for_member", mock_sync)
        monkeypatch.setattr(arc, "initialize", lambda client: None)
    else:
        al.sync_letters_for_member = mock_sync
        arc.initialize = lambda client: None

    try:
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
    finally:
        if monkeypatch is None:
            if orig_sync:
                al.sync_letters_for_member = orig_sync
            if orig_init:
                arc.initialize = orig_init


def main():
    import tempfile
    from pathlib import Path
    print("=== Test 1: 信件归档与 SQLite 持久化 ===")
    with tempfile.TemporaryDirectory(prefix="letter_test_") as tdir:
        asyncio.run(test_letter_archiving_and_queries(Path(tdir)))
    print("✅ Test 1 通过")

    print("=== Test 2: WebUI letters 端点路由 ===")
    test_webui_letters_api_routing()
    print("✅ Test 2 通过")

    print("=== Test 3: WebUI letters_sync 端点路由 ===")
    test_webui_letters_sync_api_routing()
    print("✅ Test 3 通过")

    print("=== Test 4: 信件多页分页拉取测试 ===")
    asyncio.run(test_fetch_member_letters_pagination())
    print("✅ Test 4 通过")

    print("==================================================")
    print("🎉 全部信件归档测试通过！")


if __name__ == "__main__":
    main()
