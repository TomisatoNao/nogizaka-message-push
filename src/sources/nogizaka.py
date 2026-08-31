import itertools
import json
import re
from html import unescape
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import httpx

NOGI_API_URL = "https://www.nogizaka46.com/s/n46/api/list/blog"
NOGI_HEADERS = {"accept": "application/json", "x-requested-with": "XMLHttpRequest"}


def _parse_jsonp(text: str) -> dict:
    """去掉 JSONP wrapper 后解析为 dict。"""
    try:
        text = re.sub(r"^\w+\(", "", text).rstrip(");")
    except (TypeError, ValueError):
        pass
    return json.loads(text)


async def fetch_posts(client: httpx.AsyncClient, limit: int = 30) -> list[dict]:
    """乃木坂的 images + body 内嵌在列表 API 响应中，无需二次抓取。"""
    try:
        r = await client.get(NOGI_API_URL, headers=NOGI_HEADERS)
        data = _parse_jsonp(r.text)
        posts = []
        for item in data.get("data", [])[:limit]:
            code = item.get("code", "")
            url = f"https://www.nogizaka46.com/s/n46/diary/detail/{code}?ima=0000&cd=MEMBER"
            raw_html = item.get("text", "")
            soup = BeautifulSoup(raw_html, "html.parser")
            images = [
                urljoin("https://www.nogizaka46.com", img["src"])
                for img in soup.find_all("img") if img.get("src")
            ]
            img_counter = itertools.count(1)
            body_text = re.sub(
                r"<img[^>]*>",
                lambda _, c=img_counter: f"\n【图片{next(c)}】\n",
                raw_html,
                flags=re.IGNORECASE,
            )
            body_text = re.sub(r"<br\s*/?>", "\n", body_text, flags=re.IGNORECASE)
            body_text = re.sub(r"<[^>]+>", "", body_text)
            body_text = unescape(body_text).strip()
            posts.append({
                "url": url, "title": item.get("title", "无标题"),
                "author": item.get("name", "乃木坂46成员"),
                "images": images, "date": item.get("date", ""),
                "body": body_text, "body_html": raw_html,
            })
        return posts
    except Exception:
        return []
