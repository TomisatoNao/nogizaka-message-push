"""博客翻译链路回归测试：代理一致性、超时预算、状态与缓存一致性。"""

import asyncio
import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import translator  # noqa: E402
from src.webui_modules import archive_handlers  # noqa: E402


def test_translation_client_uses_global_proxy(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(translator.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(translator.cfg, "PROXY", "http://127.0.0.1:7890", raising=False)
    client = translator.create_client(timeout=12)

    assert isinstance(client, FakeClient)
    assert captured["proxy"] == "http://127.0.0.1:7890"
    assert captured["timeout"] == 12
    assert captured["follow_redirects"] is True


def test_blog_translation_total_timeout_does_not_cache_failure(monkeypatch):
    monkeypatch.setattr(translator.cfg, "GEMINI_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(translator.cfg, "ZHIPU_API_KEY", "", raising=False)
    monkeypatch.setattr(translator.cfg, "TRANSLATE_TOTAL_TIMEOUT", 1, raising=False)
    translator._blog_structured_cache.clear()

    async def slow_translate(*_args, **_kwargs):
        await asyncio.sleep(1.1)
        return {"0": "你好"}, "test-model"

    monkeypatch.setattr(translator, "_do_translate_gemini_json", slow_translate)
    result = asyncio.run(
        translator.translate_blog_structured(
            "<p>こんにちは</p>",
            "测试成员",
            "nogizaka46",
            request_id="trace-timeout",
            source="test",
        )
    )

    assert result == ([], "")
    assert not translator._blog_structured_cache


def _translation_db(with_state=True):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    columns = """
        id INTEGER PRIMARY KEY, group_key TEXT, author TEXT, title TEXT,
        body_html TEXT, translation TEXT, content_json TEXT, translation_model TEXT
    """
    if with_state:
        columns += """,
            translation_status TEXT, translation_error TEXT,
            translation_request_id TEXT, translation_updated_at TEXT
        """
    db.execute(f"CREATE TABLE blog_posts ({columns})")
    db.execute(
        """INSERT INTO blog_posts
           (id, group_key, author, title, body_html, translation, content_json, translation_model)
           VALUES (1, 'nogizaka46', '测试成员', '测试博客', '<p>こんにちは</p>', NULL, NULL, NULL)"""
    )
    db.commit()
    return db


class _Handler:
    command = "POST"
    path = "/api/archive/blogs/translate"

    def __init__(self):
        self.payload = None
        self.code = None

    def _send_json(self, payload, code=200):
        self.payload = payload
        self.code = code


def test_manual_translation_records_trace_and_state(monkeypatch):
    db = _translation_db()
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)

    async def fake_translate(*_args, **kwargs):
        assert kwargs["source"] == "archive_manual"
        assert kwargs["request_id"].startswith("manual-")
        return ([{"type": "text", "jp": "こんにちは", "zh": "你好"}], "test-model")

    monkeypatch.setattr(translator, "translate_blog_structured", fake_translate)
    handler = _Handler()
    archive_handlers.handle_archive(
        handler,
        "blogs/translate",
        lambda **_: True,
        lambda: {"id": 1},
    )

    assert handler.code == 200
    assert handler.payload["ok"] is True
    assert handler.payload["request_id"].startswith("manual-")
    row = db.execute(
        "SELECT translation_status, translation_request_id FROM blog_posts WHERE id = 1"
    ).fetchone()
    assert row[0] == "succeeded"
    assert row[1] == handler.payload["request_id"]


def test_manual_translation_duplicate_request_is_rejected(monkeypatch):
    db = _translation_db()
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)
    lock = archive_handlers._get_blog_translation_lock(1)
    assert lock.acquire(blocking=False)
    try:
        handler = _Handler()
        archive_handlers.handle_archive(
            handler,
            "blogs/translate",
            lambda **_: True,
            lambda: {"id": 1},
        )
    finally:
        lock.release()

    assert handler.code == 409
    assert handler.payload["status"] == "running"


def test_manual_translation_failure_is_persisted_for_retry(monkeypatch):
    db = _translation_db()
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)

    async def fake_translate(*_args, **_kwargs):
        return [], ""

    monkeypatch.setattr(translator, "translate_blog_structured", fake_translate)
    handler = _Handler()
    archive_handlers.handle_archive(
        handler,
        "blogs/translate",
        lambda **_: True,
        lambda: {"id": 1},
    )

    assert handler.code == 502
    assert handler.payload["status"] == "failed"
    assert handler.payload["request_id"].startswith("manual-")
    row = db.execute(
        "SELECT translation_status, translation_error, translation_request_id FROM blog_posts WHERE id = 1"
    ).fetchone()
    assert tuple(row)[:2] == ("failed", "no_model_result")
    assert row[2] == handler.payload["request_id"]


def test_partial_translation_is_not_treated_as_final_cache(monkeypatch):
    db = _translation_db()
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)
    calls = []

    async def fake_translate(*_args, **_kwargs):
        calls.append(1)
        return (
            [
                {"type": "text", "jp": "こんにちは", "zh": "你好"},
                {"type": "text", "jp": "さようなら", "zh": ""},
            ],
            "test-model",
        )

    monkeypatch.setattr(translator, "translate_blog_structured", fake_translate)
    for _ in range(2):
        handler = _Handler()
        archive_handlers.handle_archive(
            handler,
            "blogs/translate",
            lambda **_: True,
            lambda: {"id": 1},
        )
        assert handler.code == 200
        assert handler.payload["translation_status"] == "partial"

    assert len(calls) == 2


def test_delete_translation_invalidates_structured_cache(monkeypatch):
    db = _translation_db()
    db.execute(
        "UPDATE blog_posts SET translation = ?, content_json = ?, translation_model = ? WHERE id = 1",
        ("译文", '[{"zh":"你好"}]', "test-model"),
    )
    db.commit()
    monkeypatch.setattr(archive_handlers, "get_blog_db", lambda: db)
    monkeypatch.setattr(archive_handlers, "record_event", lambda *args, **kwargs: None)
    html = "<p>こんにちは</p>"
    key = ("测试成员", translator._get_text_hash(html))
    translator._blog_structured_cache[key] = ([{"type": "text", "jp": "こんにちは", "zh": "你好"}], "test-model")

    handler = _Handler()
    archive_handlers.handle_archive(
        handler,
        "blogs/delete_translation",
        lambda **_: True,
        lambda: {"id": 1},
    )

    assert handler.code == 200
    assert key not in translator._blog_structured_cache
