"""验证账号系统：密码哈希、用户库、会话、限流、路由守卫

运行: python tests/test_auth.py
"""
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _http(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    """返回 (status, json_or_bytes, headers)。不自动跟随重定向。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=10) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw), dict(resp.headers)
            except ValueError:
                return resp.status, raw, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except ValueError:
            return e.code, raw, dict(e.headers)


def main() -> None:
    import config.config as cfg
    from src import auth

    tmpdir = Path(tempfile.mkdtemp(prefix="auth_test_"))
    orig_auth_db = auth.AUTH_DB_PATH
    auth.AUTH_DB_PATH = tmpdir / "auth.db"
    auth._auth_conn = None
    auth._sessions.clear()
    auth._sessions_loaded_from_db = False
    orig_flags = (cfg.AUTH_ENABLED, cfg.AUTH_ARCHIVE_PUBLIC, cfg.AUTH_SESSION_HOURS)

    try:
        # ── Test 1: 密码哈希 ─────────────────────────────
        print("=== Test 1: 密码哈希 ===")
        rec = auth.hash_password("correct horse battery")
        assert rec["algo"] == "scrypt" and len(rec["salt"]) == 32
        assert "correct horse battery" not in json.dumps(rec), "记录里不得含明文"
        assert auth.verify_password("correct horse battery", rec)
        assert not auth.verify_password("wrong password", rec)
        assert not auth.verify_password("", rec)
        assert not auth.verify_password("x", {}), "损坏记录应返回 False 而非抛异常"
        assert not auth.verify_password("x", {"algo": "md5", "salt": "aa", "hash": "bb"})
        rec2 = auth.hash_password("correct horse battery")
        assert rec2["salt"] != rec["salt"] and rec2["hash"] != rec["hash"], "同密码应有不同盐"
        print("✅ Test 1 通过\n")

        # ── Test 2: 用户库 CRUD ──────────────────────────
        print("=== Test 2: 用户 CRUD ===")
        ok, msg = auth.add_user("admin1", "adminpass123", "admin")
        assert ok, msg
        assert auth.has_users()
        ok, msg = auth.add_user("admin1", "other12345", "admin")
        assert not ok and "已存在" in msg
        ok, msg = auth.add_user("weak", "123", "admin")
        assert not ok and "至少" in msg, "弱密码应被拒"
        ok, msg = auth.add_user("bad name!", "goodpass123", "admin")
        assert not ok, "非法用户名应被拒"
        ok, msg = auth.add_user("v1", "viewerpass123", "root")
        assert not ok and "角色" in msg, "未知角色应被拒"
        ok, msg = auth.add_user("v1", "viewerpass123", "viewer")
        assert ok, msg

        # 最后一个 admin 保护
        ok, msg = auth.delete_user("admin1")
        assert not ok and "最后一个 admin" in msg
        ok, msg = auth.set_role("admin1", "viewer")
        assert not ok, "不能降级最后一个 admin"
        assert auth.add_user("admin2", "adminpass456", "admin")[0]
        assert auth.delete_user("admin2")[0], "有两个 admin 时可删除其一"

        # 数据库文件里不含明文密码
        raw_bytes = auth.AUTH_DB_PATH.read_bytes()
        assert b"adminpass123" not in raw_bytes and b"viewerpass123" not in raw_bytes

        # 验证 ensure_initial_admin
        assert not auth.ensure_initial_admin()[0], "已存在用户时不应重复创建初始管理员"
        print("✅ Test 2 通过\n")

        # ── Test 3: 认证与会话 ──────────────────────────
        print("=== Test 3: 认证与会话 ===")
        assert auth.authenticate("admin1", "adminpass123")["role"] == "admin"
        assert auth.authenticate("admin1", "wrong") is None
        assert auth.authenticate("nobody", "whatever") is None
        assert auth.authenticate("", "") is None

        tok = auth.create_session("admin1", "admin", 3600)
        sess = auth.get_session(tok)
        assert sess and sess["username"] == "admin1" and sess["role"] == "admin"
        assert auth.get_session("bogus-token") is None
        assert auth.get_session("") is None
        expired = auth.create_session("admin1", "admin", -1)   # 立即过期
        assert auth.get_session(expired) is None, "过期会话不应可用"
        # 会话持久化与跨重启恢复测试
        tok_pers = auth.create_session("admin1", "admin", 7200)
        # 模拟内存清空（主程序/容器重启）
        with auth._lock:
            auth._sessions.clear()
            auth._sessions_loaded_from_db = False
        sess_recovered = auth.get_session(tok_pers)
        assert sess_recovered and sess_recovered["username"] == "admin1", "重启后应能从 SQLite 恢复未过期会话"
        assert sess_recovered["role"] == "admin"
        auth.destroy_session(tok_pers)
        with auth._lock:
            auth._sessions.clear()
            auth._sessions_loaded_from_db = False
        assert auth.get_session(tok_pers) is None, "销毁后 SQLite 中也应被删除"

        # 改密使该用户所有会话失效（同时清理内存与 SQLite）
        t1, t2 = auth.create_session("v1", "viewer", 3600), auth.create_session("v1", "viewer", 3600)
        assert auth.set_password("v1", "newviewerpass")[0]
        with auth._lock:
            auth._sessions.clear()
            auth._sessions_loaded_from_db = False
        assert auth.get_session(t1) is None and auth.get_session(t2) is None, "改密应踢掉旧会话（含 SQLite）"
        assert auth.authenticate("v1", "newviewerpass") is not None
        assert auth.authenticate("v1", "viewerpass123") is None, "旧密码应失效"
        print("✅ Test 3 通过\n")

        # ── Test 4: 登录限流 ────────────────────────────
        print("=== Test 4: 登录限流 ===")
        ip = "10.1.2.3"
        auth.clear_failures(ip)
        assert auth.is_locked_out(ip) == 0
        for _ in range(auth.MAX_FAILURES - 1):
            auth.record_failure(ip)
        assert auth.is_locked_out(ip) == 0, f"少于 {auth.MAX_FAILURES} 次不应锁定"
        auth.record_failure(ip)
        assert auth.is_locked_out(ip) > 0, "达到阈值应锁定"
        auth.clear_failures(ip)
        assert auth.is_locked_out(ip) == 0, "成功登录应清除锁定"
        print("✅ Test 4 通过\n")

        # ── Test 5: 路由守卫 ────────────────────────────
        print("=== Test 5: 路由守卫 ===")
        from src import webui
        import os
        os.environ.pop("WEB_ADMIN_TOKEN", None)
        cfg.AUTH_ENABLED = True
        cfg.AUTH_ARCHIVE_PUBLIC = False
        cfg.AUTH_SESSION_HOURS = 12

        server = webui.start_webui(host="127.0.0.1", port=0)
        assert server is not None
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            # 未登录：管理页 → 302 到 /login，API → 401，归档页 → 302
            code, _, h = _http("GET", base + "/")
            assert code == 302 and "/login" in h.get("Location", ""), f"管理页应重定向: {code}"
            code, body, _ = _http("GET", base + "/api/config")
            assert code == 401 and not body["ok"], f"管理 API 应 401: {code}"
            code, _, _ = _http("GET", base + "/archive")
            assert code == 302, "归档页未登录应重定向"
            code, _, _ = _http("GET", base + "/api/archive/members")
            assert code == 401, "归档 API 未登录应 401"
            # 主题静态资源对未登录也开放（登录页要用），且限定白名单
            code, body, h = _http("GET", base + "/static/theme.css")
            assert code == 200 and b"--accent" in body, "主题 CSS 应可匿名获取"
            assert "text/css" in h.get("Content-Type", "")
            code, body, _ = _http("GET", base + "/static/theme.js")
            assert code == 200 and b"data-theme" in body
            code, body, _ = _http("GET", base + "/static/../webui.py")
            assert code in (404, 400), f"静态路由不应接受任意路径: {code}"

            # 登录页与身份接口始终可访问
            code, body, _ = _http("GET", base + "/login")
            assert code == 200 and b"login" in body.lower()
            code, body, _ = _http("GET", base + "/api/auth/me")
            assert code == 200 and body["auth_enabled"] and body["user"] is None

            # 登录失败
            code, body, _ = _http("POST", base + "/api/auth/login",
                                  {"username": "admin1", "password": "nope"})
            assert code == 401 and "错误" in body["errors"][0]

            # admin 登录 → 拿 cookie
            code, body, h = _http("POST", base + "/api/auth/login",
                                  {"username": "admin1", "password": "adminpass123"})
            assert code == 200 and body["user"]["role"] == "admin", f"登录应成功: {body}"
            set_cookie = h.get("Set-Cookie", "")
            assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie, \
                f"cookie 应带 HttpOnly/SameSite: {set_cookie}"
            admin_cookie = set_cookie.split(";")[0]

            ck = {"Cookie": admin_cookie}
            code, body, _ = _http("GET", base + "/api/config", headers=ck)
            assert code == 200 and body["ok"], "admin 应可访问管理 API"
            code, _, _ = _http("GET", base + "/", headers=ck)
            assert code == 200, "admin 应可访问管理页"
            code, body, _ = _http("GET", base + "/api/archive/members", headers=ck)
            assert code == 200, "admin 应可访问归档"
            code, body, _ = _http("GET", base + "/api/auth/me", headers=ck)
            assert body["user"]["username"] == "admin1"

            # viewer 登录 → 只能归档
            code, body, h = _http("POST", base + "/api/auth/login",
                                  {"username": "v1", "password": "newviewerpass"})
            assert code == 200 and body["user"]["role"] == "viewer"
            vck = {"Cookie": h.get("Set-Cookie", "").split(";")[0]}
            code, body, _ = _http("GET", base + "/api/archive/members", headers=vck)
            assert code == 200, "viewer 应可访问归档 API"
            code, _, _ = _http("GET", base + "/archive", headers=vck)
            assert code == 200, "viewer 应可访问归档页"
            code, body, _ = _http("GET", base + "/api/config", headers=vck)
            assert code == 403, f"viewer 不应访问管理 API: {code}"
            code, _, _ = _http("GET", base + "/", headers=vck)
            assert code == 403, "viewer 不应访问管理页"
            code, body, _ = _http("POST", base + "/api/restart", headers=vck)
            assert code == 403, "viewer 不应能重启"
            code, body, _ = _http("PUT", base + "/api/config", body={"x": 1}, headers=vck)
            assert code == 403, "viewer 不应能改配置"

            # 用户管理接口：admin 可用，viewer 一律 403
            code, body, _ = _http("GET", base + "/api/users", headers=ck)
            assert code == 200 and any(u["username"] == "admin1" and u["is_me"]
                                       for u in body["users"]), f"用户列表: {body}"
            assert all("password" not in json.dumps(u) for u in body["users"]), "列表不得含密码字段"
            code, body, _ = _http("GET", base + "/api/users", headers=vck)
            assert code == 403, "viewer 不应看到用户列表"
            code, body, _ = _http("POST", base + "/api/users", headers=vck,
                                  body={"action": "add", "username": "hacker",
                                        "password": "hackerpass1", "role": "admin"})
            assert code == 403, "viewer 不应能创建用户"

            # 通过接口新增 viewer → 可登录且只有归档权限
            code, body, _ = _http("POST", base + "/api/users", headers=ck,
                                  body={"action": "add", "username": "guest1",
                                        "password": "guestpass123", "role": "viewer"})
            assert code == 200 and body["ok"], f"新增用户应成功: {body}"
            code, body, h = _http("POST", base + "/api/auth/login",
                                  {"username": "guest1", "password": "guestpass123"})
            assert code == 200 and body["user"]["role"] == "viewer"
            gck = {"Cookie": h.get("Set-Cookie", "").split(";")[0]}
            assert _http("GET", base + "/api/archive/members", headers=gck)[0] == 200
            assert _http("GET", base + "/api/config", headers=gck)[0] == 403

            # 改密码 → 旧会话立即失效
            code, body, _ = _http("POST", base + "/api/users", headers=ck,
                                  body={"action": "passwd", "username": "guest1",
                                        "password": "newguestpass1"})
            assert code == 200 and body["ok"]
            assert _http("GET", base + "/api/archive/members", headers=gck)[0] == 401, \
                "改密后旧会话应失效"

            # 防误锁：不能删自己、不能删/降最后一个 admin、弱密码被拒
            code, body, _ = _http("POST", base + "/api/users", headers=ck,
                                  body={"action": "delete", "username": "admin1"})
            assert code == 400 and "当前登录" in body["errors"][0], f"不应能删自己: {body}"
            code, body, _ = _http("POST", base + "/api/users", headers=ck,
                                  body={"action": "role", "username": "admin1", "role": "viewer"})
            assert code == 400, "不应能降级最后一个 admin"
            code, body, _ = _http("POST", base + "/api/users", headers=ck,
                                  body={"action": "add", "username": "weakling", "password": "123"})
            assert code == 400 and "至少" in body["errors"][0]
            code, body, _ = _http("POST", base + "/api/users", headers=ck,
                                  body={"action": "bogus", "username": "x"})
            assert code == 400, "未知操作应 400"

            # 删除 viewer
            code, body, _ = _http("POST", base + "/api/users", headers=ck,
                                  body={"action": "delete", "username": "guest1"})
            assert code == 200 and body["ok"], f"删除应成功: {body}"

            # CSRF：跨站 Origin 的写请求被拒
            code, body, _ = _http("POST", base + "/api/restart",
                                  headers={**ck, "Origin": "https://evil.example.com"})
            assert code == 403 and "跨站" in body["errors"][0], f"跨站 Origin 应 403: {code}"
            code, body, _ = _http("PUT", base + "/api/config", body={"x": 1},
                                  headers={**ck, "Origin": "http://127.0.0.1:1"})
            assert code != 403 or "跨站" not in str(body), "同源 Origin 不应被拦"

            # 登出后 cookie 失效，且指示浏览器清理已缓存的私密媒体
            code, _, h = _http("POST", base + "/api/auth/logout", headers=ck)
            assert code == 200
            csd = h.get("Clear-Site-Data", "")
            assert "cache" in csd and "cookies" in csd, f"登出应清缓存和 cookie: {csd!r}"
            assert "storage" not in csd, \
                f"不得清 storage —— 会把主题偏好一起清掉: {csd!r}"
            code, _, _ = _http("GET", base + "/api/config", headers=ck)
            assert code == 401, "登出后应 401"
            code, _, _ = _http("GET", base + "/api/archive/members", headers=ck)
            assert code == 401, "登出后归档 API 也应 401"

            # archive_public：归档免登录，管理端仍受保护
            cfg.AUTH_ARCHIVE_PUBLIC = True
            code, _, _ = _http("GET", base + "/archive")
            assert code == 200, "公开模式下归档页应免登录"
            code, _, _ = _http("GET", base + "/api/archive/members")
            assert code == 200, "公开模式下归档 API 应免登录"
            code, _, _ = _http("GET", base + "/api/config")
            assert code == 401, "公开模式下管理端仍须登录"
            cfg.AUTH_ARCHIVE_PUBLIC = False

            # 无用户时：503 并提示创建账号
            saved = auth.load_users()
            auth.save_users({})
            code, body, _ = _http("GET", base + "/api/config")
            assert code == 503 and "manage_users" in body["errors"][0], f"无用户应 503: {body}"
            auth.save_users(saved)

            # 账号系统关闭 → 恢复旧行为（无 token 时全放行）
            cfg.AUTH_ENABLED = False
            code, _, _ = _http("GET", base + "/api/config")
            assert code == 200, "关闭账号系统后应放行"
            code, _, _ = _http("GET", base + "/api/archive/members")
            assert code == 200
        finally:
            server.shutdown()
            server.server_close()
        print("✅ Test 5 通过\n")

    finally:
        auth.AUTH_DB_PATH = orig_auth_db
        auth._auth_conn = None
        cfg.AUTH_ENABLED, cfg.AUTH_ARCHIVE_PUBLIC, cfg.AUTH_SESSION_HOURS = orig_flags

    print("=" * 50)
    print("🎉 全部测试通过！账号系统工作正常")


if __name__ == "__main__":
    main()
