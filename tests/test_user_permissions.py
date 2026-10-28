"""用户权限升级与独立功能配额测试套件。

涵盖：
1. 基础能力判定（has_permission, get_user_capabilities）
2. 权限授予、修改、乐观锁版本控制、到期失效与撤销（set_user_permission）
3. 每日原子配额结算、JST 站点日界线与超额拦截（reserve_translation_quota）
4. 用户删除级联清理授权与用量账本
5. /api/auth/me 与 /api/users 接口契约
6. 双层路由鉴权（blogs/translate 放行授权用户，拦截无权用户；delete_translation 与 force 严格保护）
7. 缓存命中零扣额、单篇并发锁与单用户在途限制
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from src import auth
from src.webui_modules import archive_handlers
from src.webui_modules.archive import common as archive_common
from src.webui_modules.archive.blogs import _user_in_flight_translations
from src.webui_modules.auth_handlers import handle_auth_me, handle_users_get, handle_users_write


class _DummyHandler:
    def __init__(self, user=None, body=None, path="/"):
        self.command = "GET"
        self.path = path
        self._user = user
        self.payload = None
        self.code = None
        self._pending_headers = []
        self._pending_set_cookies = []

        cookie_str = ""
        if user and isinstance(user, dict):
            token = auth.create_session(user.get("username", "test"), user.get("role", "viewer"), 3600)
            cookie_str = f"sakamichi_session={token}"
        self.headers = {"Cookie": cookie_str}

    def _current_user(self):
        return self._user

    def send_response(self, code):
        self.code = code

    def send_header(self, key, value):
        self._pending_headers.append((key, value))

    def end_headers(self):
        pass

    @property
    def wfile(self):
        class _WFile:
            def __init__(self, parent):
                self.parent = parent

            def write(self, data):
                try:
                    self.parent.payload = json.loads(data.decode("utf-8"))
                except Exception:
                    self.parent.payload = data
        return _WFile(self)

    def _send_json(self, payload, code=200):
        self.payload = payload
        self.code = code


@pytest.fixture(autouse=True)
def _isolate_auth_and_flights(tmp_path, monkeypatch):
    """隔离 auth 数据库与用户在途翻译任务集合。"""
    db_file = tmp_path / "test_auth.db"
    monkeypatch.setattr(auth, "AUTH_DB_PATH", db_file)
    monkeypatch.setattr(auth, "USERS_PATH", db_file)
    auth._auth_conn = None
    _user_in_flight_translations.clear()

    # 初始化测试用户：admin 与 viewer
    auth.add_user("admin_user", "AdminPassword123!", role="admin")
    auth.add_user("friend_user", "FriendPassword123!", role="viewer")
    auth.add_user("viewer_user", "ViewerPassword123!", role="viewer")
    yield
    auth._auth_conn = None
    _user_in_flight_translations.clear()


# ================================================================
# 1. 权限判定与能力列表
# ================================================================

def test_admin_has_all_registered_permissions():
    assert auth.has_permission("admin_user", auth.PERM_BLOG_TRANSLATE) is True
    assert auth.has_permission({"username": "admin_user", "role": "admin"}, auth.PERM_BLOG_TRANSLATE) is True
    caps = auth.get_user_capabilities({"username": "admin_user", "role": "admin"})
    assert auth.PERM_BLOG_TRANSLATE in caps


def test_anonymous_and_invalid_permissions_denied():
    assert auth.has_permission(None, auth.PERM_BLOG_TRANSLATE) is False
    assert auth.has_permission("", auth.PERM_BLOG_TRANSLATE) is False
    assert auth.has_permission("admin_user", "unregistered.permission") is False
    assert auth.has_permission("non_existent_user", auth.PERM_BLOG_TRANSLATE) is False


def test_viewer_permission_grant_lifecycle():
    # 默认无权
    assert auth.has_permission("friend_user", auth.PERM_BLOG_TRANSLATE) is False
    assert auth.get_user_capabilities("friend_user") == []

    # 管理员授权
    ok, msg = auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        daily_limit=15,
    )
    assert ok is True
    assert auth.PERM_BLOG_TRANSLATE in msg

    # 授权后生效
    assert auth.has_permission("friend_user", auth.PERM_BLOG_TRANSLATE) is True
    assert auth.get_user_capabilities("friend_user") == [auth.PERM_BLOG_TRANSLATE]
    grants = {g["permission"]: g for g in auth.get_user_permission_grants("friend_user")}
    assert auth.PERM_BLOG_TRANSLATE in grants
    assert grants[auth.PERM_BLOG_TRANSLATE]["daily_limit"] == 15
    assert grants[auth.PERM_BLOG_TRANSLATE]["revision"] == 1

    # 撤销权限
    ok, msg = auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=False,
    )
    assert ok is True
    assert auth.has_permission("friend_user", auth.PERM_BLOG_TRANSLATE) is False
    assert auth.get_user_capabilities("friend_user") == []


def test_permission_expiration(monkeypatch):
    # 过期时间在过去应被拒绝
    past_time = time.time() - 3600
    ok, err_msg = auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        expires_at=past_time,
    )
    assert ok is False
    assert "大于当前时间" in err_msg

    # 设置未来到期时间
    future_time = time.time() + 100
    ok, _ = auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        expires_at=future_time,
    )
    assert ok is True
    assert auth.has_permission("friend_user", auth.PERM_BLOG_TRANSLATE) is True

    # 时间流逝后自动失效
    monkeypatch.setattr(time, "time", lambda: future_time + 10)
    assert auth.has_permission("friend_user", auth.PERM_BLOG_TRANSLATE) is False
    assert auth.get_user_capabilities("friend_user") == []


def test_permission_optimistic_locking_revision():
    # 第一次设置
    auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        daily_limit=10,
    )
    grants = {g["permission"]: g for g in auth.get_user_permission_grants("friend_user")}
    rev = grants[auth.PERM_BLOG_TRANSLATE]["revision"]
    assert rev == 1

    # 使用错误的 expected_revision 更新，应冲突拦截
    ok, err_msg = auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        daily_limit=20,
        expected_revision=999,
    )
    assert ok is False
    assert "已被他人修改" in err_msg

    # 使用正确的 expected_revision 更新，成功且版本号递增
    ok, _ = auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        daily_limit=20,
        expected_revision=rev,
    )
    assert ok is True
    grants_after = {g["permission"]: g for g in auth.get_user_permission_grants("friend_user")}
    assert grants_after[auth.PERM_BLOG_TRANSLATE]["revision"] == 2
    assert grants_after[auth.PERM_BLOG_TRANSLATE]["daily_limit"] == 20


# ================================================================
# 2. 配额原子预留与日界线
# ================================================================

def test_admin_translation_quota_unlimited():
    ok, msg, q = auth.reserve_translation_quota("admin_user", 1)
    assert ok is True
    assert q["unlimited"] is True
    assert q["remaining"] is None


def test_viewer_quota_reservation_and_exhaustion():
    auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        daily_limit=2,
    )

    # 第 1 次
    ok, msg, q = auth.reserve_translation_quota("friend_user", 1)
    assert ok is True
    assert q["used"] == 1
    assert q["remaining"] == 1
    assert "日本时间" in q["reset_at_display"]

    # 第 2 次
    ok, msg, q = auth.reserve_translation_quota("friend_user", 1)
    assert ok is True
    assert q["used"] == 2
    assert q["remaining"] == 0

    # 第 3 次超额
    ok, msg, q = auth.reserve_translation_quota("friend_user", 1)
    assert ok is False
    assert "已用完" in msg
    assert q["used"] == 2
    assert q["remaining"] == 0


def test_delete_user_cascades_permissions_and_quota():
    auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        daily_limit=10,
    )
    auth.reserve_translation_quota("friend_user", 1)
    assert len(auth.get_user_permission_grants("friend_user")) == 1

    # 删除用户
    ok, _ = auth.delete_user("friend_user")
    assert ok is True

    # 授权与用量表级联清除
    conn = auth.get_auth_db()
    g_count = conn.execute("SELECT count(*) FROM user_permission_grants WHERE username = ?", ("friend_user",)).fetchone()[0]
    q_count = conn.execute("SELECT count(*) FROM translation_quota_ledger WHERE username = ?", ("friend_user",)).fetchone()[0]
    assert g_count == 0
    assert q_count == 0


# ================================================================
# 3. HTTP 接口契约 (/api/auth/me 与 /api/users)
# ================================================================

def test_auth_me_returns_capabilities_and_quota(monkeypatch):
    import src.config.config as cfg
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)

    auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        daily_limit=5,
    )

    handler = _DummyHandler(user={"username": "friend_user", "role": "viewer"})
    handle_auth_me(handler)
    assert handler.code == 200
    assert handler.payload["capabilities"] == [auth.PERM_BLOG_TRANSLATE]
    quota = handler.payload["translation_quota"]
    assert quota["daily_limit"] == 5
    assert quota["used"] == 0
    assert quota["remaining"] == 5


def test_users_api_permission_management(monkeypatch):
    import src.config.config as cfg
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)

    # 1. GET /api/users 返回权限与可用能力列表
    admin_handler = _DummyHandler(user={"username": "admin_user", "role": "admin"})
    handle_users_get(admin_handler)
    assert admin_handler.code == 200
    assert "available_permissions" in admin_handler.payload
    assert admin_handler.payload["available_permissions"][0]["permission"] == auth.PERM_BLOG_TRANSLATE

    # 2. 普通用户请求修改权限，应被拦截 (403)
    viewer_handler = _DummyHandler(user={"username": "friend_user", "role": "viewer"})
    handle_users_write(viewer_handler, {
        "action": "set_permissions",
        "username": "friend_user",
        "permission": auth.PERM_BLOG_TRANSLATE,
        "enabled": True,
    })
    assert viewer_handler.code == 403

    # 3. 管理员请求修改权限，成功
    handle_users_write(admin_handler, {
        "action": "set_permissions",
        "username": "friend_user",
        "permission": auth.PERM_BLOG_TRANSLATE,
        "enabled": True,
        "daily_limit": 12,
    })
    assert admin_handler.code == 200
    assert admin_handler.payload["ok"] is True

    # 验证权限已生效
    assert auth.has_permission("friend_user", auth.PERM_BLOG_TRANSLATE) is True
    grants = {g["permission"]: g for g in auth.get_user_permission_grants("friend_user")}
    assert grants[auth.PERM_BLOG_TRANSLATE]["daily_limit"] == 12


# ================================================================
# 4. 博客翻译双层鉴权与安全防护
# ================================================================

def _setup_blog_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY, group_key TEXT, author TEXT, title TEXT,
            body_html TEXT, translation TEXT, content_json TEXT, translation_model TEXT,
            translation_status TEXT, translation_error TEXT,
            translation_request_id TEXT, translation_updated_at TEXT
        );
    """)
    db.execute("""
        INSERT INTO blog_posts (id, group_key, author, title, body_html)
        VALUES (101, 'nogizaka46', '与田祐希', '测试博客', '<p>こんにちは、与田です。</p>');
    """)
    db.commit()
    return db


def test_blog_translate_access_control(monkeypatch):
    import src.config.config as cfg
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)
    blog_db = _setup_blog_db()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: blog_db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: blog_db)

    # 1. 匿名 / 未登录访问 -> guard 拦截
    anon_handler = _DummyHandler()
    archive_handlers.handle_archive(
        anon_handler,
        "blogs/translate",
        lambda need_admin=True, capability=None: False,
        lambda: {"id": 101},
    )
    # guard 拦截未进入处理
    assert anon_handler.payload is None or not anon_handler.payload.get("ok")

    # 2. 未授权 viewer 访问 -> 403
    viewer_user = {"username": "viewer_user", "role": "viewer"}
    viewer_handler = _DummyHandler(user=viewer_user)

    def guard_fn(need_admin=True, capability=None):
        if capability:
            has_p = auth.has_permission(viewer_user, capability)
            if not has_p:
                viewer_handler.send_response(403)
                viewer_handler.payload = {"ok": False, "errors": [f"缺少所需权限: {capability}"]}
            return has_p
        return not need_admin

    archive_handlers.handle_archive(
        viewer_handler,
        "blogs/translate",
        guard_fn,
        lambda: {"id": 101},
    )
    assert viewer_handler.code == 403

    # 3. 未授权普通用户尝试调用 blogs/delete_translation -> 403 拦截
    del_handler = _DummyHandler(user=viewer_user)
    del_handler.command = "POST"

    def del_guard(need_admin=True, capability=None):
        if need_admin:
            del_handler.send_response(403)
            del_handler.payload = {"ok": False, "errors": ["需要管理员权限"]}
            return False
        return True

    archive_handlers.handle_archive(
        del_handler,
        "blogs/delete_translation",
        del_guard,
        lambda: {"id": 101},
    )
    assert del_handler.code == 403


def test_authorized_viewer_can_translate_but_cannot_force(monkeypatch):
    import src.config.config as cfg
    from src import translator
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)
    blog_db = _setup_blog_db()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: blog_db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: blog_db)

    # 给 friend_user 授予权限
    auth.set_user_permission(
        "friend_user",
        auth.PERM_BLOG_TRANSLATE,
        "admin_user",
        enabled=True,
        daily_limit=5,
    )
    friend_user = {"username": "friend_user", "role": "viewer"}

    def guard_fn(need_admin=True, capability=None):
        if capability:
            return auth.has_permission(friend_user, capability)
        return not need_admin

    # 普通授权用户尝试传入 force=True -> 403 拦截
    force_handler = _DummyHandler(user=friend_user)
    force_handler.command = "POST"
    archive_handlers.handle_archive(
        force_handler,
        "blogs/translate",
        guard_fn,
        lambda: {"id": 101, "force": True},
    )
    assert force_handler.code == 403
    assert "仅管理员可执行强制重译" in force_handler.payload["msg"]

    # 普通授权用户正常发起翻译 -> 成功，并原子扣除 1 个配额
    async def fake_translate(*_args, **_kwargs):
        return ([{"type": "text", "jp": "こんにちは", "zh": "你好"}], "gemini-test")

    monkeypatch.setattr(translator, "translate_blog_structured", fake_translate)

    trans_handler = _DummyHandler(user=friend_user)
    trans_handler.command = "POST"
    archive_handlers.handle_archive(
        trans_handler,
        "blogs/translate",
        guard_fn,
        lambda: {"id": 101},
    )
    assert trans_handler.code == 200
    assert trans_handler.payload["ok"] is True
    assert trans_handler.payload["quota"]["used"] == 1
    assert trans_handler.payload["quota"]["remaining"] == 4

    # 再次请求同一篇已完成翻译（无 force） -> 命中缓存直接返回，零扣额！
    cache_handler = _DummyHandler(user=friend_user)
    cache_handler.command = "POST"
    archive_handlers.handle_archive(
        cache_handler,
        "blogs/translate",
        guard_fn,
        lambda: {"id": 101},
    )
    assert cache_handler.code == 200
    assert cache_handler.payload["ok"] is True
    assert cache_handler.payload["quota"]["used"] == 1  # 用量未增加
    assert cache_handler.payload["quota"]["remaining"] == 4


def test_user_in_flight_concurrency_lock(monkeypatch):
    import src.config.config as cfg
    monkeypatch.setattr(cfg, "AUTH_ENABLED", True)
    blog_db = _setup_blog_db()
    blog_db.execute("INSERT INTO blog_posts (id, author, title, body_html) VALUES (102, '与田祐希', '篇2', '<p>内容</p>');")
    blog_db.commit()
    monkeypatch.setattr(archive_common, "get_blog_db", lambda: blog_db)
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: blog_db)

    auth.set_user_permission("friend_user", auth.PERM_BLOG_TRANSLATE, "admin_user", enabled=True, daily_limit=10)
    friend_user = {"username": "friend_user", "role": "viewer"}

    def guard_fn(need_admin=True, capability=None):
        return True

    # 模拟该用户已有在途任务
    _user_in_flight_translations.add("friend_user")

    flight_handler = _DummyHandler(user=friend_user)
    flight_handler.command = "POST"
    archive_handlers.handle_archive(
        flight_handler,
        "blogs/translate",
        guard_fn,
        lambda: {"id": 102},
    )
    assert flight_handler.code == 429
    assert "已有正在进行的翻译任务" in flight_handler.payload["msg"]
