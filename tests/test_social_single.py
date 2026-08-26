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
