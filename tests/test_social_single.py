import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.social.single_fetcher import SocialUrlParser, _orig_image, _shortcode_to_media_id, _syndication_token


def test_twitter_utils():
    # Token calculation
    token = _syndication_token("1234567890")
    assert isinstance(token, str) and len(token) > 0

    # Orig image rewrite
    img1 = "https://pbs.twimg.com/media/Gabcdef.jpg"
    assert _orig_image(img1) == "https://pbs.twimg.com/media/Gabcdef?format=jpg&name=orig"

    card_img = "https://pbs.twimg.com/card_img/123/abc.jpg"
    assert _orig_image(card_img) == "https://pbs.twimg.com/card_img/123/abc.jpg?name=large"


def test_instagram_shortcode():
    media_id = _shortcode_to_media_id("B_abcdef")
    assert isinstance(media_id, int)
    assert media_id > 0


def test_social_url_parser_routing():
    parser = SocialUrlParser()

    # Empty URL error
    import pytest
    with pytest.raises(ValueError):
        parser.parse("")

    # Unsupported domain
    with pytest.raises(ValueError):
        parser.parse("https://youtube.com/watch?v=123")


def test_instagram_story_mocked(monkeypatch):
    from unittest.mock import MagicMock
    import requests
    from src.social import ig_session

    parser = SocialUrlParser()
    # Story is intentionally an authenticated-only path; provide a synthetic
    # session so this API fixture exercises the parser rather than the auth
    # boundary.
    monkeypatch.setattr(ig_session, "resolve_cookies", lambda *args: {"sessionid": "test-session"})

    # Mock user id lookup
    def mock_get(self, url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "feed/user/suzuno_mio/username" in url:
            resp.json.return_value = {
                "user": {
                    "pk": 12345678,
                    "username": "suzuno_mio",
                    "full_name": "鈴野美央",
                    "profile_pic_url": "https://ig.cdn/avatar.jpg"
                }
            }
        elif "reels_media" in url:
            resp.json.return_value = {
                "reels": {
                    "12345678": {
                        "items": [
                            {
                                "pk": "3974361302645425079",
                                "media_type": 1,
                                "taken_at": 1700000000,
                                "image_versions2": {
                                    "candidates": [{"url": "https://ig.cdn/img1.jpg", "width": 1080, "height": 1920}]
                                }
                            },
                            {
                                "pk": "3974361302645425080",
                                "media_type": 1,
                                "taken_at": 1700000010,
                                "image_versions2": {
                                    "candidates": [{"url": "https://ig.cdn/img2.jpg", "width": 1080, "height": 1920}]
                                }
                            },
                            {
                                "pk": "3974361302645425081",
                                "media_type": 1,
                                "taken_at": 1700000020,
                                "image_versions2": {
                                    "candidates": [{"url": "https://ig.cdn/img3.jpg", "width": 1080, "height": 1920}]
                                }
                            }
                        ]
                    }
                }
            }
        return resp

    monkeypatch.setattr(requests.Session, "get", mock_get)
    post = parser.parse("https://www.instagram.com/stories/suzuno_mio/3974361302645425080/")

    assert post.platform == "instagram"
    assert post.author == "鈴野美央"
    assert len(post.media) == 3
    assert all(m.type == "image" for m in post.media)
    assert post.media[0].url == "https://ig.cdn/img1.jpg"
    assert post.media[1].url == "https://ig.cdn/img2.jpg"
    assert post.media[2].url == "https://ig.cdn/img3.jpg"
    assert post.extra.get("story_count") == 3


def test_instagram_post_url_patterns():
    from src.social.instagram_embed import _post_parts, _SHORTCODE_RE
    import re

    # Test _SHORTCODE_RE on various Instagram link formats
    urls = [
        ("https://www.instagram.com/p/Dc56WSAoHrh/", "p", "Dc56WSAoHrh"),
        ("https://www.instagram.com/p/Dc56WSAoHrh/?img_index=5", "p", "Dc56WSAoHrh"),
        ("https://www.instagram.com/miku_osushi/p/Dc56WSAoHrh/?img_index=5", "p", "Dc56WSAoHrh"),
        ("https://www.instagram.com/miku_osushi/reel/Dc56WSAoHrh/", "reel", "Dc56WSAoHrh"),
        ("https://www.instagram.com/reel/Dc56WSAoHrh/", "reel", "Dc56WSAoHrh"),
        ("https://www.instagram.com/share/p/Dc56WSAoHrh/", "p", "Dc56WSAoHrh"),
        ("https://www.instagram.com/share/reel/Dc56WSAoHrh/", "reel", "Dc56WSAoHrh"),
    ]

    for url, exp_kind, exp_code in urls:
        kind, code, embed_url = _post_parts(url)
        assert kind == exp_kind
        assert code == exp_code
        assert embed_url == f"https://www.instagram.com/{exp_kind}/{exp_code}/embed/captioned/"

        # Also test regex used in single_fetcher
        m = re.search(r"instagram\.com/(?:[a-zA-Z0-9_.]+/)?(?:p|reel|tv)/([^/?#\s]+)", url)
        assert m is not None
        assert m.group(1) == exp_code

