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
    cfg.ARCHIVE_DIR = str(tmpdir)
    cfg.ARCHIVE_ENABLED = True
    cfg.ARCHIVE_MEDIA = False   # 单测不碰网络

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
        print("✅ Test 4 通过\n")

    finally:
        cfg.ARCHIVE_DIR = orig_dir
        cfg.ARCHIVE_ENABLED = orig_enabled
        cfg.ARCHIVE_MEDIA = orig_media

    print("=" * 50)
    print("🎉 全部测试通过！消息归档工作正常")


if __name__ == "__main__":
    main()
