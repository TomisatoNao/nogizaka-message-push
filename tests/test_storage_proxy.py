# ============================================================
# test_storage_proxy.py — 存储监控与网络代理单元测试
# ============================================================
import json
import os
import unittest
from http.server import HTTPServer
from pathlib import Path
import sys
from threading import Thread
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils import format_bytes, get_storage_breakdown
import config.config as cfg
from src.webui import _Handler


class TestStorageProxy(unittest.TestCase):

    def test_format_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(1048576), "1.0 MB")
        self.assertEqual(format_bytes(1073741824), "1.0 GB")
        self.assertEqual(format_bytes(1099511627776), "1.0 TB")

    def test_get_storage_breakdown(self):
        st = get_storage_breakdown(force_refresh=True)
        self.assertIn("disk", st)
        self.assertIn("total_bytes", st["disk"])
        self.assertIn("free_bytes", st["disk"])
        self.assertIn("used_percent", st["disk"])

        self.assertIn("app_total", st)
        self.assertIn("bytes", st["app_total"])
        self.assertIn("human", st["app_total"])

        self.assertIn("categories", st)
        for cat in ("message_media", "blog_images", "social_media", "live_recordings", "databases", "logs"):
            self.assertIn(cat, st["categories"])
            self.assertIn("bytes", st["categories"][cat])
            self.assertIn("human", st["categories"][cat])
            self.assertIn("count", st["categories"][cat])

    def test_proxy_config_resolution(self):
        # 确保 PROXY 属性在 config 模块中存在
        self.assertTrue(hasattr(cfg, "PROXY"))
        # 模拟环境变量回退
        orig_proxy = os.environ.get("HTTP_PROXY")
        try:
            os.environ["HTTP_PROXY"] = "http://127.0.0.1:8888"
            c = cfg._load_config()
            self.assertIn("proxy", c)
            self.assertTrue(len(c["proxy"]) > 0)
        finally:
            if orig_proxy is not None:
                os.environ["HTTP_PROXY"] = orig_proxy
            else:
                os.environ.pop("HTTP_PROXY", None)

    def test_webui_storage_and_proxy_endpoints(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        t = Thread(target=server.serve_forever, daemon=True)
        t.start()

        orig_auth = getattr(cfg, "AUTH_ENABLED", False)
        cfg.AUTH_ENABLED = False

        try:
            # 1. 测试 GET /api/system/storage
            url_storage = f"http://127.0.0.1:{port}/api/system/storage"
            req = urllib.request.Request(url_storage)
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data.get("ok"))
                self.assertIn("storage", data)
                self.assertIn("disk", data["storage"])

            # 2. 测试 POST /api/system/proxy/test
            url_proxy = f"http://127.0.0.1:{port}/api/system/proxy/test"
            payload = json.dumps({"proxy": ""}).encode("utf-8")
            req = urllib.request.Request(url_proxy, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data.get("ok"))
                self.assertIn("results", data)
                self.assertEqual(len(data["results"]), 4)

            # 3. 测试 POST /api/system/storage/clean
            url_clean = f"http://127.0.0.1:{port}/api/system/storage/clean"
            payload_clean = json.dumps({"category": "live_recordings"}).encode("utf-8")
            req = urllib.request.Request(url_clean, data=payload_clean, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data.get("ok"))
                self.assertIn("msg", data)

        finally:
            cfg.AUTH_ENABLED = orig_auth
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
