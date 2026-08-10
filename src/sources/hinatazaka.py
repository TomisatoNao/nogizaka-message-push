"""日向坂46 博客抓取（HTML 列表页解析，httpx async 版）。"""
import re
import httpx
from html import unescape
from bs4 import BeautifulSoup
from urllib.parse import urljoin

HINATA_LIST_URL = "https://www.hinatazaka46.com/s/official/diary/member?ima=0000"


async def fetch_posts(client: httpx.AsyncClient, limit: int = 30) -> list[dict]:
    try:
        r = await client.get(HINATA_LIST_URL)
        soup = BeautifulSoup(r.text, "html.parser")
        posts = []
        for item in soup.find_all("li", class_="p-blog-top__item", limit=limit):
            a_tag = item.find("a")
            if not a_tag:
                continue
            t_tag = item.find("time", class_="c-blog-top__date")
            posts.append({
                "url":    urljoin("https://www.hinatazaka46.com", a_tag.get("href", "")),
                "title":  item.find("p",   class_="c-blog-top__title").text.strip(),
                "author": item.find("div", class_="c-blog-top__name").text.strip(),
                "date":   t_tag.text.strip() if t_tag else "",
            })
        return posts
    except Exception:
        return []


async def _get_article(client: httpx.AsyncClient, url: str) -> BeautifulSoup | None:
    r = await client.get(url)
    return BeautifulSoup(r.text, "html.parser").find("div", class_="c-blog-article__text")


def fetch_images(client: httpx.AsyncClient, url: str) -> list[str]:
    """同步包装：日向坂图片抓取（图片 URL 可直接从页面获取，无需下载）"""
    import asyncio
    return asyncio.run(_fetch_images(client, url))


async def _fetch_images(client: httpx.AsyncClient, url: str) -> list[str]:
    try:
        body = await _get_article(client, url)
        if not body:
            return []
        return [
            ("https:" + img["src"] if img.get("src", "").startswith("//") else img["src"])
            for img in body.find_all("img")
            if "hinatazaka46.com" in img.get("src", "")
        ]
    except Exception:
        return []


async def fetch_body(client: httpx.AsyncClient, url: str) -> str:
    """获取正文纯文本，保留 br 换行 + 图片占位符。"""
    try:
        body = await _get_article(client, url)
        if not body:
            return ""
        html_str = str(body)
        _counter = [0]

        def _img_placeholder(m):
            _counter[0] += 1
            return f"\n【图片{_counter[0]}】\n"
        text = re.sub(r"<img[^>]*>", _img_placeholder, html_str, flags=re.IGNORECASE)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
        return text.strip()
    except Exception:
        return ""
