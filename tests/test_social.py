import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.social.models import Post, MediaItem
from src.social.store import SocialStore
from src.social.formatter import build_post_message, collect_alts
from src.social.downloader import MediaDownloader
from src.social.fetchers.x_fetcher import XFetcher
from src.social.fetchers.instagram_fetcher import InstagramFetcher
from src.social.fetchers.tiktok_fetcher import TikTokFetcher
from src.social.fetchers.tiktok_live_fetcher import TikTokLiveFetcher
from src.social.manager import start_social_service, stop_social_service


class TestSocialIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_social.db")
        self.store = SocialStore(self.db_path)
        self.config = {
            "platforms": {
                "x": {
                    "enabled": True,
                    "interval_seconds": 60,
                    "accounts": ["nogizaka46"],
                    "display_names": {"nogizaka46": "乃木坂46公式"}
                },
                "instagram": {
                    "enabled": False,
                    "interval_seconds": 1800,
                    "accounts": ["tomisato_nao_official"]
                },
                "tiktok": {
                    "enabled": False,
                    "interval_seconds": 120,
                    "accounts": ["nogizaka46_official"]
                },
                "tiktok_live": {
                    "enabled": False,
                    "interval_seconds": 8,
                    "accounts": ["nogizaka46_official"]
                }
            }
        }
        self.downloader = MediaDownloader(self.config)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_post_and_store(self):
        post = Post(
            platform="x",
            post_id="tweet_123456",
            author="乃木坂46公式",
            text="こんにちは！乃木坂46です。",
            media=[MediaItem(type="image", url="https://example.com/pic.jpg", alt_text="集合写真")]
        )
        self.assertEqual(post.platform, "x")
        self.assertFalse(self.store.is_sent("x", "tweet_123456"))
        self.store.mark_sent("x", "tweet_123456", account="nogizaka46")
        self.assertTrue(self.store.is_sent("x", "tweet_123456"))

    def test_formatter(self):
        post = Post(
            platform="x",
            post_id="tweet_789",
            author="冨里奈央",
            text="今日はお散歩したよ🐾",
            media=[MediaItem(type="image", url="https://example.com/nao.jpg", alt_text="公園のベンチ")]
        )
        alts = collect_alts(post)
        self.assertEqual(len(alts), 1)
        self.assertEqual(alts[0][1], "公園のベンチ")
        msg = build_post_message(post, translated="今天去散步了哦🐾", alt_translations={1: "公园的长椅"})
        self.assertIn("平台：X", msg)
        self.assertIn("作者：冨里奈央", msg)
        self.assertIn("今日はお散歩したよ🐾", msg)
        self.assertIn("今天去散步了哦🐾", msg)
        self.assertIn("[图1] 公园的长椅", msg)

    def test_fetcher_instantiations(self):
        x = XFetcher(self.config, self.store, self.downloader)
        ig = InstagramFetcher(self.config, self.store, self.downloader)
        tt = TikTokFetcher(self.config, self.store, self.downloader)
        ttl = TikTokLiveFetcher(self.config, self.store, self.downloader)

        self.assertTrue(x.is_enabled)
        self.assertEqual(x.accounts, ["nogizaka46"])
        self.assertEqual(x.display_name("nogizaka46"), "乃木坂46公式")
        self.assertFalse(ig.is_enabled)
        self.assertFalse(tt.is_enabled)
        self.assertFalse(ttl.is_enabled)

    def test_service_manager_lifecycle(self):
        s = start_social_service(self.config)
        self.assertIsNotNone(s)
        stop_social_service()

    def test_social_forwarder_pubsub(self):
        from src.social.forwarder import SocialForwarder
        cfg_test = {
            "channels": {"napcat": False, "tg": False, "qq_official": False},
            "napcat_routes": [
                {"group_id": 111, "push_x": True, "social_filter": ["nogizaka46"]},
                {"group_id": 222, "push_x": False, "social_filter": []},
            ],
            "tg_bots": [],
            "qq_official_bots": []
        }
        fwd = SocialForwarder(cfg_test, self.downloader)
        post = Post(
            platform="x",
            post_id="p1",
            author="nogizaka46",
            text="hello",
            extra={"account": "nogizaka46"}
        )
        fwd.forward_post(post)
        self.assertTrue(True)

    def test_social_single_url_parser(self):
        from src.social.single_fetcher import SocialUrlParser, _orig_image, _syndication_token
        self.assertEqual(_orig_image("https://pbs.twimg.com/media/xyz.jpg"), "https://pbs.twimg.com/media/xyz?format=jpg&name=orig")
        token = _syndication_token("1234567890")
        self.assertTrue(isinstance(token, str) and len(token) > 0)

        parser = SocialUrlParser(self.config)
        with self.assertRaises(ValueError):
            parser.parse("https://unknown-platform.com/xyz")

        with self.assertRaises(ValueError):
            parser.parse("")


if __name__ == "__main__":
    unittest.main()

