"""验证网页管理端：序列化往返、校验、HTTP 端点

运行: python tests/test_webui.py
"""
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SAMPLE = {
    "channels": {"napcat": True, "tg": True, "qq_official": False},
    "napcat_api": "http://127.0.0.1:3000/send_group_msg",
    "web_admin": {"enabled": True, "host": "127.0.0.1", "port": 8787},
    "accounts": {
        "nogizaka_main": {"group": "nogizaka46", "auth": "mobile"},
        "hinata_shared": {"group": "hinatazaka46"},
    },
    "monitor": [
        {"id": "55", "name": "冨里奈央", "account": "nogizaka_main", "groups": [533072575], "tg": "-100123"},
        {"id": "34", "name": "金村美玖", "account": "hinata_shared", "groups": [752269366]},
    ],
    "day_interval": [120, 180],
    "night_interval": [1500, 1800],
    "sleep_hours": [2, 7],
    "alert_cooldown": 3600,
    "translate": True,
    "gemini_min_interval": 7.0,
}


def _http(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    """返回 (status_code, parsed_json)。4xx/5xx 也解析 body。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, json.loads(raw) if raw else {}


def main() -> None:
    import json5

    import config.config as cfg
    from src import webui

    # 本套件测管理端功能本身；鉴权矩阵由 test_auth 覆盖，这里显式关掉
    # 账号系统，避免测试结果随用户的 config.json 变化
    orig_auth = cfg.AUTH_ENABLED
    cfg.AUTH_ENABLED = False

    # ── Test 1: 序列化往返 ────────────────────────────
    print("=== Test 1: 序列化往返 ===")
    text = webui.serialize_config(SAMPLE)
    assert text.startswith("{") and text.endswith("}\n")
    assert "// ── 推送通道 ──" in text, "应包含分区注释"
    reparsed = json5.loads(text)
    assert reparsed == SAMPLE, "序列化→解析后应与原对象一致"
    # 真实 config.json 也应能无损往返
    with open(webui.CONFIG_PATH, "r", encoding="utf-8") as f:
        real = json5.load(f)
    assert json5.loads(webui.serialize_config(real)) == real, "真实 config.json 往返不一致"
    print("✅ Test 1 通过\n")

    # ── Test 2: 校验 ─────────────────────────────────
    print("=== Test 2: validate_config ===")
    assert webui.validate_config(SAMPLE) == [], "合法配置不应报错"

    bad_ref = json.loads(json.dumps(SAMPLE))
    bad_ref["monitor"][0]["account"] = "ghost_account"
    errs = webui.validate_config(bad_ref)
    assert any("未定义的账号" in e for e in errs), f"应检出未定义账号引用: {errs}"

    dup = json.loads(json.dumps(SAMPLE))
    dup["monitor"].append(dict(dup["monitor"][0]))
    errs = webui.validate_config(dup)
    assert any("重复" in e for e in errs), f"应检出重复成员: {errs}"

    bad_schema = json.loads(json.dumps(SAMPLE))
    del bad_schema["monitor"][0]["name"]
    errs = webui.validate_config(bad_schema)
    assert errs and "结构校验失败" in errs[0], f"应检出 schema 错误: {errs}"

    empty_name = json.loads(json.dumps(SAMPLE))
    empty_name["monitor"][0]["name"] = "  "
    empty_name["monitor"][1]["id"] = ""
    errs = webui.validate_config(empty_name)
    assert any("name 为空" in e for e in errs) and any("id 为空" in e for e in errs), \
        f"应检出空 id/name: {errs}"

    with_bots = json.loads(json.dumps(SAMPLE))
    with_bots["qq_official_bots"] = [
        {"name": "qq_official_bot1", "app_id": "102000001", "target_openid": "ABC123"},
        {"name": "push_bot", "app_id": "102000002"},
    ]
    assert webui.validate_config(with_bots) == [], "合法的官方 Bot 声明不应报错"
    dup_bot = json.loads(json.dumps(with_bots))
    dup_bot["qq_official_bots"][1]["name"] = "qq_official_bot1"
    errs = webui.validate_config(dup_bot)
    assert any("Bot 名称重复" in e for e in errs), f"应检出重复 Bot 名: {errs}"
    bad_bot = json.loads(json.dumps(with_bots))
    bad_bot["qq_official_bots"][0]["name"] = "Bad Name"
    errs = webui.validate_config(bad_bot)
    assert errs and "结构校验失败" in errs[0], f"Bot 名不合法应被 schema 拒绝: {errs}"
    print("✅ Test 2 通过\n")

    # ── Test 2.5: .env 写入 ──────────────────────────
    print("=== Test 2.5: 凭证键校验 + .env 更新 ===")
    assert webui.validate_secret_values({"HINATA_SHARED_TOKEN": "eyJx"}) == []
    assert webui.validate_secret_values({"GEMINI_API_KEY": "AIza1"}) == []
    for bad_vals in (
        {"WEB_ADMIN_TOKEN": "x"},          # 明确禁止
        {"RANDOM_KEY": "x"},               # 不在白名单
        {"lowercase_token": "x"},          # 小写
        {"HINATA_SHARED_TOKEN": "a\nb"},   # 换行注入
        {"HINATA_SHARED_TOKEN": "  "},     # 空值
        {},                                # 无键
    ):
        assert webui.validate_secret_values(bad_vals), f"应拒绝: {bad_vals}"

    env_file = Path(tempfile.mkdtemp(prefix="webui_env_")) / ".env"
    env_file.write_text("# 注释保留\nFOO_TOKEN='old'\nGEMINI_API_KEY=\n", encoding="utf-8")
    webui.update_env_file(
        {"FOO_TOKEN": "new", "GEMINI_API_KEY": "AIza9", "BAR_COOKIE": "session=a; b=c"},
        path=env_file,
    )
    text = env_file.read_text(encoding="utf-8")
    assert "# 注释保留" in text, "注释应保留"
    assert "FOO_TOKEN='new'" in text and "old" not in text, "已有键应原地替换"
    assert "GEMINI_API_KEY='AIza9'" in text
    assert "BAR_COOKIE='session=a; b=c'" in text, "新键应追加且带引号"
    from dotenv import dotenv_values
    parsed = dotenv_values(env_file)
    assert parsed["BAR_COOKIE"] == "session=a; b=c", "dotenv 应能解析写入的值"

    # 值含单引号 → 走双引号转义路径，dotenv 仍能还原
    webui.update_env_file({"BAZ_TOKEN": "it's a 'quoted' value"}, path=env_file)
    assert dotenv_values(env_file)["BAZ_TOKEN"] == "it's a 'quoted' value", "单引号值应经双引号转义往返"
    print("✅ Test 2.5 通过\n")

    # ── Test 2.6: 内存日志环 + 文件尾读取 ────────────
    print("=== Test 2.6: 日志查看 ===")
    from src.logger import get_recent, log_all
    _, seq0 = get_recent()
    log_all("webui 日志测试普通行")
    log_all("webui 日志测试错误行", is_error=True)
    log_all("webui 日志测试调试行", is_debug=True)
    entries, seq = get_recent(seq0)
    assert [e["level"] for e in entries] == ["INFO", "ERROR", "DEBUG"], f"级别应齐全: {entries}"
    assert seq == seq0 + 3 and entries[0]["text"] == "webui 日志测试普通行"
    inc, _ = get_recent(seq - 1)
    assert len(inc) == 1 and inc[0]["level"] == "DEBUG", "增量拉取应只返回新条目"

    tail_src = Path(tempfile.mkdtemp(prefix="webui_log_")) / "t.log"
    tail_src.write_text("\n".join(f"line{i}" for i in range(50)) + "\n", encoding="utf-8")
    assert webui._tail_file(tail_src, 3) == ["line47", "line48", "line49"]
    assert webui._tail_file(tail_src / "missing", 3) == []
    print("✅ Test 2.6 通过\n")

    # ── Test 2.7: health 快照 ────────────────────────
    print("=== Test 2.7: health.snapshot ===")
    from src import health
    tracker = health.get_tracker()
    tracker.record_channel("napcat", True)
    tracker.record_channel("napcat", False, "HTTP 500")
    tracker.record_token("nogizaka_main", 1800.0)
    tracker.record_member_fetch("测试成员", False, health.ErrorTier.PERSISTENT, "401")
    tracker.record_next_cycle(1234567890.0, "☀️ 日间巡查")
    snap = tracker.snapshot()
    assert snap["channels"]["napcat"]["success"] == 1 and snap["channels"]["napcat"]["total"] == 2
    assert snap["channels"]["napcat"]["last_error"] == "HTTP 500"
    assert snap["tokens"]["nogizaka_main"]["remaining"] == 1800.0
    assert snap["members"][0]["name"] == "测试成员" and not snap["members"][0]["fetch_ok"]
    assert snap["next_cycle"] == {"at_epoch": 1234567890.0, "tag": "☀️ 日间巡查"}
    assert any(e["tier"] == "PERSISTENT" for e in snap["errors"]), f"应有 PERSISTENT 错误: {snap['errors']}"
    import json as _json
    _json.dumps(snap)   # 必须可 JSON 序列化
    print("✅ Test 2.7 通过\n")

    # ── Test 2.8: 成员目录响应解析 ───────────────────
    print("=== Test 2.8: member_directory.normalize_groups ===")
    from src.member_directory import normalize_groups
    assert normalize_groups([{"id": 1}]) == [{"id": 1}], "裸数组应原样返回"
    assert normalize_groups({"groups": [{"id": 2}]}) == [{"id": 2}]
    assert normalize_groups({"items": [{"id": 3}]}) == [{"id": 3}]
    assert normalize_groups({"data": [{"id": 4}]}) == [{"id": 4}]
    assert normalize_groups({"foo": 1}) is None and normalize_groups("x") is None
    print("✅ Test 2.8 通过\n")

    # ── Test 3: HTTP 端点 ────────────────────────────
    print("=== Test 3: HTTP 端点 ===")
    tmpdir = tempfile.mkdtemp(prefix="webui_test_")
    tmp_config = Path(tmpdir) / "config.json"
    tmp_config.write_text(webui.serialize_config(SAMPLE), encoding="utf-8")

    orig_config_path = webui.CONFIG_PATH
    orig_env_path = webui.ENV_PATH
    orig_trigger = webui._trigger_reload
    orig_rotate = webui._rotate_account_creds
    reload_calls = []
    rotate_calls = []
    restart_calls = []
    webui.CONFIG_PATH = tmp_config
    webui.ENV_PATH = Path(tmpdir) / ".env"
    webui._trigger_reload = lambda: (reload_calls.append(1), True)[1]
    webui._rotate_account_creds = lambda acc: rotate_calls.append(acc)
    os.environ.pop("WEB_ADMIN_TOKEN", None)

    server = webui.start_webui(host="127.0.0.1", port=0)
    assert server is not None, "服务应能在临时端口启动"
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        # 首页
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
            assert resp.status == 200 and "配置管理" in html

        # GET /api/config
        code, data = _http("GET", base + "/api/config")
        assert code == 200 and data["ok"] and data["config"] == SAMPLE
        assert "nogizaka_main" in data["cred_status"]
        assert [b["name"] for b in data["qq_bot_status"]] == ["BOT1", "BOT2"], "未声明时应报告 .env 编号槽位状态"

        # PUT 声明官方 Bot → 状态改为按声明报告
        cfg_bots = json.loads(json.dumps(SAMPLE))
        cfg_bots["qq_official_bots"] = [{"name": "my_push_bot", "app_id": "102000001", "target_openid": "XYZ"}]
        code, data = _http("PUT", base + "/api/config", body=cfg_bots)
        assert code == 200 and data["ok"], f"声明官方 Bot 的 PUT 应成功: {data}"
        st = data["qq_bot_status"]
        assert [b["name"] for b in st] == ["my_push_bot"] and st[0]["declared"], f"应按声明报告: {st}"
        assert st[0]["secret_env"] == "MY_PUSH_BOT_CLIENT_SECRET"
        assert st[0]["app_id"] and st[0]["target_openid"] and not st[0]["client_secret"]

        # PUT 合法配置：新增一个成员
        new_cfg = json.loads(json.dumps(SAMPLE))
        new_cfg["monitor"].append({"id": "99", "name": "新成员", "account": "hinata_shared", "groups": []})
        code, data = _http("PUT", base + "/api/config", body=new_cfg)
        assert code == 200 and data["ok"] and data["reloaded"], f"PUT 应成功: {data}"
        assert reload_calls, "保存后应触发热重载"
        assert json5.loads(tmp_config.read_text(encoding="utf-8")) == new_cfg, "文件应已写入新配置"

        # PUT 非法配置：引用未定义账号 → 400 且文件不变
        bad = json.loads(json.dumps(new_cfg))
        bad["monitor"][0]["account"] = "ghost"
        code, data = _http("PUT", base + "/api/config", body=bad)
        assert code == 400 and not data["ok"], f"非法配置应 400: {code} {data}"
        assert json5.loads(tmp_config.read_text(encoding="utf-8")) == new_cfg, "校验失败不应写文件"

        # POST /api/reload
        code, data = _http("POST", base + "/api/reload")
        assert code == 200 and data["ok"]

        # POST /api/secrets：写账号凭证并轮换
        code, data = _http("POST", base + "/api/secrets", body={
            "values": {"HINATA_SHARED_TOKEN": "eyJtest", "HINATA_SHARED_COOKIE": "session=abc; x=y"},
            "account": "hinata_shared",
        })
        assert code == 200 and data["ok"], f"写凭证应成功: {data}"
        assert data["updated"] == ["HINATA_SHARED_COOKIE", "HINATA_SHARED_TOKEN"]
        assert rotate_calls == ["hinata_shared"], "带 account 应触发凭证轮换"
        assert os.environ["HINATA_SHARED_TOKEN"] == "eyJtest", "应同步进程环境变量"
        env_text = webui.ENV_PATH.read_text(encoding="utf-8")
        assert "HINATA_SHARED_COOKIE='session=abc; x=y'" in env_text, ".env 应写入带引号的值"
        assert data["cred_status"]["hinata_shared"]["ok"], "写入后凭证状态应变为已配置"

        # POST /api/secrets：非法键 / 未知账号 → 400 且不写文件
        code, data = _http("POST", base + "/api/secrets", body={"values": {"WEB_ADMIN_TOKEN": "x"}})
        assert code == 400 and not data["ok"], "WEB_ADMIN_TOKEN 应被拒绝"
        code, data = _http("POST", base + "/api/secrets", body={
            "values": {"GHOST_TOKEN": "x"}, "account": "ghost"})
        assert code == 400 and any("未知账号" in e for e in data["errors"]), f"未知账号应 400: {data}"
        assert "GHOST_TOKEN" not in webui.ENV_PATH.read_text(encoding="utf-8"), "校验失败不应写 .env"

        # GET /api/status：健康快照可通过 HTTP 获取
        code, data = _http("GET", base + "/api/status")
        assert code == 200 and data["ok"], f"状态接口应可用: {code}"
        assert data["channels"]["napcat"]["total"] == 2, "应包含 Test 2.7 记录的通道数据"
        assert not data["embedded"], "独立模式 embedded 应为 false"

        # GET /api/members：未知账号 → 400（凭证/网络路径不在单测覆盖）
        code, data = _http("GET", base + "/api/members?account=ghost")
        assert code == 400 and any("未知账号" in e for e in data["errors"]), f"未知账号应 400: {data}"

        # POST /api/poll：独立模式 → 400；注入回调后 → 触发
        code, data = _http("POST", base + "/api/poll")
        assert code == 400, "无巡查回调时应 400"
        poll_calls = []
        webui._on_poll_cb = lambda: poll_calls.append(1)
        code, data = _http("POST", base + "/api/poll")
        assert code == 200 and data["ok"] and poll_calls == [1], f"应触发巡查回调: {data}"
        webui._on_poll_cb = None

        # POST /api/test_push：独立模式 → 400；注入回调后 → 校验参数并转发
        code, data = _http("POST", base + "/api/test_push", body={"channel": "tg", "target": "-100"})
        assert code == 400, "无测试推送回调时应 400"
        push_calls = []
        webui._on_test_push_cb = lambda ch, tgt, txt: (push_calls.append((ch, tgt)), (True, ""))[1]
        code, data = _http("POST", base + "/api/test_push", body={"channel": "tg", "target": "-100123"})
        assert code == 200 and data["ok"] and push_calls == [("tg", "-100123")], f"应转发到回调: {data}"
        code, data = _http("POST", base + "/api/test_push", body={"channel": "email", "target": "x"})
        assert code == 400, "非法通道应 400"
        code, data = _http("POST", base + "/api/test_push", body={"channel": "tg", "target": ""})
        assert code == 400, "缺目标应 400"
        code, data = _http("POST", base + "/api/test_push", body={"channel": "napcat", "target": "abc"})
        assert code == 400, "NapCat 非数字群号应 400"
        webui._on_test_push_cb = lambda ch, tgt, txt: (False, "模拟失败")
        code, data = _http("POST", base + "/api/test_push", body={"channel": "tg", "target": "-100123"})
        assert code == 502 and "模拟失败" in data["errors"][0], f"回调失败应 502: {data}"
        webui._on_test_push_cb = None

        # openid 捕获：解析事件体、独立模式拒绝、参数校验、回调转发
        from src import qq_openid
        evt = {"t": "C2C_MESSAGE_CREATE", "d": {
            "author": {"user_openid": "ABC123OPENID", "username": "someone"},
            "content": "hi", "nested": [{"group_openid": "GRP999"}]}}
        hits = dict(qq_openid.find_openid_values(evt))
        assert hits.get("d.author.user_openid") == "ABC123OPENID", f"应提取 openid: {hits}"
        assert "d.nested[0].group_openid" in hits, "嵌套结构也应遍历到"
        assert qq_openid.find_openid_values({"a": 1, "b": "x"}) == []

        code, data = _http("GET", base + "/api/qq_openid/status")
        assert code == 200 and data["state"] == "idle" and not data["available"], \
            f"独立模式应报告不可用: {data}"
        code, data = _http("POST", base + "/api/qq_openid/start",
                           body={"app_id": "1", "client_secret": "s"})
        assert code == 400 and "独立模式" in data["errors"][0], f"独立模式应 400: {data}"

        openid_calls = []
        webui._on_openid_cb = lambda act, aid, sec: (
            openid_calls.append((act, aid, bool(sec))), (True, "ok"))[1]
        code, data = _http("POST", base + "/api/qq_openid/start",
                           body={"app_id": "102000001", "client_secret": "secret1"})
        assert code == 200 and data["ok"] and openid_calls == [("start", "102000001", True)], \
            f"应转发到回调: {data} {openid_calls}"
        code, data = _http("POST", base + "/api/qq_openid/start", body={"client_secret": "s"})
        assert code == 400 and "App ID" in data["errors"][0], "缺 app_id 应 400"
        code, data = _http("POST", base + "/api/qq_openid/start",
                           body={"app_id": "1", "bot_name": "no_such_bot"})
        assert code == 400 and "Secret" in data["errors"][0], "无 secret 且 .env 也没有时应 400"
        code, data = _http("POST", base + "/api/qq_openid/stop")
        assert code == 200 and openid_calls[-1][0] == "stop"
        webui._on_openid_cb = None

        # POST /api/restart：独立模式（无回调）→ 400；注入回调后 → 触发重启
        code, data = _http("POST", base + "/api/restart")
        assert code == 400 and not data["ok"], "无重启回调时应 400"
        webui._on_restart_cb = lambda: restart_calls.append(1)
        code, data = _http("POST", base + "/api/restart")
        assert code == 200 and data["ok"] and restart_calls == [1], f"应触发重启回调: {data}"
        code, data = _http("GET", base + "/api/config")
        assert data["can_restart"], "注入回调后 can_restart 应为 true"
        webui._on_restart_cb = None

        # GET /api/logs：实时环 / 文件尾 / 非法源
        code, data = _http("GET", base + "/api/logs?source=live")
        assert code == 200 and data["ok"] and data["seq"] >= 3, f"实时日志应可用: {code}"
        assert any("webui 日志测试错误行" in e["text"] for e in data["entries"]), "应包含刚写入的日志"
        after = data["seq"]
        code, data = _http("GET", base + f"/api/logs?source=live&after={after}")
        assert code == 200 and data["entries"] == [], "after=最大 seq 时应返回空增量"
        code, data = _http("GET", base + "/api/logs?source=error&tail=5")
        assert code == 200 and data["ok"] and isinstance(data["lines"], list), "错误日志文件源应可用"
        code, data = _http("GET", base + "/api/logs?source=bogus")
        assert code == 400, "非法日志源应 400"

        # 配置历史：每次 PUT 前都应有快照；可恢复；非法名被拒
        code, data = _http("GET", base + "/api/config/history")
        assert code == 200 and len(data["history"]) >= 2, f"两次成功 PUT 应产生 ≥2 份快照: {data}"
        newest = data["history"][0]["name"]
        code, data = _http("GET", base + "/api/config/history?name=" + newest)
        assert code == 200 and data["ok"] and json5.loads(data["content"]) == cfg_bots, \
            "查看快照应返回可解析的原始内容"
        code, data = _http("GET", base + "/api/config/history?name=../evil.json")
        assert code == 400, "查看接口也应拒绝路径穿越名"
        code, data = _http("POST", base + "/api/config/restore", body={"name": newest})
        assert code == 200 and data["ok"] and data["restored"] == newest, f"恢复应成功: {data}"
        restored = json5.loads(tmp_config.read_text(encoding="utf-8"))
        assert restored == cfg_bots, "最新快照应是 cfg_bots 版本（第二次 PUT 前的状态）"
        code, data = _http("POST", base + "/api/config/restore", body={"name": "../evil.json"})
        assert code == 400, "路径穿越名应被拒"
        code, data = _http("POST", base + "/api/config/restore", body={"name": "config-19700101-000000-000.json"})
        assert code == 404, "不存在的快照应 404"

        # Host 校验：伪造外部域名的 Host 头 → 403（DNS rebinding 防护）
        code, data = _http("GET", base + "/api/config", headers={"Host": "evil.example.com"})
        assert code == 403, f"非本机 Host 应 403，实际 {code}"
        code, data = _http("GET", base + "/api/config", headers={"Host": "localhost:9999"})
        assert code == 200, "localhost 应放行"

        # 鉴权：设置 token 后无头 → 401，带头 → 200
        os.environ["WEB_ADMIN_TOKEN"] = "s3cret"
        code, data = _http("GET", base + "/api/config")
        assert code == 401, f"缺 token 应 401，实际 {code}"
        code, data = _http("GET", base + "/api/config", headers={"X-Auth-Token": "s3cret"})
        assert code == 200 and data["ok"], "带 token 应通过"
        code, data = _http("GET", base + "/api/config", headers={"Authorization": "Bearer s3cret"})
        assert code == 200 and data["ok"], "Bearer 也应通过"
    finally:
        for key in ("WEB_ADMIN_TOKEN", "HINATA_SHARED_TOKEN", "HINATA_SHARED_COOKIE"):
            os.environ.pop(key, None)
        server.shutdown()
        server.server_close()
        webui._on_restart_cb = None
        webui._on_poll_cb = None
        webui._on_test_push_cb = None
        webui._on_openid_cb = None
        webui.CONFIG_PATH = orig_config_path
        webui.ENV_PATH = orig_env_path
        webui._trigger_reload = orig_trigger
        webui._rotate_account_creds = orig_rotate

    print("✅ Test 3 通过\n")

    cfg.AUTH_ENABLED = orig_auth
    print("=" * 50)
    print("🎉 全部测试通过！网页管理端工作正常")


if __name__ == "__main__":
    main()
