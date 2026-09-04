"""Regression tests for Instagram anonymous public-post handling."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.social import ig_session
from src.social.downloader import MediaDownloader
from src.social.instagram_embed import _extract_page_data
from src.social.models import MediaItem, Post
from src.social.single_fetcher import InstagramAuthRequired, SocialUrlParser
from src.social.fetchers.instagram_fetcher import InstagramFetcher
from src.social.store import SocialStore


def test_public_post_uses_embed_after_private_api_rejection(monkeypatch, caplog):
    # Keep the regression deterministic in CI, where no local SQLite session
    # exists.  This case specifically verifies the private-API rejection
    # fallback, so provide the session that makes that branch reachable.
    monkeypatch.setattr(ig_session, "resolve_cookies", lambda *args: {"sessionid": "session"})
    parser = SocialUrlParser({"platforms": {"instagram": {}}})
    expected = Post(
        platform="instagram",
        post_id="DbNlqsAFPDm",
        author="public_user",
        media=[MediaItem(type="image", url="https://scontent.cdninstagram.com/a.jpg")],
        extra={"source": "public_embed", "auth": "anonymous"},
    )

    def rejected(_shortcode, _url):
        raise InstagramAuthRequired("HTTP 403")

    monkeypatch.setattr(parser, "_parse_instagram_api", rejected)
    monkeypatch.setattr(parser, "_parse_instagram_embed", lambda _url: expected)
    caplog.set_level(logging.DEBUG, logger="collink")

    result = parser.parse("https://www.instagram.com/p/DbNlqsAFPDm/?img_index=1")

    assert result is expected
    assert "转公开 Embed" in caplog.text


def test_story_without_session_does_not_fall_back_to_ytdlp(monkeypatch):
    parser = SocialUrlParser()
    monkeypatch.setattr(ig_session, "resolve_cookies", lambda *args: {})

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("匿名 Story 不应调用后续解析器")

    monkeypatch.setattr(parser, "_parse_instagram_story", should_not_run)
    monkeypatch.setattr(parser, "_extract_with_ytdlp", should_not_run)

    with pytest.raises(InstagramAuthRequired, match="需要有效登录 Cookies"):
        parser.parse("https://www.instagram.com/stories/example_user/123/")


def test_story_api_failure_is_reported_as_auth_boundary(monkeypatch):
    parser = SocialUrlParser()
    monkeypatch.setattr(ig_session, "resolve_cookies", lambda *args: {"sessionid": "session"})
    monkeypatch.setattr(
        parser,
        "_parse_instagram_story",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 403")),
    )

    with pytest.raises(RuntimeError, match="Story 官方接口暂时失败"):
        parser.parse("https://www.instagram.com/stories/example_user/123/")


def test_temp_cookie_file_is_netscape_and_removed(tmp_path: Path):
    cookies = {"sessionid": "sid", "csrftoken": "csrf"}
    with ig_session.temporary_cookie_file(cookies) as path:
        cookie_path = Path(path)
        assert cookie_path.exists()
        lines = cookie_path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# Netscape HTTP Cookie File"
        assert any(line.endswith("\tsessionid\tsid") for line in lines[1:])
    assert not cookie_path.exists()


def test_ytdlp_options_never_use_raw_cookie_header(monkeypatch):
    monkeypatch.setattr(ig_session, "resolve_cookies", lambda *args: {"sessionid": "sid"})
    downloader = MediaDownloader({})
    opts = downloader.base_ydl_opts({"_platform": "instagram"})

    assert "Cookie" not in opts.get("http_headers", {})
    temp = opts.get("_collink_temp_cookiefile")
    assert temp and Path(temp).exists()
    downloader._take_temp_cookiefile(opts)
    ig_session.remove_temp_cookie_file(temp)
    assert not Path(temp).exists()


class _FakeLocator:
    def __init__(self, values, attrs=None):
        self.values = values
        self.attrs = attrs or {}

    def evaluate_all(self, _script):
        return self.values

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.attrs else len(self.values)

    def get_attribute(self, name):
        return self.attrs.get(name, "")


class _FakePage:
    def __init__(self):
        self.locators = {
            "img": _FakeLocator([
                {
                    "src": "https://scontent.cdninstagram.com/avatar.jpg?s100x100",
                    "srcset": "",
                    "alt": "public_user",
                    "className": "",
                    "width": 100,
                    "height": 100,
                },
                {
                    "src": "https://scontent.cdninstagram.com/low.jpg",
                    "srcset": "https://scontent.cdninstagram.com/low.jpg 640w, https://scontent.cdninstagram.com/high.jpg 1440w",
                    "alt": "public photo",
                    "className": "EmbeddedMediaImage",
                    "width": 1440,
                    "height": 1000,
                },
                {
                    "src": "https://scontent.cdninstagram.com/thumb.jpg?s150x150",
                    "srcset": "",
                    "alt": "",
                    "className": "",
                    "width": 150,
                    "height": 150,
                }
            ]),
            "video": _FakeLocator([]),
            "a": _FakeLocator([
                {"href": "https://www.instagram.com/public_user/", "text": "Public User"}
            ]),
            'meta[property="og:image"]': _FakeLocator([], {"content": ""}),
            'meta[name="twitter:image"]': _FakeLocator([], {"content": ""}),
            'meta[property="og:description"]': _FakeLocator([], {"content": "A public caption"}),
            'meta[name="description"]': _FakeLocator([], {"content": ""}),
            'meta[property="article:published_time"]': _FakeLocator([], {"content": "2026-09-02T10:00:00+00:00"}),
        }

    def locator(self, selector):
        return self.locators[selector]


def test_embed_dom_extraction_prefers_largest_cdn_image():
    media, author, username, caption, timestamp = _extract_page_data(_FakePage(), max_media=20)

    assert len(media) == 1
    assert media[0].url.endswith("/high.jpg")
    assert media[0].type == "image"
    assert author == "Public User"
    assert username == "public_user"
    assert caption == "A public caption"
    assert timestamp == "2026-09-02 10:00:00"


class _FeedDownloader:
    def extract_info(self, *_args, **_kwargs):
        return None

    def download_many(self, tasks, referer=""):
        paths = []
        for _url, target in tasks:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake-image")
            paths.append(str(path))
        return paths

    def download_via_ytdlp(self, *_args, **_kwargs):
        return []


def test_scheduled_feed_uses_embed_when_detail_extraction_fails(tmp_path, monkeypatch):
    config = {
        "platforms": {
            "instagram": {
                "enabled": True,
                "accounts": ["public_user"],
                "download_dir": str(tmp_path / "media"),
            }
        }
    }
    expected = Post(
        platform="instagram",
        post_id="shortcode",
        author="Public User",
        text="caption",
        media=[MediaItem(type="image", url="https://scontent.cdninstagram.com/photo.jpg")],
        timestamp="2026-09-02 10:00:00",
    )
    monkeypatch.setattr(
        "src.social.instagram_embed.fetch_public_post",
        lambda *_args, **_kwargs: expected,
    )
    fetcher = InstagramFetcher(
        config,
        SocialStore(str(tmp_path / "social.db")),
        _FeedDownloader(),
    )

    post = fetcher._build_feed_post(
        "public_user",
        {"id": "shortcode", "url": "https://www.instagram.com/p/shortcode/", "kind": "post"},
    )

    assert post is not None
    assert post.text == "caption"
    assert len(post.media) == 1
    assert post.media[0].local_path and Path(post.media[0].local_path).exists()


def test_instagram_anonymous_429_graceful_handling(tmp_path: Path):
    from src.social.ig_safety import get_guard
    guard = get_guard()
    guard.reset()

    config = {
        "platforms": {
            "instagram": {
                "safety": {"failure_threshold": 3, "rate_limit_cooldown_seconds": 600}
            }
        }
    }

    # 1. 验证匿名模式（has_session=False）遇到 429 时，立即触发静默退避（返回 True），不需要连撞 3 次
    triggered = guard.record_failure(config, 429, has_session=False)
    assert triggered is True
    assert guard.status(config)["blocked"] is True
    assert "匿名限流" in guard._block_reason

    guard.reset()

    # 2. 验证有会话模式（has_session=True）遇到 429 时，仍需达到阈值 (3 次)
    assert guard.record_failure(config, 429, has_session=True) is False
    assert guard.record_failure(config, 429, has_session=True) is False
    assert guard.record_failure(config, 429, has_session=True) is True
    assert guard.status(config)["blocked"] is True
