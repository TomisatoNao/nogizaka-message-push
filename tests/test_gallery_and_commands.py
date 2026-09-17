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
    monkeypatch.setattr("src.blog_fetcher.init_blog_db", lambda: None)

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


def test_gallery_toolbar_three_rows_layout_contract():
    """验证相册筛选栏无论成员年份多少均严格保持图二的清晰三行排版布局规范。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    html = (root / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    css = (root / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")

    # 1. 结构契约：三行独立容器必须存在
    assert "gallery-toolbar-row1" in html
    assert "gallery-toolbar-row2" in html
    assert "gallery-toolbar-row3" in html

    # 2. 元素归属语义契约：
    # Row 1: 包含成员下拉选择与来源切换胶囊
    r1_idx = html.find("gallery-toolbar-row1")
    r2_idx = html.find("gallery-toolbar-row2")
    r3_idx = html.find("gallery-toolbar-row3")
    assert r1_idx < r2_idx < r3_idx

    r1_content = html[r1_idx:r2_idx]
    assert "galleryMemberDropdownWrap" in r1_content
    assert "gallerySourceChips" in r1_content

    # Row 2: 年份切换胶囊独占整行
    r2_content = html[r2_idx:r3_idx]
    assert "galleryYearChips" in r2_content

    # Row 3: 时间排序按钮在左，统计数量在右
    r3_end = html.find("</div>", r3_idx)
    r3_content = html[r3_idx:r3_end + 300]
    assert "btnGallerySortOrder" in r3_content
    assert "galleryStats" in r3_content

    # 3. CSS 样式契约
    assert ".gallery-toolbar {" in css
    assert "flex-direction: column;" in css
    assert ".gallery-toolbar-row" in css
    assert ".gallery-toolbar-row1" in css
    assert ".gallery-toolbar-row2" in css
    assert ".gallery-toolbar-row3" in css
    assert "justify-content: space-between;" in css


def test_fair_random_photo_weighted_sampling(tmp_path, monkeypatch):
    """验证 /美图 抽图多源加权公平随机算法：彻底消除级联偏置，支持博客与消息双向抽选及安全降级。"""
    from collections import Counter
    import src.blog_fetcher as bf
    import src.archive_query as aq

    # 1. 构建独立测试归档 messages 数据库（只有 1 张照片）
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True)
    conn_msg = sqlite3.connect(str(archive_dir / "archive.db"))
    conn_msg.execute("""
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
    m_dir = archive_dir / "海邉朱莉" / "2025" / "01" / "picture"
    m_dir.mkdir(parents=True)
    msg_pic = m_dir / "msg_single.jpg"
    msg_pic.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 300)
    conn_msg.execute("""
        INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, text, local_file, raw_json)
        VALUES ('msg_kaibe_1', '海邉朱莉', '海邉朱莉', 2025, 1, 'picture', '2025-01-10T12:00:00Z', '初次见面！', '2025/01/picture/msg_single.jpg', '{}');
    """)
    conn_msg.commit()

    # 2. 构建独立测试博客数据库（有 5 篇博文，共 10 张配图）
    blog_img_dir = tmp_path / "blog_images"
    blog_img_dir.mkdir(parents=True)
    conn_blog = sqlite3.connect(str(tmp_path / "blogs.db"))
    conn_blog.execute("""
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
    conn_blog.execute("""
        CREATE TABLE IF NOT EXISTS blog_watermarks (
            group_key TEXT PRIMARY KEY,
            last_url TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
    """)

    blog_files = []
    for i in range(1, 6):
        p_dir = blog_img_dir / "nogizaka" / f"post_{i}"
        p_dir.mkdir(parents=True)
        f1 = p_dir / "01.jpg"
        f1.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 300)
        f2 = p_dir / "02.jpg"
        f2.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 300)
        rel1 = f"nogizaka/post_{i}/01.jpg"
        rel2 = f"nogizaka/post_{i}/02.jpg"
        blog_files.extend([f1, f2])
        conn_blog.execute(
            "INSERT INTO blog_posts (id, group_key, author, title, date, image_paths_json) VALUES (?, 'nogizaka', '海邉 朱莉', ?, '2026-03-10 12:00:00', ?);",
            (i, f"博文_{i}", json.dumps([rel1, rel2]))
        )
    conn_blog.commit()

    # Mock 环境
    monkeypatch.setattr(aq, "_get_init_db", lambda: conn_msg)
    monkeypatch.setattr(aq, "_get_archive_root", lambda: archive_dir)
    monkeypatch.setattr(bf, "init_blog_db", lambda: conn_blog)
    monkeypatch.setattr(bf, "BLOG_IMAGE_DIR", blog_img_dir)
    aq._random_photo_counts_cache.clear()

    # 3. 抽样 40 次：因为博客有 10 张图，消息只有 1 张图，加权算法下必须能抽中大量博客图片！
    draws = Counter()
    for _ in range(40):
        photo = aq.get_random_photo(member_dir="海邉朱莉")
        assert photo is not None
        draws[photo["source"]] += 1

    # 验证博客图片必须被抽中且占主要多数（原版代码博客为 0%）
    assert draws["blog"] > 0
    assert draws["blog"] >= draws["message"]

    # 4. 容错测试：当博客图片被删除时，安全降级抽中消息图片
    for f in blog_files:
        f.unlink(missing_ok=True)
    fallback_photo = aq.get_random_photo(member_dir="海邉朱莉")
    assert fallback_photo is not None
    assert fallback_photo["source"] == "message"
    assert fallback_photo["id"] == "msg_kaibe_1"


def test_gallery_per_page_performance_optimization_contract():
    """验证相册单页数量性能优化契约（从40降至16~24，优先保证移动端与大屏低延迟和流畅度）。"""
    from pathlib import Path
    import inspect
    from src.webui_modules.archive.gallery import _get_blog_gallery
    from src.archive_query import get_gallery_photos

    root = Path(__file__).resolve().parent.parent
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    # 1. 前端计算契约：移动端采用 16 张，桌面端以 20 张为基准（5 列时 4 行共 20 张），消除 40 张导致的渲染与流量负担
    assert "const targetCards = isMobile ? 16 : 20;" in js
    assert "curGalleryPerPage = 20;" in js

    # 2. 后端函数签名默认值契约：默认 20 张
    blog_sig = inspect.signature(_get_blog_gallery)
    assert blog_sig.parameters["per_page"].default == 20

    msg_sig = inspect.signature(get_gallery_photos)
    assert msg_sig.parameters["per_page"].default == 20


def test_gallery_infinite_scroll_contract():
    """验证相册无感无限滚动（IntersectionObserver 触底预载与优雅到底提示）前端契约。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    css = (root / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    # 1. HTML 节点完整性契约：包含加载哨兵/按钮与到底提示
    assert 'id="galleryLoadMore"' in html
    assert 'id="galleryEndHint"' in html
    assert '已加载全部图片' in html

    # 2. CSS 样式契约：包含到底提示的居中、低对比与分割线修饰
    assert ".gallery-end-hint {" in css
    assert "#galleryLoadMore:disabled" in css

    # 3. JS 行为契约：基于 IntersectionObserver 的触底预载与状态管理
    assert "initGalleryObserver" in js
    assert "IntersectionObserver" in js
    assert 'rootMargin: "350px 0px"' in js
    assert "galleryObserver.observe(sentinel)" in js
    assert 'endHint.style.display = (!curGalleryHasMore && curGalleryImages.length > 0) ? "block" : "none";' in js
    assert "正在加载更多图片..." in js
    assert "加载失败，点击重试" in js


def test_thumbnail_pipeline_generation_and_caching(tmp_path, monkeypatch):
    """验证 WebP 缩略图管线生成、等比缩放、两级哈希持久化缓存与损坏安全降级。"""
    from PIL import Image
    from src.webui_modules.archive.thumbnails import get_or_create_thumbnail

    # 隔离测试缓存目录
    test_cache_dir = tmp_path / "cache_thumbs"
    monkeypatch.setattr("src.webui_modules.archive.thumbnails.THUMBNAIL_CACHE_DIR", test_cache_dir)

    # 1. 构造一个 1200x800 的测试源图
    src_img_path = tmp_path / "source_big.jpg"
    im = Image.new("RGB", (1200, 800), color=(240, 100, 50))
    im.save(src_img_path, format="JPEG")

    # 2. 首次生成：验证等比缩放至 480 宽并存为 WebP
    thumb_path = get_or_create_thumbnail(src_img_path, max_width=480, quality=80)
    assert thumb_path is not None
    assert thumb_path.is_file()
    assert thumb_path.suffix == ".webp"

    with Image.open(thumb_path) as thumb_im:
        assert thumb_im.size == (480, 320)
        assert thumb_im.format == "WEBP"

    # 3. 二次调用：验证命中磁盘持久化缓存（不会抛错并直接返回）
    cached_path = get_or_create_thumbnail(src_img_path, max_width=480, quality=80)
    assert cached_path == thumb_path

    # 4. 容错测试：损坏/不存在文件安全返回 None 降级
    non_existent = tmp_path / "not_found.jpg"
    assert get_or_create_thumbnail(non_existent) is None

    corrupt_file = tmp_path / "corrupt.jpg"
    corrupt_file.write_bytes(b"not_an_image_data")
    assert get_or_create_thumbnail(corrupt_file) is None


def test_media_service_immutable_cache_control(tmp_path):
    """验证归档媒体流 HTTP 200/206/304 具备 1 年期 immutable 强缓存头。"""
    from src.webui_modules.media_service import serve_file_range

    test_file = tmp_path / "test_media.jpg"
    test_file.write_bytes(b"dummy_media_bytes_1234567890")

    class DummyHandler:
        def __init__(self):
            self.headers = {}
            self.response_code = 200
            self.sent_headers = {}
            self.wfile = bytearray()

        def send_response(self, code):
            self.response_code = code

        def send_header(self, k, v):
            self.sent_headers[k] = v

        def end_headers(self):
            pass

    # 1. 验证标准 200 响应携带 immutable 强缓存头
    h200 = DummyHandler()
    h200.wfile = type("WFile", (), {"write": lambda self, b: None})()
    serve_file_range(h200, test_file)
    assert h200.response_code == 200
    assert "immutable" in h200.sent_headers.get("Cache-Control", "")
    assert "max-age=31536000" in h200.sent_headers.get("Cache-Control", "")

    # 2. 验证 304 条件缓存同样携带 immutable 强缓存头
    h304 = DummyHandler()
    h304.headers["If-None-Match"] = h200.sent_headers.get("ETag", "")
    serve_file_range(h304, test_file)
    assert h304.response_code == 304
    assert "immutable" in h304.sent_headers.get("Cache-Control", "")


def test_gallery_card_thumbnail_frontend_contract():
    """验证前端相册卡片消费 WebP 缩略图（?thumb=1）且与大图预览分离契约。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    # 验证网格卡片加载时注入 ?thumb=1
    assert 'thumbUrl += (photo.url.includes("?") ? "&thumb=1" : "?thumb=1");' in js
    assert '<img src="\' + esc(thumbUrl) +' in js
    # 验证 Lightbox 依然保存原始无损大图 URL
    assert 'url: photo.url,' in js
    # 验证跨页去重与灯箱无缝渐进占位防止错位闪烁
    assert "existingUrls.has(photo.url)" in js
    assert 'function openLightbox(i, opener, caption, placeholderUrl)' in js
    assert '$("lbImg").removeAttribute("src");' in js


def test_blog_images_optimization_and_fallback_contract():
    """验证博客页卡片封面 WebP 缩略图接入、三级降级容错及阅读器 0ms 占位契约。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    # 1. 缩略图生成接入契约
    assert "function _getBlogThumbUrl(url)" in js
    assert 'url.startsWith("/api/archive/blog_media/")' in js
    assert 'let coverThumbUrl = _getBlogThumbUrl(coverUrl);' in js
    assert 'const coverThumbUrl = _getBlogThumbUrl(coverUrl);' in js

    # 2. 三级降级容错契约（缩略图 -> 本地原图 -> 官方远程 -> 文本占位）
    assert 'data-full-src="' in js
    assert 'dataset.triedFull !== "1"' in js
    assert 'dataset.triedFull = "1"' in js
    assert 'heroCoverImg.src = fullSrc;' in js
    assert 'coverImg.src = fullSrc;' in js
    assert '<div class="bc-cover no-pic">📝</div>' in js

    # 3. 博客正文阅读器图片点击 0ms 秒开占位契约
    assert "openLightbox(idx, img, null, img.src);" in js


def test_gallery_lightbox_original_image_and_actions_contract():
    """验证灯箱查看高清原图加载保障、查看/下载原图入口及竞态消除契约。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    css = (root / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    # 1. 验证 HTML 动作栏、原图直达链接与下载按钮契约
    assert 'id="lbActions"' in html
    assert 'id="lbOriginalBtn"' in html
    assert 'id="lbDownloadBtn"' in html
    assert 'id="lbStatus"' in html

    # 2. 验证 CSS 样式契约
    assert ".lb-actions" in css
    assert ".lb-status" in css
    assert ".lb-btn" in css
    assert "#lightbox img.lb-preview" in css
    assert "#lightbox img.lb-full" in css

    # 3. 验证 JS 原图加载逻辑契约（事件先于赋值，解决事件丢失与缓存竞态）
    assert "preloader.onload = onDone;" in js
    assert "preloader.onerror =" in js
    assert "preloader.src = targetUrl;" in js
    assert "if (preloader.complete && preloader.naturalWidth > 0)" in js
    assert '$("lbOriginalBtn").href = targetUrl' in js
    assert '$("lbDownloadBtn")' in js
    assert 'a.download = filename;' in js


def test_view_components_isolation_contract():
    """验证相册、信件、博客和首页各视图首屏预加载时的组件强隔离契约。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "src" / "webui_static" / "archive.html").read_text(encoding="utf-8")
    css = (root / "src" / "webui_static" / "archive.css").read_text(encoding="utf-8")
    js = (root / "src" / "webui_static" / "archive.js").read_text(encoding="utf-8")

    # 1. 验证 HTML 语义类名与元素 ID
    assert 'class="toolbar msg-search-toolbar"' in html
    assert 'id="tagToggleWrap"' in html

    # 2. 验证 CSS 视图强隔离规则（杜绝首屏网络请求未完成时的组件暴露闪烁）
    assert "html.view-gallery #archiveSide" in css
    assert "html.view-gallery .msg-search-toolbar" in css
    assert "html.view-letter #archiveSide" in css
    assert "html.view-letter .msg-search-toolbar" in css
    assert "html.view-blog #tagToggleWrap" in css
    assert "html.view-home #archiveSide" in css
    assert "html.view-home .msg-search-toolbar" in css

    # 3. 验证 JS boot() 预热预路由隔离逻辑
    assert '$("archiveSide").style.display = "none";' in js
    assert '$("tagToggleWrap").style.display = "none";' in js












