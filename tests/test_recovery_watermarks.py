"""Failure and cancellation regressions for safe recovery watermarks."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from src import archive, blog_fetcher, delivery_state, fetcher
from src.notifier import DeliveryAttempt, DeliveryReport


@pytest.fixture
def member_db(monkeypatch, tmp_path):
    path = tmp_path / "messages.db"
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE timeline_watermarks (group_type TEXT, m_id TEXT, last_time TEXT, updated_at REAL, PRIMARY KEY(group_type,m_id))"
    )
    monkeypatch.setattr(archive, "init_db", lambda: db)
    delivery_state._conn()
    monkeypatch.setattr(fetcher.cfg, "ENABLE_TRANSLATION", False)
    monkeypatch.setattr(fetcher.cfg, "QQ_SEND_INTERVAL", 0)
    monkeypatch.setattr(fetcher.cfg, "SENT_IDS_DIR", str(tmp_path / "sent"))
    monkeypatch.setattr(archive, "archive_message", AsyncMock())
    monkeypatch.setattr(fetcher, "_maybe_alert_napcat_lane_failure", AsyncMock())
    yield db, path
    db.close()


@pytest.mark.asyncio
async def test_pending_write_failure_preserves_safe_watermark_and_successful_routes(member_db, monkeypatch):
    db, path = member_db
    member = dict(m_name="audit", group_type="nogizaka46", m_id="1")
    old_time = "2026-11-11T00:00:00Z"
    messages = [dict(id=str(i), updated_at=f"2026-11-11T00:0{i}:00Z", text="body") for i in (1, 2)]
    archive.set_timeline_watermark("nogizaka46", "1", old_time)
    report = DeliveryReport(
        (
            DeliveryAttempt("napcat", "napcat:42", "QQ", "42", False, "timeout"),
            DeliveryAttempt("tg", "tg:main", "TG", "main", True),
        ),
        ("napcat:42", "tg:main"),
    )
    send = AsyncMock(return_value=report)
    monkeypatch.setattr(fetcher, "send_member_message_detailed", send)
    db.execute(
        "CREATE TRIGGER reject_pending BEFORE INSERT ON delivery_pending_routes BEGIN SELECT RAISE(ABORT,'test pending failure'); END"
    )
    last_time = [old_time]

    assert await fetcher._push_member_messages(member, messages, [], set(), last_time, "", None) is False
    assert send.await_count == 1
    assert last_time == [old_time]
    assert archive.get_timeline_watermark("nogizaka46", "1") == old_time
    with sqlite3.connect(path) as reopened:
        assert reopened.execute("SELECT route_id FROM delivered_routes").fetchall() == [("tg:main",)]
        assert reopened.execute("SELECT COUNT(*) FROM delivery_pending_routes").fetchone()[0] == 0

    db.execute("DROP TRIGGER reject_pending")
    retry = AsyncMock(
        return_value=DeliveryReport(
            (DeliveryAttempt("napcat", "napcat:42", "QQ", "42", False, "timeout"),), ("napcat:42", "tg:main")
        )
    )
    monkeypatch.setattr(fetcher, "send_member_message_detailed", retry)
    assert await fetcher._push_member_messages(member, messages[:1], [], set(), last_time, "", None) is False
    assert retry.await_args.kwargs["skip_route_ids"] == {"tg:main"}
    assert len(delivery_state.pending_routes_for_member("nogizaka46", "1")) == 1


@pytest.mark.asyncio
async def test_delivery_state_read_failure_does_not_send(member_db, monkeypatch):
    db, _ = member_db
    db.execute("DROP TABLE delivered_routes")
    monkeypatch.setattr(delivery_state, "_conn", lambda: db)
    send = AsyncMock()
    monkeypatch.setattr(fetcher, "send_member_message_detailed", send)
    old_time = "2026-11-11T00:00:00Z"
    result = await fetcher._push_member_messages(
        dict(m_name="audit", group_type="nogizaka46", m_id="1"),
        [dict(id="1", updated_at="2026-11-11T00:01:00Z", text="body")],
        [],
        set(),
        [old_time],
        "",
        None,
    )
    assert result is False
    send.assert_not_awaited()


@pytest.fixture
def blog_db(tmp_path, monkeypatch):
    monkeypatch.setattr(blog_fetcher, "BLOG_DB_PATH", tmp_path / "blogs.db")
    monkeypatch.setattr(blog_fetcher, "_blog_images_healed", True)
    monkeypatch.setattr(blog_fetcher, "_in_flight_blogs", set())
    monkeypatch.setattr(blog_fetcher, "_RECORDS_PATH", tmp_path / "legacy.json")
    monkeypatch.setattr(blog_fetcher, "_LEGACY_RECORDS_PATH", tmp_path / "other-legacy.json")
    db = blog_fetcher.init_blog_db()
    blog_fetcher._save_records({"nogizaka": "old"}, db)

    async def parse(post, key, name, *_args):
        return {**post, "group_key": key, "group_name": name, "author": "audit", "title": post["url"]}

    monkeypatch.setattr(blog_fetcher, "_process_single_post", parse)
    yield db
    db.close()


def configure_blog_page(monkeypatch, urls, *, key="nogizaka"):
    fetch = AsyncMock(return_value=[{"url": url} for url in urls])
    monkeypatch.setattr(blog_fetcher, "TASKS", [("audit", fetch, None, key, False)])
    return fetch


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_url", ["one", "two", "three"])
async def test_blog_insert_failure_does_not_skip_failed_article(blog_db, monkeypatch, failed_url):
    db = blog_db
    configure_blog_page(monkeypatch, ["three", "two", "one", "old"])
    db.execute(
        f"CREATE TRIGGER reject_post BEFORE INSERT ON blog_posts WHEN NEW.url='{failed_url}' BEGIN SELECT RAISE(ABORT,'test blog failure'); END"
    )
    first = await blog_fetcher.run_blog_cycle(None, db, {})
    expected_watermark = {"one": "old", "two": "one", "three": "two"}[failed_url]
    assert blog_fetcher._load_records(db)["nogizaka"] == expected_watermark
    assert {post["url"] for post in first} == {"one", "two", "three"} - {failed_url}
    assert not blog_fetcher._in_flight_blogs

    db.execute("DROP TRIGGER reject_post")
    with sqlite3.connect(blog_fetcher.BLOG_DB_PATH) as reopened:
        reopened.row_factory = sqlite3.Row
        second = await blog_fetcher.run_blog_cycle(None, reopened, {})
    assert [post["url"] for post in second] == [failed_url]
    assert blog_fetcher._load_records(db)["nogizaka"] == "three"
    assert db.execute("SELECT COUNT(*) FROM blog_posts").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_blog_cancellation_releases_reservation_and_retries(blog_db, monkeypatch):
    db = blog_db
    configure_blog_page(monkeypatch, ["new", "old"])
    started = asyncio.Event()

    async def wait_for_cancel(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(blog_fetcher, "_process_single_post", wait_for_cancel)
    task = asyncio.create_task(blog_fetcher.run_blog_cycle(None, db, {}))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert blog_fetcher._load_records(db)["nogizaka"] == "old"
    assert not blog_fetcher._in_flight_blogs

    async def parse(post, key, *_args):
        return {**post, "group_key": key, "author": "audit", "title": "new"}

    monkeypatch.setattr(blog_fetcher, "_process_single_post", parse)
    with sqlite3.connect(blog_fetcher.BLOG_DB_PATH) as reopened:
        reopened.row_factory = sqlite3.Row
        assert len(await blog_fetcher.run_blog_cycle(None, reopened, {})) == 1
    assert blog_fetcher._load_records(db)["nogizaka"] == "new"


@pytest.mark.asyncio
async def test_blog_existing_inflight_article_blocks_watermark(blog_db, monkeypatch):
    db = blog_db
    configure_blog_page(monkeypatch, ["two", "one", "old"])
    blog_fetcher._in_flight_blogs.add("one")
    assert [post["url"] for post in await blog_fetcher.run_blog_cycle(None, db, {})] == ["two"]
    assert blog_fetcher._load_records(db)["nogizaka"] == "old"
    assert blog_fetcher._in_flight_blogs == {"one"}
    blog_fetcher._in_flight_blogs.clear()
    assert [post["url"] for post in await blog_fetcher.run_blog_cycle(None, db, {})] == ["one"]
    assert blog_fetcher._load_records(db)["nogizaka"] == "two"


@pytest.mark.asyncio
async def test_blog_first_run_and_missing_watermark_keep_skip_policy(blog_db, monkeypatch):
    db = blog_db
    db.execute("DELETE FROM blog_watermarks")
    db.commit()
    configure_blog_page(monkeypatch, ["new", "older"])
    assert await blog_fetcher.run_blog_cycle(None, db, {}) == []
    assert blog_fetcher._load_records(db)["nogizaka"] == "new"
    assert db.execute("SELECT COUNT(*) FROM blog_posts").fetchone()[0] == 0
    configure_blog_page(monkeypatch, ["latest", "unseen"])
    assert await blog_fetcher.run_blog_cycle(None, db, {}) == []
    assert blog_fetcher._load_records(db)["nogizaka"] == "latest"


@pytest.mark.asyncio
async def test_confirmed_route_write_failure_stops_before_later_message(member_db, monkeypatch):
    db, _ = member_db
    member = dict(m_name="audit", group_type="nogizaka46", m_id="1")
    old_time = "2026-11-11T00:00:00Z"
    db.execute(
        "CREATE TRIGGER reject_success BEFORE INSERT ON delivered_routes BEGIN SELECT RAISE(ABORT,'test receipt failure'); END"
    )
    send = AsyncMock(return_value=DeliveryReport((DeliveryAttempt("tg", "tg:main", "TG", "main", True),), ("tg:main",)))
    saved = []
    monkeypatch.setattr(fetcher, "send_member_message_detailed", send)
    monkeypatch.setattr(fetcher, "save_sent_id", lambda *_args: saved.append(True))
    last_time = [old_time]
    result = await fetcher._push_member_messages(
        member,
        [dict(id=str(i), updated_at=f"2026-11-11T00:0{i}:00Z", text="body") for i in (1, 2)],
        [],
        set(),
        last_time,
        "",
        None,
    )
    assert result is False and send.await_count == 1
    assert saved == [] and last_time == [old_time]
    assert archive.get_timeline_watermark("nogizaka46", "1") == old_time


def test_pending_state_unavailable_is_not_an_empty_backlog(monkeypatch):
    monkeypatch.setattr(delivery_state, "_conn", lambda: None)
    with pytest.raises(delivery_state.DeliveryStateError):
        delivery_state.pending_routes_for_member("nogizaka46", "1")
    with pytest.raises(delivery_state.DeliveryStateError):
        delivery_state.successful_routes("nogizaka46", "1", "message")


@pytest.mark.asyncio
async def test_blog_parse_failure_keeps_gap_and_retries(blog_db, monkeypatch):
    db = blog_db
    configure_blog_page(monkeypatch, ["two", "one", "old"])
    original = blog_fetcher._process_single_post

    async def parse(post, *args):
        if post["url"] == "one":
            raise ValueError("bad source content")
        return await original(post, *args)

    monkeypatch.setattr(blog_fetcher, "_process_single_post", parse)
    assert [p["url"] for p in await blog_fetcher.run_blog_cycle(None, db, {})] == ["two"]
    assert blog_fetcher._load_records(db)["nogizaka"] == "old"
    assert not blog_fetcher._in_flight_blogs
    monkeypatch.setattr(blog_fetcher, "_process_single_post", original)
    assert [p["url"] for p in await blog_fetcher.run_blog_cycle(None, db, {})] == ["one"]
    assert blog_fetcher._load_records(db)["nogizaka"] == "two"


@pytest.mark.asyncio
async def test_blog_cancellation_keeps_committed_articles_and_retries_only_gap(blog_db, monkeypatch):
    db = blog_db
    configure_blog_page(monkeypatch, ["two", "one", "old"])
    original = blog_fetcher._process_single_post
    blocked = asyncio.Event()

    async def parse(post, *args):
        if post["url"] == "two":
            blocked.set()
            await asyncio.Event().wait()
        return await original(post, *args)

    monkeypatch.setattr(blog_fetcher, "_process_single_post", parse)
    task = asyncio.create_task(blog_fetcher.run_blog_cycle(None, db, {}))
    await asyncio.wait_for(blocked.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not blog_fetcher._in_flight_blogs
    assert [row[0] for row in db.execute("SELECT url FROM blog_posts")] == ["one"]
    assert blog_fetcher._load_records(db)["nogizaka"] == "old"
    monkeypatch.setattr(blog_fetcher, "_process_single_post", original)
    assert [p["url"] for p in await blog_fetcher.run_blog_cycle(None, db, {})] == ["two"]
    assert blog_fetcher._load_records(db)["nogizaka"] == "two"


@pytest.mark.asyncio
async def test_blog_failed_group_does_not_block_other_group(blog_db, monkeypatch):
    db = blog_db
    blog_fetcher._save_records({"hinatazaka": "hinata-old"}, db)
    monkeypatch.setattr(
        blog_fetcher,
        "TASKS",
        [
            ("nogizaka", AsyncMock(return_value=[{"url": "bad"}, {"url": "old"}]), None, "nogizaka", False),
            ("hinatazaka", AsyncMock(return_value=[{"url": "good"}, {"url": "hinata-old"}]), None, "hinatazaka", False),
        ],
    )
    db.execute(
        "CREATE TRIGGER reject_bad BEFORE INSERT ON blog_posts WHEN NEW.url='bad' BEGIN SELECT RAISE(ABORT,'test group failure'); END"
    )
    assert [p["url"] for p in await blog_fetcher.run_blog_cycle(None, db, {})] == ["good"]
    records = blog_fetcher._load_records(db)
    assert records["nogizaka"] == "old" and records["hinatazaka"] == "good"


@pytest.mark.asyncio
async def test_blog_watermark_read_failure_does_not_bootstrap(blog_db, monkeypatch):
    db = blog_db
    fetch = configure_blog_page(monkeypatch, ["new", "old"])
    db.execute("ALTER TABLE blog_watermarks RENAME TO unavailable_watermarks")
    with pytest.raises(sqlite3.Error):
        await blog_fetcher.run_blog_cycle(None, db, {})
    fetch.assert_not_awaited()
    assert db.execute("SELECT last_url FROM unavailable_watermarks WHERE group_key='nogizaka'").fetchone()[0] == "old"
