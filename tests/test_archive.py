"""验证消息归档：存储合并、媒体命名、查看器 API、Range、路径防护

运行: python tests/test_archive.py
"""
import asyncio
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


def _http(method: str, url: str, headers: dict | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def main() -> None:
    import config.config as cfg
    from src import archive

    tmpdir = Path(tempfile.mkdtemp(prefix="archive_test_"))
    orig_dir = cfg.ARCHIVE_DIR
    orig_enabled = cfg.ARCHIVE_ENABLED
    orig_media = cfg.ARCHIVE_MEDIA
    orig_auth = cfg.AUTH_ENABLED
    cfg.ARCHIVE_DIR = str(tmpdir)
    cfg.ARCHIVE_ENABLED = True
    cfg.ARCHIVE_MEDIA = False   # 单测不碰网络
    cfg.AUTH_ENABLED = False    # 本套件测归档本身，鉴权由 test_auth 覆盖

    member = {"m_name": "测试 成员", "m_id": "99", "group_type": "nogizaka46", "account_id": "x"}

    try:
        # ── Test 1: 归档写入 + 幂等合并 + 译文 ───────────
        print("=== Test 1: 归档写入与合并 ===")
        msg1 = {"id": 101, "type": "text", "text": "こんにちは",
                "published_at": "2026-07-05T10:00:00Z", "updated_at": "2026-07-05T10:00:00Z"}
        msg2 = {"id": 102, "type": "picture", "text": "写真",
                "file": "https://example.com/a.jpg",
                "published_at": "2026-07-06T11:00:00Z", "updated_at": "2026-07-06T11:00:00Z"}
        asyncio.run(archive.archive_message(member, msg1, translated="你好"))
        asyncio.run(archive.archive_message(member, msg2))
        asyncio.run(archive.archive_message(member, msg1, translated="你好"))   # 重复 → 幂等

        mdir = archive.member_dir_name("测试 成员")
        assert mdir == "测试_成员", f"目录名应替换空格: {mdir}"
        saved = archive.load_month(mdir, 2026, 7)
        assert len(saved) == 2, f"重复归档不应产生重复记录: {len(saved)}"
        assert saved[0]["id"] == 101 and saved[0]["_translation"] == "你好", "应按时间排序且带译文"
        assert saved[1]["id"] == 102
        print("✅ Test 1 通过\n")

        # ── Test 2: 扩展名推断 + 索引扫描 ────────────────
        print("=== Test 2: 工具函数 ===")
        assert archive._guess_extension("https://x/y.jpg?sig=1", None) == ".jpg"
        assert archive._guess_extension("https://x/y", "image/png") == ".png"
        assert archive._guess_extension("https://x/y", None) == ".bin"

        months = archive.list_months(mdir)
        assert months == [{"year": 2026, "month": 7, "count": 2}], f"月份索引: {months}"
        assert archive.list_members() == ["测试_成员"]

        # 标记一条下载失败，验证失败索引
        month_json = tmpdir / "测试_成员" / "2026" / "07" / "messages.json"
        data = json.loads(month_json.read_text(encoding="utf-8"))
        data[1]["_download_failed"] = True
        month_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        ok_ids, fail_ids = archive.load_archived_ids("测试 成员")
        assert ok_ids == {"101"} and fail_ids == {"102"}, f"失败索引: {ok_ids} {fail_ids}"

        # "幽灵"状态：媒体消息无 _local_file 也无失败标记（下载中途被杀）→ 应纳入重试
        data = json.loads(month_json.read_text(encoding="utf-8"))
        data.append({"id": 103, "type": "video", "file": "https://x/v.mp4",
                     "updated_at": "2026-07-07T00:00:00Z"})
        month_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _, fail_ids = archive.load_archived_ids("测试 成员")
        assert "103" in fail_ids, f"无本地文件的媒体消息应进重试集合: {fail_ids}"
        data = [m for m in data if m.get("id") != 103]   # 还原，避免影响后续用例
        month_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print("✅ Test 2 通过\n")

        # ── Test 3: 查看器 API ───────────────────────────
        print("=== Test 3: 查看器 API ===")
        # 造一个媒体文件并把 102 标记为已下载
        img_dir = tmpdir / "测试_成员" / "2026" / "07" / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        img_file = img_dir / "20260706_110000_102.jpg"
        img_file.write_bytes(b"JPEGDATA-0123456789")
        data = json.loads(month_json.read_text(encoding="utf-8"))
        data[1].pop("_download_failed", None)
        data[1]["_local_file"] = "2026/07/images/20260706_110000_102.jpg"
        month_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        from src import webui
        os.environ.pop("WEB_ADMIN_TOKEN", None)
        server = webui.start_webui(host="127.0.0.1", port=0)
        assert server is not None
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            code, body, _ = _http("GET", base + "/archive")
            assert code == 200 and "消息归档".encode() in body, "归档页面应可访问"

            code, body, _ = _http("GET", base + "/api/archive/members")
            j = json.loads(body)
            assert code == 200 and j["members"][0]["name"] == "测试_成员" and j["members"][0]["total"] == 2

            code, body, _ = _http("GET", base + "/api/archive/months?member=%E6%B5%8B%E8%AF%95_%E6%88%90%E5%91%98")
            j = json.loads(body)
            assert code == 200 and j["months"][0]["count"] == 2

            code, body, _ = _http("GET", base + "/api/archive/messages?member=%E6%B5%8B%E8%AF%95_%E6%88%90%E5%91%98&year=2026&month=7")
            j = json.loads(body)
            assert code == 200 and j["total"] == 2
            assert j["messages"][0]["translation"] == "你好"
            media_url = j["messages"][1]["media_url"]
            assert media_url and media_url.endswith("102.jpg"), f"media_url: {media_url}"

            code, body, _ = _http("GET", base + "/api/archive/messages?member=%E6%B5%8B%E8%AF%95_%E6%88%90%E5%91%98&year=2026&month=7&type=text")
            assert json.loads(body)["total"] == 1, "类型过滤应生效"

            # 媒体：完整 + Range 206 + 越权路径
            from urllib.parse import quote
            code, body, headers = _http("GET", base + quote(media_url))
            assert code == 200 and body == b"JPEGDATA-0123456789"
            code, body, headers = _http("GET", base + quote(media_url), headers={"Range": "bytes=0-3"})
            assert code == 206 and body == b"JPEG" and headers.get("Content-Range") == "bytes 0-3/19", \
                f"Range: {code} {body} {headers.get('Content-Range')}"
            code, body, _ = _http("GET", base + "/api/archive/media/%E6%B5%8B%E8%AF%95_%E6%88%90%E5%91%98/../../config/config.json")
            assert code in (403, 404), f"路径穿越应被拒: {code}"
            code, body, _ = _http("GET", base + "/api/archive/months?member=ghost")
            assert code == 404, "未知成员应 404"
        finally:
            server.shutdown()
            server.server_close()
        print("✅ Test 3 通过\n")

        # ── Test 4: 每日摘要构建 ─────────────────────────
        print("=== Test 4: 每日摘要 ===")
        from datetime import datetime, timedelta, timezone

        from src.app import _build_daily_summary

        # 造一条"今天"的消息（JST），确保今日计数能统计到
        jst_now = datetime.now(timezone(timedelta(hours=9)))
        utc_now = (jst_now - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        member2 = dict(member, m_name="摘要 测试")
        asyncio.run(archive.archive_message(
            member2, {"id": 201, "type": "text", "text": "today",
                      "published_at": utc_now, "updated_at": utc_now}))
        orig_monitor = cfg.MONITOR_LIST[:]
        cfg.MONITOR_LIST.clear()
        cfg.MONITOR_LIST.append({"m_name": "摘要 测试", "m_id": "1",
                                 "group_type": "nogizaka46", "account_id": "x"})
        try:
            text = _build_daily_summary()
        finally:
            cfg.MONITOR_LIST.clear()
            cfg.MONITOR_LIST.extend(orig_monitor)
        assert "每日运行摘要" in text and "摘要测试 1 条" in text, f"摘要应含今日计数:\n{text}"
        assert "正常运行" in text
        assert "存储:" in text and "归档" in text, f"摘要应含存储水位:\n{text}"

        # 磁盘告警：阈值调到极大时应出现警告
        import src.app as app_mod
        orig_warn = app_mod.DISK_WARN_BYTES
        app_mod.DISK_WARN_BYTES = 10 ** 18
        try:
            assert "磁盘空间不足" in app_mod._storage_line(), "低于阈值应告警"
        finally:
            app_mod.DISK_WARN_BYTES = orig_warn
        assert "磁盘空间不足" not in app_mod._storage_line(), "空间充足时不应告警"

        # 摘要发送失败 → 按次重试，全失败后记 PERSISTENT 错误
        import src.notifier as notifier_mod
        from src import health as health_mod
        orig_send = notifier_mod.send_report_message
        orig_retry, orig_attempts = app_mod.SUMMARY_RETRY_SECONDS, app_mod.SUMMARY_MAX_ATTEMPTS
        calls = []

        async def _fail(text):
            calls.append(1)
            return False

        notifier_mod.send_report_message = _fail
        app_mod.SUMMARY_RETRY_SECONDS = 0
        app_mod.SUMMARY_MAX_ATTEMPTS = 3
        try:
            before = len(health_mod.get_tracker().snapshot()["errors"])
            asyncio.run(app_mod._send_summary_with_retry())
            assert len(calls) == 3, f"应重试到 3 次: {len(calls)}"
            after = health_mod.get_tracker().snapshot()["errors"]
            assert len(after) > before and after[-1]["tier"] == "PERSISTENT", \
                "连续失败应记 PERSISTENT 错误"

            # 第二次尝试成功 → 不再继续重试
            calls.clear()
            async def _second_ok(text):
                calls.append(1)
                return len(calls) >= 2
            notifier_mod.send_report_message = _second_ok
            asyncio.run(app_mod._send_summary_with_retry())
            assert len(calls) == 2, f"成功后应停止重试: {len(calls)}"
        finally:
            notifier_mod.send_report_message = orig_send
            app_mod.SUMMARY_RETRY_SECONDS, app_mod.SUMMARY_MAX_ATTEMPTS = orig_retry, orig_attempts
        print("✅ Test 4 通过\n")

        # ── Test 5: 并发合并写（无丢失、无损坏）─────────
        print("=== Test 5: 并发写入 ===")
        member3 = dict(member, m_name="并发 测试")

        async def _concurrent_writes():
            tasks = []
            for i in range(30):
                mid = 300 + (i % 10)   # 10 个 id 各写 3 次（含重复更新）
                tasks.append(archive.archive_message(member3, {
                    "id": mid, "type": "text", "text": f"v{i}",
                    "published_at": "2026-06-01T10:00:00Z",
                    "updated_at": f"2026-06-01T10:{i % 10:02d}:00Z"}))
            await asyncio.gather(*tasks)

        asyncio.run(_concurrent_writes())
        saved = archive.load_month(archive.member_dir_name("并发 测试"), 2026, 6)
        assert len(saved) == 10, f"30 次并发写 10 个 id 应得 10 条: {len(saved)}"
        json.loads((tmpdir / "并发_测试" / "2026" / "06" / "messages.json").read_text(encoding="utf-8"))
        print("✅ Test 5 通过\n")

        # ── Test 6: 边角情况 ─────────────────────────────
        print("=== Test 6: 边角情况 ===")
        # 非法字符成员名 → 目录名净化
        weird = dict(member, m_name='冨里/奈央?')
        asyncio.run(archive.archive_message(weird, {
            "id": 401, "type": "text", "text": "x",
            "published_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}))
        assert (tmpdir / "冨里_奈央_").is_dir(), "非法字符应替换为下划线"

        # 跨月消息 → 分月文件
        asyncio.run(archive.archive_message(weird, {
            "id": 402, "type": "text", "text": "y",
            "published_at": "2026-06-15T00:00:00Z", "updated_at": "2026-06-15T00:00:00Z"}))
        assert archive.load_month("冨里_奈央_", 2026, 5) and archive.load_month("冨里_奈央_", 2026, 6)

        # 同 id 再归档（内容更新）→ 覆盖不重复
        asyncio.run(archive.archive_message(weird, {
            "id": 401, "type": "text", "text": "x-edited",
            "published_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}))
        may = archive.load_month("冨里_奈央_", 2026, 5)
        assert len(may) == 1 and may[0]["text"] == "x-edited", "同 id 应覆盖更新"

        # 失败标记清除：先失败，重试成功（带 _local_file 的增量）后标记应消失
        fail_member = dict(member, m_name="失败 重试")
        asyncio.run(archive._merge_write(fail_member["m_name"],
                    archive._parse_utc("2026-05-02T00:00:00Z"),
                    {"id": 500, "updated_at": "2026-05-02T00:00:00Z", "_download_failed": True}))
        asyncio.run(archive._merge_write(fail_member["m_name"],
                    archive._parse_utc("2026-05-02T00:00:00Z"),
                    {"id": 500, "updated_at": "2026-05-02T00:00:00Z", "_local_file": "a.jpg"}))
        rec = archive.load_month(archive.member_dir_name("失败 重试"), 2026, 5)[0]
        assert rec.get("_local_file") == "a.jpg" and not rec.get("_download_failed"), \
            f"重试成功应清除失败标记: {rec}"

        # 坏时间戳 → 安静跳过不抛
        asyncio.run(archive.archive_message(weird, {"id": 403, "updated_at": "not-a-date"}))
        # 开关关闭 → schedule 为 no-op（在无事件循环环境调用不应报错）
        cfg.ARCHIVE_ENABLED = False
        archive.schedule_archive(weird, {"id": 404, "updated_at": "2026-05-01T00:00:00Z"})
        cfg.ARCHIVE_ENABLED = True

        # 损坏的 messages.json → 读取返回空、原文件改名保留现场（不静默丢数据）
        bad_json = tmpdir / "冨里_奈央_" / "2026" / "06" / "messages.json"
        bad_json.write_text("{corrupted", encoding="utf-8")
        assert archive.load_month("冨里_奈央_", 2026, 6) == []
        rescued = list((tmpdir / "冨里_奈央_" / "2026" / "06").glob("messages.corrupt-*.json"))
        assert rescued, "损坏文件应被改名保留"

        # 月度计数缓存：mtime 不变走缓存，文件更新后缓存失效
        cache_json = tmpdir / "测试_成员" / "2026" / "07" / "messages.json"
        c1 = archive.list_months("测试_成员")[0]["count"]
        data = json.loads(cache_json.read_text(encoding="utf-8"))
        data.append({"id": 999, "type": "text", "updated_at": "2026-07-09T00:00:00Z"})
        cache_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.utime(cache_json, (1e9, 1e9))   # 强制改 mtime，避免同秒分辨率下缓存不失效
        c2 = archive.list_months("测试_成员")[0]["count"]
        assert c2 == c1 + 1, f"文件更新后计数缓存应失效: {c1} → {c2}"
        print("✅ Test 6 通过\n")

        # ── Test 7: 回填工具单元 ─────────────────────────
        print("=== Test 7: AdaptivePacer / 进度文件 ===")
        from tools.backfill_archive import AdaptivePacer, _load_progress, _save_progress
        import tools.backfill_archive as bf
        p = AdaptivePacer(base=1.5, floor=0.8, ceil=90.0)
        for _ in range(20):
            p.on_success()
        assert abs(p.delay - 0.8) < 1e-9, f"连续成功应降到地板值: {p.delay}"
        p.on_error()
        assert p.delay == 3.0, f"错误应翻倍(自 1.5 起): {p.delay}"
        p.on_error(rate_limited=True)
        assert p.delay == 9.0, f"限流应三倍: {p.delay}"
        for _ in range(10):
            p.on_error(rate_limited=True)
        assert p.delay == 90.0, "退避应封顶"

        orig_pp = bf.PROGRESS_PATH
        bf.PROGRESS_PATH = tmpdir / "progress.json"
        try:
            _save_progress({"nogizaka46_55": "2026-01-01T00:00:00Z"})
            assert _load_progress() == {"nogizaka46_55": "2026-01-01T00:00:00Z"}
            bf.PROGRESS_PATH.write_text("{bad", encoding="utf-8")
            assert _load_progress() == {}, "损坏的进度文件应回退为空"
        finally:
            bf.PROGRESS_PATH = orig_pp
        print("✅ Test 7 通过\n")

        # ── Test 7.5: 跨月搜索 ───────────────────────────
        print("=== Test 7.5: 搜索 ===")
        s_member = dict(member, m_name="搜索 用例")
        for i, (mo, text, trans) in enumerate([
            (3, "今日はライブでした", ""),
            (4, "ライブ楽しかった！", "演唱会真开心！"),
            (5, "おやすみなさい", "晚安"),
        ]):
            asyncio.run(archive.archive_message(
                s_member,
                {"id": 600 + i, "type": "text", "text": text,
                 "published_at": f"2026-{mo:02d}-10T10:00:00Z",
                 "updated_at": f"2026-{mo:02d}-10T10:00:00Z"},
                translated=trans))
        sdir = archive.member_dir_name("搜索 用例")
        hits = archive.search(sdir, "ライブ")
        assert len(hits) == 2 and hits[0]["_month"] == 4 and hits[1]["_month"] == 3, \
            f"跨月命中且新的在前: {[(h['id'], h['_month']) for h in hits]}"
        assert len(archive.search(sdir, "演唱会")) == 1, "译文应参与匹配"
        assert len(archive.search(sdir, "ライブ 楽しかった")) == 1, "空格分词应为 AND 语义"
        assert archive.search(sdir, "LIVE不存在的词") == []
        assert archive.search(sdir, "   ") == [], "空关键词返回空"
        assert len(archive.search(sdir, "ライブ", type_filter={"video"})) == 0, "类型过滤应生效"
        print("✅ Test 7.5 通过\n")

        # ── Test 7.8: 日历按天计数（JST 日界）────────────
        print("=== Test 7.8: 日历计数 ===")
        c_member = dict(member, m_name="日历 用例")
        for i, utc in enumerate([
            "2026-07-05T10:00:00Z",   # JST 7/5 19:00
            "2026-07-05T14:59:00Z",   # JST 7/5 23:59
            "2026-07-05T15:00:00Z",   # JST 7/6 00:00 ← 跨日界
            "2026-07-31T16:00:00Z",   # JST 8/1 ← 跨月界
        ]):
            asyncio.run(archive.archive_message(c_member, {
                "id": 700 + i, "type": "text", "text": "x",
                "published_at": utc, "updated_at": utc}))
        cdir = archive.member_dir_name("日历 用例")
        days = archive.day_counts(cdir)
        assert days.get("2026-07-05") == 2, f"JST 7/5 应 2 条: {days}"
        assert days.get("2026-07-06") == 1, f"UTC 15:00 应归入 JST 次日: {days}"
        assert days.get("2026-08-01") == 1, f"月末跨界应归入 JST 8/1: {days}"
        days2 = archive.day_counts(cdir)   # 二次调用走缓存，结果一致
        assert days2 == days

        # 类型过滤：7/5 补一条 picture，各口径计数应正确
        asyncio.run(archive.archive_message(c_member, {
            "id": 704, "type": "picture", "text": "photo",
            "published_at": "2026-07-05T11:00:00Z", "updated_at": "2026-07-05T11:00:00Z"}))
        assert archive.day_counts(cdir)["2026-07-05"] == 3, "全类型应 3 条"
        assert archive.day_counts(cdir, {"text"})["2026-07-05"] == 2, "text 过滤应 2 条"
        assert archive.day_counts(cdir, {"picture", "image"})["2026-07-05"] == 1, "picture 过滤应 1 条"
        assert "2026-07-06" not in archive.day_counts(cdir, {"picture", "image"}), \
            "过滤后无该类型的日期不应出现"
        print("✅ Test 7.8 通过\n")

        # ── Test 8: 查看器 API 边界 ──────────────────────
        print("=== Test 8: API 边界 ===")
        server = webui.start_webui(host="127.0.0.1", port=0)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        m_enc = "%E6%B5%8B%E8%AF%95_%E6%88%90%E5%91%98"
        try:
            # 分页越界 → 空列表但 total 正确
            code, body, _ = _http("GET", base + f"/api/archive/messages?member={m_enc}&year=2026&month=7&page=99")
            j = json.loads(body)
            assert code == 200 and j["messages"] == [] and j["total"] == 3, \
                f"越界页应空且 total 正确: {j['total']}"

            # per_page 超限 → 被压到 200
            code, body, _ = _http("GET", base + f"/api/archive/messages?member={m_enc}&year=2026&month=7&per_page=9999")
            assert code == 200, "超大 per_page 应被钳制而不是报错"

            # type=image 与 picture 等价
            code, body, _ = _http("GET", base + f"/api/archive/messages?member={m_enc}&year=2026&month=7&type=image")
            assert json.loads(body)["total"] == 1, "image 应命中 picture 类型"

            # 非数字参数 → 400
            code, body, _ = _http("GET", base + f"/api/archive/messages?member={m_enc}&year=abc&month=7")
            assert code == 400

            # Range 形态：后缀 / 开区间 / 非法 / 越界（文件内容 b"JPEGDATA-0123456789"，19 字节）
            from urllib.parse import quote as _q
            murl = base + _q("/api/archive/media/测试_成员/2026/07/images/20260706_110000_102.jpg")
            code, body, h = _http("GET", murl, headers={"Range": "bytes=-4"})
            assert code == 206 and body == b"6789", f"后缀 Range: {code} {body}"
            code, body, h = _http("GET", murl, headers={"Range": "bytes=15-"})
            assert code == 206 and body == b"6789" and h.get("Content-Range") == "bytes 15-18/19"
            code, body, h = _http("GET", murl, headers={"Range": "bytes=abc"})
            assert code == 200 and len(body) == 19, "非法 Range 应回退全量 200"
            code, body, h = _http("GET", murl, headers={"Range": "bytes=99-"})
            assert code == 416, f"越界 Range 应 416: {code}"

            # 缓存策略：绝不能让浏览器在登出后仍从本地缓存渲染私密媒体
            code, body, h = _http("GET", murl)
            cc = h.get("Cache-Control", "")
            assert "no-cache" in cc and "private" in cc, f"媒体应 private+no-cache: {cc!r}"
            assert "max-age" not in cc, f"媒体不得带 max-age（会绕过鉴权）: {cc!r}"
            etag = h.get("ETag", "")
            assert etag and h.get("Last-Modified"), "应带 ETag / Last-Modified 供条件请求"
            # 条件请求命中 → 304（省流量但仍每次回源鉴权）
            code, body, h = _http("GET", murl, headers={"If-None-Match": etag})
            assert code == 304, f"ETag 命中应 304: {code}"
            code, body, h = _http("GET", murl, headers={"If-None-Match": '"stale-etag"'})
            assert code == 200, "ETag 不匹配应返回完整内容"

            # 搜索接口：命中 / 缺参 / 未知成员
            s_enc = "%E6%90%9C%E7%B4%A2_%E7%94%A8%E4%BE%8B"
            code, body, _ = _http("GET", base + f"/api/archive/search?member={s_enc}&q=%E3%83%A9%E3%82%A4%E3%83%96")
            j = json.loads(body)
            assert code == 200 and j["total"] == 2 and j["messages"][0]["year"] == 2026, f"搜索: {j}"
            code, body, _ = _http("GET", base + f"/api/archive/search?member={s_enc}&q=")
            assert code == 400, "缺关键词应 400"
            code, body, _ = _http("GET", base + f"/api/archive/search?member={s_enc}&q=" + "x" * 101)
            assert code == 400 and "100" in json.loads(body)["errors"][0], "超长关键词应被拒绝"
            code, body, _ = _http("GET", base + "/api/archive/search?member=ghost&q=x")
            assert code == 404

            # 日历接口
            c_enc = "%E6%97%A5%E5%8E%86_%E7%94%A8%E4%BE%8B"
            code, body, _ = _http("GET", base + f"/api/archive/calendar?member={c_enc}")
            j = json.loads(body)
            assert code == 200 and j["days"]["2026-07-05"] == 3, f"日历接口: {j}"
            code, body, _ = _http("GET", base + f"/api/archive/calendar?member={c_enc}&type=text")
            assert json.loads(body)["days"]["2026-07-05"] == 2, "日历类型过滤应生效"
            code, body, _ = _http("GET", base + f"/api/archive/calendar?member={c_enc}&type=bogus")
            assert code == 400, "非法类型应 400"
            code, body, _ = _http("GET", base + "/api/archive/calendar?member=ghost")
            assert code == 404
        finally:
            server.shutdown()
            server.server_close()
        print("✅ Test 8 通过\n")

    finally:
        cfg.ARCHIVE_DIR = orig_dir
        cfg.ARCHIVE_ENABLED = orig_enabled
        cfg.ARCHIVE_MEDIA = orig_media
        cfg.AUTH_ENABLED = orig_auth

    print("=" * 50)
    print("🎉 全部测试通过！消息归档工作正常")


if __name__ == "__main__":
    main()
