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


def test_blog_gallery_remote_images_fallback(temp_archive_env, monkeypatch):
    """验证博客配图仅存在远程 URL（未下载本地路径）时，仍能完整统计并正确渲染画廊卡片。"""
    from src.webui_modules.archive.gallery import _get_blog_gallery, get_gallery_members, handle_gallery
    import src.webui_modules.archive.gallery as gm

    blog_conn = sqlite3.connect(":memory:")
    blog_conn.execute("""
        CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY,
            group_key TEXT,
            author TEXT,
            title TEXT,
            date TEXT,
            images_json TEXT,
            image_paths_json TEXT
        );
    """)
    # 模拟远藤樱仅有远程 URL（如 5 张图），本地 image_paths_json 为空列表
    blog_conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, images_json, image_paths_json)
        VALUES (
            101, 'nogizaka', '遠藤 さくら', '乃木坂三昧', '2026-09-01 20:28:00',
            '["https://www.nogizaka46.com/files/img1.jpg", "https://www.nogizaka46.com/files/img2.jpg", "https://www.nogizaka46.com/files/img3.jpg"]',
            '[]'
        );
    """)
    # 第二篇博客，2 张图，image_paths_json 为 None
    blog_conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, images_json, image_paths_json)
        VALUES (
            102, 'nogizaka', '遠藤 さくら', 'ナイショ', '2026-08-01 19:58:00',
            '["https://www.nogizaka46.com/files/img4.jpg", "https://www.nogizaka46.com/files/img5.jpg"]',
            NULL
        );
    """)
    blog_conn.commit()

    monkeypatch.setattr("src.webui_modules.archive_handlers.get_blog_db", lambda: blog_conn)
    gm._gallery_members_cache = None
    gm._blog_count_cache.clear()

    # 1. 验证名册统计总数正确纳入所有 5 张图片（而不是 0 或仅有本地的）
    mem_res = get_gallery_members()
    assert mem_res["ok"] is True
    endo = next((m for m in mem_res["members"] if "遠藤" in m["display"]), None)
    assert endo is not None
    assert endo["blog_photos"] == 5
    assert endo["total_photos"] == 5

    # 2. 验证 _get_blog_gallery 分页查询正确返回 5 张图片及远程 URL
    gal_res = _get_blog_gallery(member="遠藤 さくら", page=1, per_page=10)
    assert gal_res["ok"] is True
    assert gal_res["total"] == 5
    assert len(gal_res["photos"]) == 5
    assert gal_res["photos"][0]["url"].startswith("https://www.nogizaka46.com/")
    assert gal_res["photos"][0]["member_name"] == "遠藤 さくら"

    # 3. 验证 handle_gallery HTTP 端点
    class MockHandler:
        def __init__(self, path):
            self.path = path
            self.payload = None

        def _send_json(self, payload, code=200):
            self.payload = payload

    h = MockHandler("/api/archive/gallery?member=遠藤さくら&source=blog")
    handled = handle_gallery(h, "gallery", lambda need_admin: True, lambda: {})
    assert handled is True
    assert h.payload["ok"] is True
    assert h.payload["total"] == 5
    assert len(h.payload["photos"]) == 5


def test_napcat_command_mukai_resolution(temp_archive_env, monkeypatch):
    """验证输入 /美图 向井 时精准匹配向井纯叶，且绝不误回退至群默认成员（冨里奈央）。"""
    import config.config as cfg
    from src.platforms.napcat_commands import NapCatCommandHandler

    # 模拟群 533072575 默认推送冨里奈央
    monkeypatch.setattr(cfg, "NAPCAT_ROUTES", [
        {"group_id": "533072575", "member_filter": ["冨里奈央"], "remark": "冨里群"}
    ])

    # 模拟 blog_posts 中有向井纯叶的数据
    blog_conn = sqlite3.connect(":memory:")
    blog_conn.execute("""
        CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY,
            group_key TEXT,
            author TEXT,
            title TEXT,
            date TEXT,
            image_paths_json TEXT,
            images_json TEXT
        );
    """)
    blog_conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, image_paths_json, images_json)
        VALUES (36, 'sakurazaka', '向井 純葉', '連れ出して', '2026-08-07 21:24', '["sakurazaka/01.jpg"]', '[]');
    """)
    blog_conn.commit()
    monkeypatch.setattr("src.webui_modules.archive_handlers.get_blog_db", lambda: blog_conn)

    handler = NapCatCommandHandler()

    # 1. 验证无参数时，正确使用群默认小偶像（冨里奈央）
    m_default, is_unmatched = handler._resolve_target_member("533072575", "")
    assert is_unmatched is False
    assert m_default == "冨里奈央"

    # 2. 验证输入 "向井" 时，命中向井纯叶，绝不误回退至冨里奈央
    m_mukai, is_unmatched = handler._resolve_target_member("533072575", "向井")
    assert is_unmatched is False
    assert "向井" in m_mukai and ("純葉" in m_mukai or "纯叶" in m_mukai)

    # 3. 验证输入简体 "向井纯叶" 时，命中向井纯叶
    m_itoha, is_unmatched = handler._resolve_target_member("533072575", "向井纯叶")
    assert is_unmatched is False
    assert "向井" in m_itoha and ("純葉" in m_itoha or "纯叶" in m_itoha)

    # 4. 验证输入未知参数时，明确返回 is_unmatched=True，禁止回退群默认
    m_unknown, is_unmatched = handler._resolve_target_member("533072575", "不存在的小偶像XYZ")
    assert is_unmatched is True
    assert m_unknown is None


def test_gallery_years_and_sort_order(temp_archive_env, monkeypatch):
    """验证相册年份分布聚合统计与时间正序/倒序切换能力。"""
    from src.webui_modules.archive.gallery import (
        _get_blog_gallery,
        get_gallery_years,
        handle_gallery,
    )
    import src.webui_modules.archive.gallery as gm

    # 1. 验证消息年份统计
    msg_years = _archive.get_gallery_message_years("冨里奈央")
    assert 2024 in msg_years
    assert msg_years[2024] == 2

    # 2. 构造多篇包含不同年份的博客
    blog_conn = sqlite3.connect(":memory:")
    blog_conn.execute("""
        CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY,
            group_key TEXT,
            author TEXT,
            title TEXT,
            date TEXT,
            image_paths_json TEXT,
            images_json TEXT
        );
    """)
    blog_conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, image_paths_json, images_json)
        VALUES
            (1, 'nogizaka', '冨里 奈央', '2023博客', '2023-05-01 10:00:00', '["blogs/2023.jpg"]', '[]'),
            (2, 'nogizaka', '冨里 奈央', '2025博客', '2025-06-01 12:00:00', '["blogs/2025.jpg"]', '[]'),
            (3, 'sakurazaka', '向井 純葉', '2024博客', '2024-07-01 14:00:00', '["blogs/2024.jpg"]', '[]');
    """)
    blog_conn.commit()
    monkeypatch.setattr("src.webui_modules.archive_handlers.get_blog_db", lambda: blog_conn)
    gm._gallery_years_cache.clear()
    gm._blog_count_cache.clear()

    # 3. 聚合年份测试 (冨里奈央: 消息 2024:2, 博客 2023:1, 2025:1 => 总计 2025, 2024, 2023)
    years_all = get_gallery_years(member="冨里奈央", source="all")
    assert years_all["ok"] is True
    assert years_all["total"] == 4
    y_map = {item["year"]: item["count"] for item in years_all["years"]}
    assert y_map[2025] == 1
    assert y_map[2024] == 2
    assert y_map[2023] == 1

    # 只查博客源
    years_blog = get_gallery_years(member="冨里奈央", source="blog")
    y_blog_map = {item["year"]: item["count"] for item in years_blog["years"]}
    assert 2024 not in y_blog_map
    assert y_blog_map[2025] == 1
    assert y_blog_map[2023] == 1

    # 4. 验证时间正序 (order=asc) 与倒序 (order=desc)
    res_desc = _get_blog_gallery(member="冨里奈央", order="desc")
    assert res_desc["photos"][0]["published_at"].startswith("2025")
    assert res_desc["photos"][1]["published_at"].startswith("2023")

    res_asc = _get_blog_gallery(member="冨里奈央", order="asc")
    assert res_asc["photos"][0]["published_at"].startswith("2023")
    assert res_asc["photos"][1]["published_at"].startswith("2025")

    # 5. 验证 handle_gallery HTTP 路由
    class MockHandler:
        def __init__(self, path):
            self.path = path
            self.payload = None

        def _send_json(self, payload, code=200):
            self.payload = payload

    # 测试 sub == "gallery_years"
    h_years = MockHandler("/api/archive/gallery_years?member=冨里奈央&source=all")
    handled = handle_gallery(h_years, "gallery_years", lambda need_admin: True, lambda: {})
    assert handled is True
    assert h_years.payload["ok"] is True
    assert len(h_years.payload["years"]) == 3

    # 测试 sub == "gallery" 带 order=asc 参数
    h_gal = MockHandler("/api/archive/gallery?member=冨里奈央&source=all&order=asc")
    handled_gal = handle_gallery(h_gal, "gallery", lambda need_admin: True, lambda: {})
    assert handled_gal is True
    assert h_gal.payload["ok"] is True
    # 最早的应该是 2023 年博客图片
    assert h_gal.payload["photos"][0]["published_at"].startswith("2023")


def test_gallery_year_chips_and_sort_btn_contract():
    """验证前端模板与样式包含年份胶囊筛选和正/倒序按钮规范。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    html = (root / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    css = (root / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    # 1. archive.html
    assert 'id="galleryYearChips"' in html
    assert 'id="btnGallerySortOrder"' in html
    assert 'id="gallerySortOrderText"' in html

    # 2. archive.css
    assert '.gallery-year-chips' in css
    assert '.gallery-sort-btn' in css
    assert '.gallery-sort-btn.order-asc' in css

    # 3. archive.js
    assert 'curGalleryYear' in js
    assert 'curGalleryOrder' in js
    assert 'loadGalleryYears' in js
    assert 'renderGalleryYearChips' in js
    assert 'syncGallerySortButton' in js
    assert 'btnGallerySortOrder' in js


def test_heal_corrupt_blog_images(tmp_path, monkeypatch):
    """验证 heal_corrupt_blog_images 能够自动清理磁盘损坏文件并修复 SQLite 元数据。"""
    from src.blog_fetcher import heal_corrupt_blog_images
    import src.blog_fetcher as bf

    img_dir = tmp_path / "blog_images"
    img_dir.mkdir(parents=True)
    monkeypatch.setattr(bf, "BLOG_IMAGE_DIR", img_dir)

    # 1. 创建 0 字节文件、404 HTML 伪图片、有效图片
    sub_dir = img_dir / "nogizaka" / "staff" / "post_1"
    sub_dir.mkdir(parents=True)
    zero_file = sub_dir / "zero.jpg"
    zero_file.write_bytes(b"")
    html_file = sub_dir / "404.jpg"
    html_file.write_bytes(b"<!DOCTYPE html><html><head><title>404 Not Found</title></head></html>")
    valid_file = sub_dir / "valid.jpg"
    valid_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 300)

    # 2. 构建 SQLite 数据库
    db_path = tmp_path / "blogs.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY,
            group_key TEXT,
            author TEXT,
            title TEXT,
            date TEXT,
            images_json TEXT,
            image_paths_json TEXT
        );
    """)
    rel_zero = str(zero_file.relative_to(img_dir)).replace("\\", "/")
    rel_html = str(html_file.relative_to(img_dir)).replace("\\", "/")
    rel_valid = str(valid_file.relative_to(img_dir)).replace("\\", "/")

    # post 1: 包含早期官方已失效的死链
    conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, images_json, image_paths_json)
        VALUES (1, 'nogizaka', 'staff', '早期博客', '2012-02-15 10:00',
                '["http://www.nogizaka46.com/_pre/blog/dead1.jpg", "https://valid.cdn/ok.jpg"]',
                ?);
    """, (json.dumps([rel_zero, rel_valid]),))

    # post 2: 仅包含已删除或 404 文件
    conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, images_json, image_paths_json)
        VALUES (2, 'nogizaka', 'staff', '全部损坏', '2012-02-16 10:00',
                '["http://img.nogizaka46.com/blog/dead2.jpg"]',
                ?);
    """, (json.dumps([rel_html]),))
    conn.commit()

    stats = heal_corrupt_blog_images(conn)

    assert stats["deleted_files"] == 2
    assert stats["healed_posts"] == 2
    assert not zero_file.exists()
    assert not html_file.exists()
    assert valid_file.exists()

    # 验证 post 1 元数据已自愈
    row1 = conn.execute("SELECT images_json, image_paths_json FROM blog_posts WHERE id = 1").fetchone()
    imgs1 = json.loads(row1[0])
    paths1 = json.loads(row1[1])
    assert imgs1 == ["https://valid.cdn/ok.jpg"]
    assert paths1 == [rel_valid]

    # 验证 post 2 死链与损坏路径已被清空
    row2 = conn.execute("SELECT images_json, image_paths_json FROM blog_posts WHERE id = 2").fetchone()
    imgs2 = json.loads(row2[0])
    paths2 = json.loads(row2[1])
    assert imgs2 == []
    assert paths2 == []

    conn.close()


def test_get_blog_gallery_hole_filling_and_corrupt_filtering(tmp_path, monkeypatch):
    """验证 _get_blog_gallery 在遇到损坏图片或死链时，通过空洞补齐拉取有效图片填满一页。"""
    from src.webui_modules.archive.gallery import _get_blog_gallery
    import src.blog_fetcher as bf

    img_dir = tmp_path / "blog_images"
    img_dir.mkdir(parents=True)
    monkeypatch.setattr(bf, "BLOG_IMAGE_DIR", img_dir)

    # 创建一个小于等于 200 字节的损坏文件与一个正常大小文件
    corrupt_file = img_dir / "corrupt.jpg"
    corrupt_file.write_bytes(b"corrupt")
    valid_file = img_dir / "valid.jpg"
    valid_file.write_bytes(b"\xff\xd8\xff" + b"X" * 400)

    db_path = tmp_path / "blogs.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY,
            group_key TEXT,
            author TEXT,
            title TEXT,
            date TEXT,
            images_json TEXT,
            image_paths_json TEXT
        );
    """)
    # 记录 1: 本地文件损坏 (<= 200B)
    conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, image_paths_json)
        VALUES (1, 'nogizaka', '成员A', '损坏', '2012-01-01 10:00', '["corrupt.jpg"]');
    """)
    # 记录 2: 远端 404 死链 (_pre/blog)
    conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, images_json)
        VALUES (2, 'nogizaka', '成员A', '死链', '2012-01-02 10:00', '["http://www.nogizaka46.com/_pre/blog/a.jpg"]');
    """)
    # 记录 3: 有效图片
    conn.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, date, image_paths_json)
        VALUES (3, 'nogizaka', '成员A', '有效', '2012-01-03 10:00', '["valid.jpg"]');
    """)
    conn.commit()

    monkeypatch.setattr("src.webui_modules.archive_handlers.get_blog_db", lambda: conn)

    # 请求 per_page=1，最早优先。前 2 条损坏/死链应被跳过，空洞补齐直接命中第 3 条有效记录
    data = _get_blog_gallery(page=1, per_page=1, order="asc")
    assert data["ok"] is True
    assert len(data["photos"]) == 1
    assert data["photos"][0]["blog_id"] == "3"
    assert data["photos"][0]["text"] == "有效"

    conn.close()


def test_gallery_card_error_handling_contract():
    """验证前端渲染相册卡片绝不强制隐藏卡片，而是优雅展示占位图标。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")
    css = (root / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")

    # 1. 严禁使用 this.parentElement.style.display='none' 隐藏整个卡片
    assert "onerror=\"this.parentElement.style.display='none';\"" not in js[js.find("renderGalleryCards"):]

    # 2. 必须包含 img-broken 与 is-broken 状态标记
    assert r"this.classList.add(\'img-broken\');this.parentElement.classList.add(\'is-broken\');" in js

    # 3. CSS 中定义了 is-broken 的回退占位
    assert ".gallery-card.is-broken" in css
    assert ".gallery-card img.img-broken" in css


def test_gallery_member_switch_year_fallback_contract():
    """验证成员切换时先完成年份校验与自愈回退，再加载相册图片，杜绝异步竞争导致的 0 张图片空白假象。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    # 1. 切换成员时必须 await loadGalleryYears() 确保年份校验先于 loadGalleryPhotos 完成
    select_fn = js[js.find("async function selectGalleryMember"):js.find("function getGalleryGridCols")]
    assert "await loadGalleryYears();" in select_fn
    assert select_fn.find("await loadGalleryYears();") < select_fn.find("await loadGalleryPhotos(true);")

    # 2. loadGalleryYears 中具备失效年份自愈重置逻辑
    load_years_fn = js[js.find("async function loadGalleryYears"):js.find("function renderGalleryYearChips")]
    assert "!galleryYears.some(item => String(item.year) === String(curGalleryYear))" in load_years_fn
    assert 'curGalleryYear = "";' in load_years_fn

    # 3. 具备版本号屏障（galleryLoadVersion 和 galleryYearsVersion）防止过时异步回调竞态覆盖
    assert "galleryYearsVersion" in js
    assert "galleryLoadVersion" in js
    assert "myVersion !== galleryLoadVersion" in js





