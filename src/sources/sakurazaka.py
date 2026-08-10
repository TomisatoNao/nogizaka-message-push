"""樱坂46 博客抓取（HTML 列表页 + 详情页，httpx async 版）。"""
import re
import httpx
from html import unescape
from bs4 import BeautifulSoup
from urllib.parse import urljoin

SAKURA_LIST_URL = "https://sakurazaka46.com/s/s46/diary/blog/list?ima=0000"


def _parse_date(raw: str) -> str:
    """统一樱坂各处日期格式。"""
    raw = raw.strip()
    m = re.match(r"(\d{4})(\d{2})(\d{2})\s+(\d{2})(\d{2})$", raw)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)} {m.group(4)}:{m.group(5)}"
    m2 = re.match(r"(\d{4})(\d{2})(\d{2})$", raw)
    if m2:
        return f"{m2.group(1)}/{m2.group(2)}/{m2.group(3)}"
    m3 = re.match(r"(\d{4})/(\d{1,2})/(\d{2})$", raw)
    if m3:
        return f"{m3.group(1)}/{int(m3.group(2)):02d}/{m3.group(3)}"
    return raw


async def fetch_posts(client: httpx.AsyncClient, limit: int = 30) -> list[dict]:
    try:
        r = await client.get(SAKURA_LIST_URL)
        soup = BeautifulSoup(r.text, "html.parser")
        container = soup.find("ul", class_="com-blog-part")
        if not container:
            return []
        posts = []
        for item in container.find_all("li", class_="box", limit=limit):
            a_tag = item.find("a")
            if not a_tag:
                continue
            d_tag = item.find("p", class_="date")
            posts.append({
                "url":    urljoin("https://sakurazaka46.com", a_tag.get("href", "")),
                "title":  item.find("h3", class_="title").text.strip(),
                "author": item.find("p",  class_="name").text.strip(),
                "date":   d_tag.text.strip() if d_tag else "",
            })
        return posts
    except Exception:
        return []


async def fetch_images(client: httpx.AsyncClient, url: str) -> list[str]:
    imgs, _, _ = await fetch_detail(client, url)
    return imgs


async def fetch_detail(client: httpx.AsyncClient, url: str) -> tuple[list[str], str, str]:
    """返回 (图片列表, 精确发送时间, 正文纯文本)。"""
    try:
        r = await client.get(url)
        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.find("div", class_="box-article") or soup
        imgs = [
            urljoin("https://sakurazaka46.com", img["src"])
            for img in body.find_all("img") if img.get("src")
        ]
        body_text_raw = str(body)
        _counter = [0]

        def _img_placeholder(m):
            _counter[0] += 1
            return f"\n【图片{_counter[0]}】\n"
        body_text = re.sub(r"<img[^>]*>", _img_placeholder, body_text_raw, flags=re.IGNORECASE)
        body_text = re.sub(r"<br\s*/?>", "\n", body_text, flags=re.IGNORECASE)
        body_text = re.sub(r"<[^>]+>", "", body_text)
        body_text = unescape(body_text).strip()
        foot = soup.find("div", class_="blog-foot")
        date_str = ""
        if foot:
            d_tag = foot.find("p", class_="date")
            if d_tag:
                date_str = _parse_date(d_tag.text.strip())
        return imgs, date_str, body_text
    except Exception:
        return [], "", ""
