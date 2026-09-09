import asyncio
import json
import sqlite3

from tools import backfill_blogs


def make_db():
    db = sqlite3.connect(":memory:")
    db.execute(
        """
        CREATE TABLE blog_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key TEXT NOT NULL,
            author TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            date TEXT,
            body_html TEXT,
            body_text TEXT,
            translation TEXT,
            content_json TEXT,
            translation_model TEXT,
            translation_status TEXT,
            translation_error TEXT,
            translation_request_id TEXT,
            translation_updated_at TEXT,
            images_json TEXT,
            image_paths_json TEXT,
            raw_json TEXT NOT NULL
        )
        """
    )
    return db


def test_normalize_groups_is_deduplicated_and_fixed_order():
    assert backfill_blogs.normalize_groups(["sakurazaka", "nogizaka", "nogizaka"]) == [
        "nogizaka",
        "sakurazaka",
    ]
    assert backfill_blogs.normalize_groups(["all"]) == list(backfill_blogs.GROUP_ORDER)
    assert backfill_blogs.normalize_groups(None) == list(backfill_blogs.GROUP_ORDER)


def test_save_post_downloads_local_images_and_is_idempotent(monkeypatch, tmp_path):
    db = make_db()
    stats = backfill_blogs.GroupStats("nogizaka")
    monkeypatch.setattr(backfill_blogs, "BLOG_IMAGE_DIR", tmp_path)
    calls = []

    async def fake_download(_client, images, *_args, **_kwargs):
        calls.append(images)
        return ["nogizaka/member/01.jpg", "nogizaka/member/02.jpg"]

    monkeypatch.setattr(backfill_blogs, "_download_images", fake_download)
    post = {
        "group_key": "nogizaka",
        "author": "测试成员",
        "title": "测试博客",
        "url": "https://example.test/blog/1",
        "date": "2026/09/10 10:00:00",
        "body_html": "正文",
        "body_text": "正文",
        "images": ["https://example.test/1.jpg", "https://example.test/2.jpg"],
        "raw": {"id": 1},
    }

    asyncio.run(backfill_blogs._save_post(db, object(), post, stats, download_images=True))
    db.commit()
    assert stats.added == 1
    assert calls == [["https://example.test/1.jpg", "https://example.test/2.jpg"]]
    row = db.execute("SELECT images_json, image_paths_json FROM blog_posts").fetchone()
    assert json.loads(row[0]) == post["images"]
    assert json.loads(row[1]) == ["nogizaka/member/01.jpg", "nogizaka/member/02.jpg"]

    # 文件不存在时，重复执行会进入补齐逻辑，但不会新增第二条记录。
    stats2 = backfill_blogs.GroupStats("nogizaka")
    asyncio.run(backfill_blogs._save_post(db, object(), post, stats2, download_images=True))
    assert stats2.added == 0
    assert stats2.skipped == 1
    assert stats2.repaired == 1
    assert db.execute("SELECT COUNT(*) FROM blog_posts").fetchone()[0] == 1
