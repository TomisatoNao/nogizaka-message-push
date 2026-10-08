"""日向坂46 博客抓取（HTML 列表页解析，httpx async 版）。"""
import re
import httpx
from html import unescape
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from typing import TypedDict

HINATA_LIST_URL = "https://www.hinatazaka46.com/s/official/diary/member?ima=0000"


class ArticleData(TypedDict):
    images: list[str]
    body: str
    body_html: str


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


def _parse_article_body(body: BeautifulSoup | None) -> ArticleData:
    """从同一份正文 DOM 提取图片、HTML 和纯文本，避免多次请求间快照不一致。"""
    if not body:
        return {"images": [], "body": "", "body_html": ""}

    body_html = str(body)
    images = [
        ("https:" + img["src"] if img.get("src", "").startswith("//") else img["src"])
        for img in body.find_all("img")
        if "hinatazaka46.com" in img.get("src", "")
    ]
    counter = [0]

    def _img_placeholder(_match):
        counter[0] += 1
        return f"\n【图片{counter[0]}】\n"

    text = re.sub(r"<img[^>]*>", _img_placeholder, body_html, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text).strip()
    return {"images": images, "body": text, "body_html": body_html}


async def fetch_article(client: httpx.AsyncClient, url: str) -> ArticleData:
    """一次请求并解析文章图片、HTML 与纯文本，确保三者属于同一份页面快照。"""
    try:
        return _parse_article_body(await _get_article(client, url))
    except Exception:
        return {"images": [], "body": "", "body_html": ""}


async def fetch_images(client: httpx.AsyncClient, url: str) -> list[str]:
    try:
        return (await fetch_article(client, url))["images"]
    except Exception:
        return []


async def fetch_html(client: httpx.AsyncClient, url: str) -> str:
    """获取正文原始 HTML（保留 <img>、<br> 等结构标签）。"""
    try:
        return (await fetch_article(client, url))["body_html"]
    except Exception:
        return ""


async def fetch_body(client: httpx.AsyncClient, url: str) -> str:
    """获取正文纯文本，保留 br 换行 + 图片占位符。"""
    try:
        return (await fetch_article(client, url))["body"]
    except Exception:
        return ""
