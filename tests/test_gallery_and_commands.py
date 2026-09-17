"""
tests/test_gallery_and_commands.py — 纯享相册画廊与 QQ 群抽美图指令单元测试
"""

import json
import sqlite3
import pytest

from src import archive as _archive
from src.platforms.napcat_commands import NapCatCommandHandler
from src.webui_modules.archive.gallery import handle_gallery


@pytest.fixture
def temp_archive_env(tmp_path, monkeypatch):
    """构建受控的测试归档目录与 SQLite archive.db。"""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True)
    monkeypatch.setattr(_archive, "archive_root", lambda: archive_dir)

    db_path = archive_dir / "archive.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
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

    # 创建测试成员文件与媒体图片
    m_dir = archive_dir / "冨里奈央" / "2024" / "09" / "picture"
    m_dir.mkdir(parents=True)
    pic1 = m_dir / "pic1.jpg"
    pic1.write_bytes(b"fake_jpeg_1")
    pic2 = m_dir / "pic2.jpg"
    pic2.write_bytes(b"fake_jpeg_2")

    # 插入 2 条图片记录
    conn.execute("""
        INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, text, local_file, raw_json)
        VALUES ('msg_1', '冨里奈央', '冨里奈央', 2024, 9, 'picture', '2024-09-18T10:00:00Z', '今天吃了红豆面包！', '2024/09/picture/pic1.jpg', '{"thumbnail_width": 1080, "thumbnail_height": 1440}');
    """)
    conn.execute("""
        INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, text, local_file, raw_json)
        VALUES ('msg_2', '冨里奈央', '冨里奈央', 2024, 9, 'picture', '2024-09-18T12:00:00Z', '彩排结束啦～', '2024/09/picture/pic2.jpg', '{"thumbnail_width": 720, "thumbnail_height": 1280}');
    """)
    conn.commit()

    monkeypatch.setattr("src.archive.init_db", lambda: conn)
    monkeypatch.setattr("src.archive_query._get_init_db", lambda: conn)
    monkeypatch.setattr("src.archive_query._get_archive_root", lambda: archive_dir)

    yield {
        "archive_dir": archive_dir,
        "conn": conn,
    }

    conn.close()


def test_get_gallery_photos_and_random_photo(temp_archive_env):
    """验证画廊查询与随机抽图逻辑。"""
    # 1. 验证 get_gallery_photos 分页与内容
    data = _archive.get_gallery_photos(member_dir="冨里奈央", page=1, per_page=10)
    assert data["ok"] is True
    assert data["total"] == 2
    assert len(data["photos"]) == 2
    assert data["photos"][0]["member_name"] == "冨里奈央"
    assert data["photos"][0]["url"].startswith("/api/archive/media/冨里奈央/")

    # 2. 验证 get_random_photo 随机抽取
    random_pic = _archive.get_random_photo(member_dir="冨里奈央")
    assert random_pic is not None
    assert random_pic["member_name"] == "冨里奈央"
    assert random_pic["abs_path"].is_file()
    assert random_pic["id"] in {"msg_1", "msg_2"}

    # 3. 验证未存在成员返回 None
    non_existent = _archive.get_random_photo(member_dir="不存在的成员")
    assert non_existent is None


def test_napcat_command_handler_matching_and_cooldown(temp_archive_env):
    """验证 NapCat 快捷指令识别与冷却机制。"""
    sent_messages = []

    async def mock_sender(gid: int, chain: list[dict]):
        sent_messages.append((gid, chain))
        return True

    handler = NapCatCommandHandler(
        cooldown_user_seconds=5.0,
        cooldown_group_seconds=2.0,
        sender=mock_sender,
    )

    # 1. 检查指令识别正则
    assert handler.is_command("/抽张美图") is True
    assert handler.is_command("/美图") is True
    assert handler.is_command("/美图 冨里奈央") is True
    assert handler.is_command("#抽张美图") is True
    assert handler.is_command("/pic") is True
    assert handler.is_command("你好呀") is False

    # 2. 触发一次抽图
    handled, status = handler.try_handle_command(
        group_id="123456",
        user_id="99999",
        raw_text="/抽张美图 冨里奈央",
    )
    assert handled is True
    assert status == "command_executed"
    assert len(sent_messages) == 1
    gid, chain = sent_messages[0]
    assert gid == 123456
    img_item = next(item for item in chain if item["type"] == "image")
    # 验证本地图片优先转为 base64:// 协议发送，杜绝 NapCat 跨容器/设备 ENOENT
    assert img_item["data"]["file"].startswith("base64://")

    # 3. 立即再次触发，应被冷却拦截
    handled_cd, status_cd = handler.try_handle_command(
        group_id="123456",
        user_id="99999",
        raw_text="/抽张美图",
    )
    assert handled_cd is True
    assert status_cd in {"rate_limited_group", "rate_limited_user"}


def test_gallery_webui_endpoint(temp_archive_env, monkeypatch):
    """验证 WebUI /api/archive/gallery HTTP 接口处理。"""
    monkeypatch.setattr("src.webui_modules.archive_handlers.get_blog_db", lambda: None)

    class MockHandler:
        path = "/api/archive/gallery?member=冨里奈央&page=1&per_page=10"
        headers = {}

        def __init__(self):
            self.response_code = None
            self.response_body = None

        def send_response(self, code):
            self.response_code = code

        def send_header(self, key, val):
            pass

        def end_headers(self):
            pass

        @property
        def wfile(self):
            class DummyWFile:
                def __init__(self, outer):
                    self.outer = outer

                def write(self, b):
                    self.outer.response_body = b
            return DummyWFile(self)

    h = MockHandler()
    matched = handle_gallery(h, "gallery", lambda **_: True, lambda: {})
    assert matched is True
    assert h.response_code == 200
    res = json.loads(h.response_body.decode("utf-8"))
    assert res["ok"] is True
    assert res["total"] == 2
    assert len(res["photos"]) == 2


def test_napcat_listener_handles_command(temp_archive_env, monkeypatch):
    """验证 NapCatInboundListener 接收到 /抽张美图 时优先分发给指令模块。"""
    from src.platforms.napcat_listener import NapCatInboundListener
    import config.config as cfg

    monkeypatch.setattr(cfg, "ENABLE_NAPCAT_QQ", True)
    monkeypatch.setattr(cfg, "NAPCAT_ROUTES", [{"group_id": 123456}])
    monkeypatch.setattr(cfg, "_config", {
        "enable_napcat_qq": True,
        "napcat_inbound": {
            "enabled": True,
        },
        "napcat_routes": [{"group_id": 123456}],
    })

    async def mock_send(*args, **kwargs):
        return True

    monkeypatch.setattr("src.platforms.napcat.send_qq_message", mock_send)

    listener = NapCatInboundListener()
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123456,
        "user_id": 99999,
        "self_id": 10000,
        "message": [{"type": "text", "data": {"text": "/抽张美图"}}],
        "raw_message": "/抽张美图",
    }

    status = listener.accept_event(event)
    assert status == "command_executed"


def test_blog_gallery_author_normalization(tmp_path, monkeypatch):
    """验证博客配图查询支持日文全角/半角空格姓名的归一化匹配。"""
    from src.webui_modules.archive.gallery import _get_blog_gallery

    db_path = tmp_path / "blogs.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY,
            group_key TEXT,
            author TEXT,
            title TEXT,
            date TEXT,
            image_paths_json TEXT
        );
    """)
    # 存入带空格的作者名，如 "冨里 奈央"
    conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, image_paths_json)
        VALUES (1, 'nogizaka', '冨里 奈央', '晴れの日', '2024-09-10 12:00:00', '["blogs/nogizaka/1/img1.jpg", "blogs/nogizaka/1/img2.jpg"]');
    """)
    conn.commit()

    monkeypatch.setattr("src.webui_modules.archive_handlers.get_blog_db", lambda: conn)

    # 1. 传入无空格的姓名 "冨里奈央"
    res1 = _get_blog_gallery(member="冨里奈央", page=1, per_page=10)
    assert res1["ok"] is True
    assert res1["total"] == 2
    assert len(res1["photos"]) == 2
    assert res1["photos"][0]["member_name"] == "冨里 奈央"

    # 2. 传入带半角空格的姓名 "冨里 奈央"
    res2 = _get_blog_gallery(member="冨里 奈央", page=1, per_page=10)
    assert res2["ok"] is True
    assert res2["total"] == 2

    # 3. 传入带全角空格的姓名 "冨里　奈央"
    res3 = _get_blog_gallery(member="冨里　奈央", page=1, per_page=10)
    assert res3["ok"] is True
    assert res3["total"] == 2

    conn.close()


def test_get_gallery_photos_hole_filling(temp_archive_env):
    """验证当存在部分本地图片缺失（文件空洞）时，get_gallery_photos 会自动向后扫描补齐每页数量。"""
    conn = temp_archive_env["conn"]
    archive_dir = temp_archive_env["archive_dir"]
    m_dir = archive_dir / "冨里奈央" / "2024" / "09" / "picture"

    # 插入一条本地文件不存在的记录（空洞）
    conn.execute("""
        INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, text, local_file, raw_json)
        VALUES ('msg_missing', '冨里奈央', '冨里奈央', 2024, 9, 'picture', '2024-09-18T11:00:00Z', '缺失文件', '2024/09/picture/non_existent.jpg', '{}');
    """)
    # 插入第三条真实存在的记录
    pic3 = m_dir / "pic3.jpg"
    pic3.write_bytes(b"fake_jpeg_3")
    conn.execute("""
        INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, text, local_file, raw_json)
        VALUES ('msg_3', '冨里奈央', '冨里奈央', 2024, 9, 'picture', '2024-09-18T09:00:00Z', '第三张真实图片', '2024/09/picture/pic3.jpg', '{}');
    """)
    conn.commit()

    # per_page=2 时，虽然 msg_missing 排在中间被跳过，但应自动扫描到 msg_3 补齐为 2 张真实图片
    data = _archive.get_gallery_photos(member_dir="冨里奈央", page=1, per_page=2)
    assert data["ok"] is True
    assert len(data["photos"]) == 2
    assert all(p["id"] != "msg_missing" for p in data["photos"])


def test_gallery_tab_selection_and_wording_contract():
    """验证相册画廊与博客/消息模式的 Tab 互斥、预路由隔离、文案与图标契约。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    html = (root / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    css = (root / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    # 1. HTML 中芯片图标与文案必须与顶部导航完全一致化
    assert "💬 消息配图" in html
    assert "📄 博客配图" in html
    assert "💌 消息生写" not in html
    assert "加载更多图片 ↓" in html
    assert "加载更多美图 ↓" not in html

    # 2. CSS 预路由隔离必须包含 html.view-gallery
    assert "html.view-gallery #tabHome" in css
    assert "html.view-gallery #tabGallery" in css
    assert "html.view-gallery #galleryGrid" in css

    # 3. JS 中 setHtmlViewClass 必须全面清除 view-gallery，杜绝残存导致多 Tab 选中
    assert 'root.classList.remove("view-home", "view-msg", "view-blog", "view-letter", "view-gallery");' in js
    assert "syncNavTabs(" in js

    # 4. JS 中统计文案与空状态使用“图片”
    assert '" 张图片"' in js
    assert '" 张美图"' not in js
    assert '暂无匹配的图片' in js
    assert '正在加载相册图片...' in js
def test_gallery_members_merging_and_blog_only(temp_archive_env, monkeypatch):
    """验证相册名册合并了消息照片与仅博客配图的成员，且统计单位为'张'。"""
    from src.webui_modules.archive.gallery import get_gallery_members, handle_gallery
    import src.webui_modules.archive.gallery as gm

    # 构造 mock blog db
    blog_conn = sqlite3.connect(":memory:")
    blog_conn.execute("""
        CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY,
            group_key TEXT,
            author TEXT,
            title TEXT,
            date TEXT,
            image_paths_json TEXT
        );
    """)
    # 插入仅博客成员远藤樱
    blog_conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, image_paths_json)
        VALUES (1, 'nogizaka', '遠藤 さくら', '秋風', '2024-09-15 10:00:00', '["blogs/1/01.jpg", "blogs/1/02.jpg"]');
    """)
    # 插入既有消息也有博客的冨里奈央
    blog_conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, image_paths_json)
        VALUES (2, 'nogizaka', '冨里 奈央', '晴れの日', '2024-09-16 12:00:00', '["blogs/2/01.jpg"]');
    """)
    blog_conn.commit()

    monkeypatch.setattr("src.webui_modules.archive_handlers.get_blog_db", lambda: blog_conn)
    gm._gallery_members_cache = None

    res = get_gallery_members()
    assert res["ok"] is True
    members = res["members"]

    # 1. 验证既有消息也有博客的成员
    tomisato = next((m for m in members if "冨里" in m["display"]), None)
    assert tomisato is not None
    assert tomisato["msg_photos"] == 2
    assert tomisato["blog_photos"] == 1
    assert tomisato["total_photos"] == 3

    # 2. 验证仅有博客的成员（远藤樱）被成功纳入画廊名册
    endo = next((m for m in members if "遠藤" in m["display"] or "远藤" in m["display"]), None)
    assert endo is not None
    assert endo["msg_photos"] == 0
    assert endo["blog_photos"] == 2
    assert endo["total_photos"] == 2
    assert endo["group"] == "nogizaka"

    # 3. 验证 HTTP 路由命中
    class MockHandler:
        def __init__(self):
            self.path = "/api/archive/gallery_members"
            self.payload = None

        def _send_json(self, payload, code=200):
            self.payload = payload

    h = MockHandler()
    handled = handle_gallery(h, "gallery_members", lambda need_admin: True, lambda: {})
    assert handled is True
    assert h.payload["ok"] is True
    assert len(h.payload["members"]) >= 2
