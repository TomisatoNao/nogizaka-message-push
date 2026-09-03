"""Regression tests for Instagram direct-download recovery."""

from __future__ import annotations

from pathlib import Path

from src.social.downloader import MediaDownloader
from src.social.models import MediaItem, Post


def _image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xfffake-jpeg")


class _PartialDownloader(MediaDownloader):
    """Make the first direct pass and fallback pass deterministic."""

    def download_many(self, tasks, *, referer=""):
        paths = []
        for _idx, (_url, target) in enumerate(tasks):
            if _idx < 3:
                _image(Path(target))
                paths.append(target)
        return paths

    def download_via_ytdlp(self, _url, dest_dir, **_kwargs):
        paths = []
        for idx in range(3):
            path = Path(dest_dir) / f"fallback-{idx + 1}.jpg"
            _image(path)
            paths.append(str(path))
        return paths

    def ensure_mobile_video_compatibility(self, files):
        return files


def test_fallback_files_are_assigned_only_to_unresolved_media(tmp_path):
    post = Post(
        platform="instagram",
        post_id="carousel",
        author="public_user",
        media=[
            MediaItem(type="image", url=f"https://cdn.example/{idx}.jpg")
            for idx in range(6)
        ],
        extra={"source_url": "https://www.instagram.com/p/carousel/"},
    )

    _PartialDownloader({"media": {"download_dir": str(tmp_path)}}).download(post)

    paths = [Path(item.local_path) for item in post.media]
    assert all(path.exists() for path in paths)
    assert len({path.resolve() for path in paths}) == 6
    assert [path.name for path in paths[:3]] == [
        "carousel_1.jpg", "carousel_2.jpg", "carousel_3.jpg"
    ]
    assert [path.name for path in paths[3:]] == [
        "fallback-1.jpg", "fallback-2.jpg", "fallback-3.jpg"
    ]


def test_instagram_batch_warms_anonymous_headers_once(monkeypatch):
    downloader = MediaDownloader({})
    calls = []

    monkeypatch.setattr(
        downloader,
        "_instagram_public_headers",
        lambda: {"Cookie": "mid=anonymous", "Accept": "image/*"},
    )

    def fake_direct(url, dest, *, referer="", headers=None):
        calls.append((url, dest, referer, headers))
        return True

    monkeypatch.setattr(downloader, "download_direct", fake_direct)
    tasks = [
        ("https://scontent.cdninstagram.com/a.jpg", "a.jpg"),
        ("https://scontent.cdninstagram.com/b.jpg", "b.jpg"),
    ]

    assert downloader.download_many(tasks, referer="https://www.instagram.com/") == [
        "a.jpg", "b.jpg"
    ]
    assert len(calls) == 2
    assert all(call[2] == "https://www.instagram.com/" for call in calls)
    assert all(call[3] == {"Cookie": "mid=anonymous", "Accept": "image/*"} for call in calls)
