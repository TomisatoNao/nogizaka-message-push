"""
tests/test_user_interactions.py — 验证用户消息收藏与点赞功能
涵盖：
  1. 核心存储层：收藏与点赞状态设置、取消、批量获取、全历史筛选与多用户隔离
  2. 级联清理：删除用户时同步清理交互数据
  3. API 鉴权层：匿名访客拦截 401，登录用户正常操作与全历史筛选
  4. 归档消息装配：归档消息列表自动附带当前用户的 is_favorite 与 is_liked 状态
"""
import io
import json
import pytest

import config.config as cfg
from src import archive as _archive
from src import auth as _auth
from src.webui_modules.archive.messages import handle_messages


@pytest.fixture(autouse=True)
def setup_test_auth_db(monkeypatch, tmp_path):
    """每个测试独立使用临时的 auth.db 与 archive.db。"""
    test_auth_db = tmp_path / "auth.db"
    test_archive_dir = tmp_path / "archive"
    test_archive_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(_auth, "AUTH_DB_PATH", test_auth_db)
    monkeypatch.setattr(_auth, "_auth_conn", None)
    monkeypatch.setattr(_auth, "_sessions", {})
    monkeypatch.setattr(_auth, "_sessions_loaded_from_db", False)

    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(test_archive_dir))
    monkeypatch.setattr(_archive, "_sqlite_conn", None)
    monkeypatch.setattr(_archive, "_schema_initialized", False)

    # 初始化表结构
    _auth.get_auth_db()
    _archive.init_db()

    yield

    # 清理连接
    conn = getattr(_auth, "_auth_conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
        _auth._auth_conn = None

    _archive.close_db()


def test_interaction_set_and_get():
    """测试单个消息的收藏与点赞状态切换及批量获取。"""
    username = "test_user_1"

    # 1. 初始状态获取为空
    res = _auth.get_user_message_interactions(username, ["msg_001", "msg_002"])
    assert res == {}

    # 2. 收藏 msg_001
    fav_res = _auth.set_user_message_interaction(
        username=username,
        message_id="msg_001",
        member_dir="nao",
        member_name="冨里奈央",
        action="favorite",
        value=True,
    )
    assert fav_res["ok"] is True
    assert fav_res["is_favorite"] is True
    assert fav_res["is_liked"] is False

    # 3. 点赞 msg_001
    like_res = _auth.set_user_message_interaction(
        username=username,
        message_id="msg_001",
        action="like",
        value=True,
    )
    assert like_res["ok"] is True
    assert like_res["is_favorite"] is True
    assert like_res["is_liked"] is True

    # 4. 批量查询
    batch = _auth.get_user_message_interactions(username, ["msg_001", "msg_002"])
    assert "msg_001" in batch
    assert batch["msg_001"]["is_favorite"] is True
    assert batch["msg_001"]["is_liked"] is True
    assert "msg_002" not in batch

    # 5. 取消收藏 msg_001
    unfav = _auth.set_user_message_interaction(
        username=username,
        message_id="msg_001",
        action="favorite",
        value=False,
    )
    assert unfav["is_favorite"] is False
    assert unfav["is_liked"] is True

    batch_after = _auth.get_user_message_interactions(username, ["msg_001"])
    assert batch_after["msg_001"]["is_favorite"] is False
    assert batch_after["msg_001"]["is_liked"] is True


def test_interaction_multi_user_isolation():
    """测试不同登录账号之间数据的绝对隔离。"""
    user_a = "alice"
    user_b = "bob"

    # Alice 收藏 msg_100
    _auth.set_user_message_interaction(user_a, "msg_100", action="favorite", value=True)

    # Bob 查询不应看到 Alice 的收藏
    bob_records = _auth.get_user_message_interactions(user_b, ["msg_100"])
    assert bob_records == {}

    bob_list, bob_total = _auth.list_user_interacted_messages(user_b, action="favorite")
    assert bob_total == 0
    assert len(bob_list) == 0

    # Alice 查询应能看到收藏
    alice_records = _auth.get_user_message_interactions(user_a, ["msg_100"])
    assert alice_records["msg_100"]["is_favorite"] is True

    alice_list, alice_total = _auth.list_user_interacted_messages(user_a, action="favorite")
    assert alice_total == 1
    assert alice_list[0]["message_id"] == "msg_100"


def test_interaction_list_and_pagination():
    """测试收藏/点赞全历史列表的分页与成员过滤。"""
    user = "curator"
    # 模拟为成员 nao 收藏 3 条，为 sakura 收藏 2 条
    for i in range(1, 4):
        _auth.set_user_message_interaction(user, f"nao_{i}", member_dir="nao", member_name="冨里奈央", action="favorite", value=True)
    for i in range(1, 3):
        _auth.set_user_message_interaction(user, f"sakura_{i}", member_dir="sakura", member_name="小島凪紗", action="favorite", value=True)

    # 1. 查询全员收藏
    all_fav, total_all = _auth.list_user_interacted_messages(user, action="favorite", page=1, per_page=10)
    assert total_all == 5
    assert len(all_fav) == 5

    # 2. 分页（每页 2 条）
    p1, _ = _auth.list_user_interacted_messages(user, action="favorite", page=1, per_page=2)
    p2, _ = _auth.list_user_interacted_messages(user, action="favorite", page=2, per_page=2)
    p3, _ = _auth.list_user_interacted_messages(user, action="favorite", page=3, per_page=2)
    assert len(p1) == 2
    assert len(p2) == 2
    assert len(p3) == 1

    # 3. 按成员过滤
    nao_fav, nao_total = _auth.list_user_interacted_messages(user, action="favorite", member_dir="nao")
    assert nao_total == 3
    assert len(nao_fav) == 3
    assert all(r["member_dir"] == "nao" for r in nao_fav)


def test_user_delete_cascade_cleanup():
    """测试删除用户时级联清理互动数据。"""
    user = "temp_user"
    _auth.add_user(user, "password123", role="viewer")
    _auth.set_user_message_interaction(user, "msg_del_test", action="favorite", value=True)

    records = _auth.get_user_message_interactions(user, ["msg_del_test"])
    assert "msg_del_test" in records

    # 删除用户
    ok, msg = _auth.delete_user(user)
    assert ok is True

    # 互动数据应被清理
    records_after = _auth.get_user_message_interactions(user, ["msg_del_test"])
    assert records_after == {}


class _MockHandler:
    """模拟 HTTP 请求处理器，用于测试 WebUI 路由。"""
    def __init__(self, path: str, headers: dict = None, client_ip: str = "127.0.0.1"):
        self.path = path
        self.headers = headers or {}
        self.client_address = (client_ip, 12345)
        self.wfile = io.BytesIO()
        self._response_code = 200
        self._headers_sent = {}

    def send_response(self, code: int):
        self._response_code = code

    def send_header(self, key: str, value: str):
        self._headers_sent[key] = value

    def end_headers(self):
        pass

    def get_json_response(self) -> dict:
        data = self.wfile.getvalue()
        if not data:
            return {}
        return json.loads(data.decode("utf-8"))


def test_api_anonymous_access_denied(monkeypatch):
    """测试匿名访客请求收藏或点赞接口时被 401 拦截。"""
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)

    # 1. 匿名尝试切换收藏状态
    handler = _MockHandler("/api/archive/message/interaction")
    body = {"message_id": "m123", "action": "favorite", "value": True}
    res = handle_messages(handler, "message/interaction", lambda **_: True, lambda: body)
    assert res is True
    assert handler._response_code == 401
    resp_data = handler.get_json_response()
    assert resp_data["ok"] is False
    assert "登录" in resp_data["errors"][0]

    # 2. 匿名尝试查询互动列表
    handler2 = _MockHandler("/api/archive/messages/interactions?action=favorite")
    res2 = handle_messages(handler2, "messages/interactions", lambda **_: True, lambda: {})
    assert res2 is True
    assert handler2._response_code == 401


def test_api_logged_in_interaction(monkeypatch):
    """测试已登录用户正常切换收藏/点赞并查询全历史列表。"""
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)

    # 准备测试用户与 Session
    username = "viewer_bob"
    _auth.add_user(username, "secret_pwd", role="viewer")
    token = _auth.create_session(username, role="viewer", ttl_seconds=7200)
    cookie_header = {"Cookie": f"sakamichi_session={token}"}

    # 1. 登录用户收藏消息
    handler = _MockHandler("/api/archive/message/interaction", headers=cookie_header)
    body = {"message_id": "msg_auth_1", "member": "nao", "action": "favorite", "value": True}
    res = handle_messages(handler, "message/interaction", lambda **_: True, lambda: body)
    assert res is True
    assert handler._response_code == 200
    resp_data = handler.get_json_response()
    assert resp_data["ok"] is True
    assert resp_data["is_favorite"] is True
    assert resp_data["is_liked"] is False

    # 2. 查询收藏列表
    handler_list = _MockHandler("/api/archive/messages/interactions?action=favorite", headers=cookie_header)
    res_list = handle_messages(handler_list, "messages/interactions", lambda **_: True, lambda: {})
    assert res_list is True
    assert handler_list._response_code == 200
    list_data = handler_list.get_json_response()
    assert list_data["ok"] is True
    assert list_data["total"] == 1
    assert list_data["messages"][0]["id"] == "msg_auth_1"
    assert list_data["messages"][0]["is_favorite"] is True


def test_api_messages_includes_interaction_flags(monkeypatch, tmp_path):
    """测试归档消息列表接口对登录用户返回其收藏/点赞状态，对匿名访客返回 False。"""
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)
    monkeypatch.setattr(cfg, "AUTH_ARCHIVE_PUBLIC", True)

    # 在归档库与月度 JSON 中创建一条测试消息
    member_name = "冨里奈央"
    member_dir = _archive.member_dir_name(member_name)
    m_dir = tmp_path / "archive" / member_dir / "2026" / "09"
    m_dir.mkdir(parents=True, exist_ok=True)
    msg_item = {
        "id": "msg_test_flag_1",
        "type": "text",
        "text": "测试消息内容",
        "published_at": "2026-09-10T12:00:00Z",
        "updated_at": "2026-09-10T12:00:00Z",
    }
    with open(m_dir / "messages.json", "w", encoding="utf-8") as f:
        json.dump([msg_item], f)

    # 准备用户并收藏该消息
    username = "alice_flag"
    _auth.add_user(username, "pwd12345", role="viewer")
    token = _auth.create_session(username, role="viewer", ttl_seconds=7200)
    cookie_header = {"Cookie": f"sakamichi_session={token}"}

    _auth.set_user_message_interaction(username, "msg_test_flag_1", member_dir=member_dir, member_name=member_name, action="favorite", value=True)

    # 1. 登录用户 Alice 访问消息列表，应该看到 is_favorite=True
    handler_auth = _MockHandler(f"/api/archive/messages?member={member_dir}&year=2026&month=9", headers=cookie_header)
    res = handle_messages(handler_auth, "messages", lambda **_: True, lambda: {})
    assert res is True
    assert handler_auth._response_code == 200
    data_auth = handler_auth.get_json_response()
    assert data_auth["ok"] is True
    assert len(data_auth["messages"]) == 1
    assert data_auth["messages"][0]["id"] == "msg_test_flag_1"
    assert data_auth["messages"][0]["is_favorite"] is True
    assert data_auth["messages"][0]["is_liked"] is False

    # 2. 匿名用户访问同一个消息列表，应该看到 is_favorite=False, is_liked=False
    handler_anon = _MockHandler(f"/api/archive/messages?member={member_dir}&year=2026&month=9")
    res_anon = handle_messages(handler_anon, "messages", lambda **_: True, lambda: {})
    assert res_anon is True
    assert handler_anon._response_code == 200
    data_anon = handler_anon.get_json_response()
    assert data_anon["ok"] is True
    assert len(data_anon["messages"]) == 1
    assert data_anon["messages"][0]["id"] == "msg_test_flag_1"
    assert data_anon["messages"][0]["is_favorite"] is False
    assert data_anon["messages"][0]["is_liked"] is False


def test_api_composite_months_and_calendar_filtering(monkeypatch, tmp_path):
    """测试月份列表、日历与消息列表的正交复合筛选（type + favorite）。"""
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)

    member_name = "冨里奈央"
    member_dir = _archive.member_dir_name(member_name)

    # 准备归档目录与测试消息
    dir_09 = tmp_path / "archive" / member_dir / "2026" / "09"
    dir_08 = tmp_path / "archive" / member_dir / "2026" / "08"
    dir_09.mkdir(parents=True, exist_ok=True)
    dir_08.mkdir(parents=True, exist_ok=True)

    msg1 = {"id": "m1", "type": "text", "text": "文字消息", "published_at": "2026-09-10T12:00:00Z", "updated_at": "2026-09-10T12:00:00Z"}
    msg2 = {"id": "m2", "type": "picture", "text": "图片消息", "published_at": "2026-09-11T12:00:00Z", "updated_at": "2026-09-11T12:00:00Z"}
    msg3 = {"id": "m3", "type": "picture", "text": "8月图片", "published_at": "2026-08-15T12:00:00Z", "updated_at": "2026-08-15T12:00:00Z"}

    with open(dir_09 / "messages.json", "w", encoding="utf-8") as f:
        json.dump([msg1, msg2], f)
    with open(dir_08 / "messages.json", "w", encoding="utf-8") as f:
        json.dump([msg3], f)

    # 同步写入 SQLite 归档索引
    conn = _archive.init_db()
    for yr, mo, m in [(2026, 9, msg1), (2026, 9, msg2), (2026, 8, msg3)]:
        conn.execute("""
            INSERT INTO messages (id, member_name, member_dir, year, month, type, published_at, updated_at, text, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (m["id"], member_name, member_dir, yr, mo, m["type"], m["published_at"], m["updated_at"], m["text"], json.dumps(m)))
    conn.commit()

    # 创建测试登录用户 Alice
    alice = "alice_composite"
    _auth.add_user(alice, "pwd12345", role="viewer")
    token_alice = _auth.create_session(alice, role="viewer", ttl_seconds=7200)
    cookie_alice = {"Cookie": f"sakamichi_session={token_alice}"}

    # Alice 收藏 m1 (9月文字) 和 m3 (8月图片)
    _auth.set_user_message_interaction(alice, "m1", member_dir=member_dir, action="favorite", value=True)
    _auth.set_user_message_interaction(alice, "m3", member_dir=member_dir, action="favorite", value=True)

    # 1. 验证月份列表：全量统计 vs 收藏统计 vs (收藏 + 图片) 复合统计
    # 1.1 全量统计 (无 filter)
    h_m_all = _MockHandler(f"/api/archive/months?member={member_dir}", headers=cookie_alice)
    assert handle_messages(h_m_all, "months", lambda **_: True, lambda: {}) is True
    res_m_all = h_m_all.get_json_response()["months"]
    month_counts_all = {f"{m['year']}-{m['month']:02d}": m["count"] for m in res_m_all}
    assert month_counts_all["2026-09"] == 2
    assert month_counts_all["2026-08"] == 1

    # 1.2 收藏统计 (favorite=1)
    h_m_fav = _MockHandler(f"/api/archive/months?member={member_dir}&favorite=1", headers=cookie_alice)
    assert handle_messages(h_m_fav, "months", lambda **_: True, lambda: {}) is True
    res_m_fav = h_m_fav.get_json_response()["months"]
    month_counts_fav = {f"{m['year']}-{m['month']:02d}": m["count"] for m in res_m_fav}
    assert month_counts_fav["2026-09"] == 1
    assert month_counts_fav["2026-08"] == 1

    # 1.3 复合统计 (favorite=1 & type=picture)
    h_m_fav_pic = _MockHandler(f"/api/archive/months?member={member_dir}&favorite=1&type=picture", headers=cookie_alice)
    assert handle_messages(h_m_fav_pic, "months", lambda **_: True, lambda: {}) is True
    res_m_fav_pic = h_m_fav_pic.get_json_response()["months"]
    month_counts_fav_pic = {f"{m['year']}-{m['month']:02d}": m["count"] for m in res_m_fav_pic}
    assert month_counts_fav_pic["2026-09"] == 0  # 9月没有被收藏的图片
    assert month_counts_fav_pic["2026-08"] == 1  # 8月有 1 条被收藏的图片

    # 2. 验证日历热力图复合筛选 (calendar)
    # 2.1 收藏日历
    h_cal_fav = _MockHandler(f"/api/archive/calendar?member={member_dir}&favorite=1", headers=cookie_alice)
    assert handle_messages(h_cal_fav, "calendar", lambda **_: True, lambda: {}) is True
    cal_days_fav = h_cal_fav.get_json_response()["days"]
    assert "2026-09-10" in cal_days_fav
    assert "2026-08-15" in cal_days_fav
    assert "2026-09-11" not in cal_days_fav  # 未收藏消息的日期不点亮

    # 2.2 收藏 + 图片复合日历
    h_cal_fav_pic = _MockHandler(f"/api/archive/calendar?member={member_dir}&favorite=1&type=picture", headers=cookie_alice)
    assert handle_messages(h_cal_fav_pic, "calendar", lambda **_: True, lambda: {}) is True
    cal_days_fav_pic = h_cal_fav_pic.get_json_response()["days"]
    assert "2026-08-15" in cal_days_fav_pic
    assert "2026-09-10" not in cal_days_fav_pic  # 9-10 是文字消息，被过滤

    # 3. 验证消息列表复合筛选 (messages)
    # 3.1 9月收藏列表
    h_msg_fav = _MockHandler(f"/api/archive/messages?member={member_dir}&year=2026&month=9&favorite=1", headers=cookie_alice)
    assert handle_messages(h_msg_fav, "messages", lambda **_: True, lambda: {}) is True
    msgs_fav = h_msg_fav.get_json_response()["messages"]
    assert len(msgs_fav) == 1
    assert msgs_fav[0]["id"] == "m1"

    # 3.2 9月收藏 + 图片列表（应为空）
    h_msg_fav_pic = _MockHandler(f"/api/archive/messages?member={member_dir}&year=2026&month=9&favorite=1&type=picture", headers=cookie_alice)
    assert handle_messages(h_msg_fav_pic, "messages", lambda **_: True, lambda: {}) is True
    msgs_fav_pic = h_msg_fav_pic.get_json_response()["messages"]
    assert len(msgs_fav_pic) == 0

    # 4. 多用户隔离：创建用户 Bob，查询其收藏状态应全部为空
    bob = "bob_clean"
    _auth.add_user(bob, "pwd12345", role="viewer")
    token_bob = _auth.create_session(bob, role="viewer", ttl_seconds=7200)
    cookie_bob = {"Cookie": f"sakamichi_session={token_bob}"}

    h_bob_m = _MockHandler(f"/api/archive/months?member={member_dir}&favorite=1", headers=cookie_bob)
    handle_messages(h_bob_m, "months", lambda **_: True, lambda: {})
    res_bob_m = h_bob_m.get_json_response()["months"]
    assert all(m["count"] == 0 for m in res_bob_m)

    h_bob_cal = _MockHandler(f"/api/archive/calendar?member={member_dir}&favorite=1", headers=cookie_bob)
    handle_messages(h_bob_cal, "calendar", lambda **_: True, lambda: {})
    assert h_bob_cal.get_json_response()["days"] == {}

