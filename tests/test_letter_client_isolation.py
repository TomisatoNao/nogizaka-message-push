"""Letter synchronization must not replace the monitor's HTTP client or locks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src import archive
import src.config.config as cfg
from src.webui_modules.archive.letters import handle_letters
from tools import archive_letters


@pytest.fixture
def isolated_archive(monkeypatch, tmp_path):
    archive.close_db()
    monkeypatch.setattr(cfg, "ARCHIVE_DIR", str(tmp_path))
    background = SimpleNamespace(is_closed=False, get=Mock(side_effect=AssertionError("Shared client used")))
    background_sem = object()
    monkeypatch.setattr(archive, "_media_client", background)
    monkeypatch.setattr(archive, "_media_sem", background_sem)
    monkeypatch.setattr(archive, "sync_all_to_sqlite", Mock(side_effect=AssertionError("Unexpected reindex")))
    yield background, background_sem
    archive.close_db()


def letter(number):
    return {"id": number, "file": f"https://fixture.invalid/{number}.jpg",
            "created_at": "2026-10-23T01:00:00Z"}


@pytest.mark.asyncio
async def test_batch_uses_owned_client_and_local_concurrency(isolated_archive):
    background, background_sem = isolated_archive
    active = peak = 0
    seen = []

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.append(request.headers["Authorization"])
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, content=b"\xff\xd8fixture-image\xff\xd9")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await archive.archive_letters_batch(
            "fixture", [letter(i) for i in range(1, 8)],
            headers={"Authorization": "Bearer fixture"}, client=client,
        )
        assert not client.is_closed
    assert len(result) == 7 and all(item.get("local_file") for item in result)
    assert 1 < peak <= 3
    assert seen == ["Bearer fixture"] * 7
    assert archive._media_client is background
    assert archive._media_sem is background_sem
    background.get.assert_not_called()


@pytest.mark.asyncio
async def test_batch_cancellation_preserves_background_dependencies(isolated_archive):
    background, background_sem = isolated_archive

    async def respond(request):
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(asyncio.CancelledError):
            await archive.archive_letters_batch("fixture", [letter(1)], client=client)
    assert client.is_closed
    assert archive._media_client is background
    assert archive._media_sem is background_sem


@pytest.mark.asyncio
async def test_closed_explicit_client_does_not_create_fallback(isolated_archive, monkeypatch):
    closed_client = SimpleNamespace(is_closed=True, get=Mock())
    monkeypatch.setattr(httpx, "AsyncClient", Mock(side_effect=AssertionError("Fallback client created")))

    result = await archive.archive_single_letter("fixture", letter(1), client=closed_client)

    assert "local_file" not in result
    assert result["_media_download_failed"] is True
    closed_client.get.assert_not_called()


@pytest.mark.parametrize("fail", [False, True])
def test_web_sync_closes_own_client_without_reinitializing(monkeypatch, isolated_archive, fail):
    background, background_sem = isolated_archive
    real_client = httpx.AsyncClient
    clients = []

    def make_client(**kwargs):
        client = real_client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"\xff\xd8image\xff\xd9")), **kwargs)
        clients.append(client)
        return client

    async def fetch(member, client):
        if fail:
            raise OSError("fixture fetch failure")
        return [letter(1)]

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    monkeypatch.setattr(archive_letters, "fetch_member_letters", fetch)
    monkeypatch.setattr(archive_letters, "get_source_headers_for_account", lambda *a: {})
    monkeypatch.setattr(cfg, "MONITOR_LIST", [])
    handler = SimpleNamespace(path="/api/archive/letters_sync?member=fixture", _send_json=Mock())
    assert handle_letters(handler, "letters_sync", lambda **_: True, lambda: {})
    assert handler._send_json.call_args.args[1] == (500 if fail else 200)
    assert len(clients) == 1 and clients[0].is_closed
    assert archive._media_client is background
    assert archive._media_sem is background_sem
    archive.sync_all_to_sqlite.assert_not_called()


@pytest.mark.asyncio
async def test_nonempty_truncated_letter_image_is_downloaded_again(isolated_archive):
    member_dir = archive.member_dir_name("fixture")
    image_dir = archive.archive_root() / member_dir / "letters"
    image_dir.mkdir(parents=True)
    target = image_dir / f"{archive.parse_jst_datetime(letter(9)['created_at']).strftime('%Y%m%d_%H%M%S')}_9.jpg"
    target.write_bytes(b"\xff\xd8truncated")
    replacement = b"\xff\xd8complete-image\xff\xd9"

    async def respond(_request):
        return httpx.Response(200, content=replacement)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        archived = await archive.archive_single_letter("fixture", letter(9), client=client)

    assert target.read_bytes() == replacement
    assert archived["local_file"].endswith("_9.jpg")
