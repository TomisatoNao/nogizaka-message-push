import asyncio
from types import SimpleNamespace

from src.sources import hinatazaka


def test_fetch_article_extracts_all_fields_from_one_response():
    class FakeClient:
        calls = 0

        async def get(self, url):
            self.calls += 1
            assert url.endswith("/detail/51640")
            return SimpleNamespace(text="""
                <html><body>
                  <div class="c-blog-article__text">
                    <p>こんにちは</p>
                    <img src="//cdn.hinatazaka46.com/images/own.jpg">
                    <br>本文です
                  </div>
                  <img src="https://cdn.hinatazaka46.com/images/sidebar.jpg">
                </body></html>
            """)

    client = FakeClient()
    result = asyncio.run(hinatazaka.fetch_article(client, "https://example.test/detail/51640"))

    assert client.calls == 1
    assert result["images"] == ["https://cdn.hinatazaka46.com/images/own.jpg"]
    assert "【图片1】" in result["body"]
    assert "本文です" in result["body"]
    assert "own.jpg" in result["body_html"]
    assert "sidebar.jpg" not in result["body_html"]


def test_hinatazaka_blog_processing_uses_combined_article_fetch(monkeypatch):
    from src import blog_fetcher

    calls = []

    async def fetch_article(_client, url):
        calls.append(url)
        return {"images": [], "body": "正文文本", "body_html": ""}

    monkeypatch.setattr(blog_fetcher.hinatazaka, "fetch_article", fetch_article)

    result = asyncio.run(blog_fetcher._process_single_post(
        {"url": "https://example.test/detail/51640", "images": []},
        "hinatazaka",
        "日向坂46",
        object(),
        False,
        lambda *_: None,
    ))

    assert calls == ["https://example.test/detail/51640"]
    assert result["body"] == "正文文本"
    assert result["body_html"] == ""
