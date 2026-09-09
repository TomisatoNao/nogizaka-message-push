"""三团官方博客全量回填工具。

脚本支持按团体顺序执行历史归档。WebUI 会显式传入 ``--download-images``，
将正文、远程图片 URL 和本地图片路径一起保存；不传该参数时保留旧版
“只抓正文与 URL”的命令行行为，避免已有手工脚本突然产生大量下载流量。
任务是幂等的：重复执行会跳过已有文章，并为缺失本地图片的旧记录补齐媒体。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
import sys
import sqlite3
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from bs4 import BeautifulSoup

from src.blog_fetcher import (
    BLOG_DB_PATH,
    BLOG_IMAGE_DIR,
    _download_images,
    _normalize_date,
    init_blog_db,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
}
GROUP_ORDER = ("nogizaka", "sakurazaka", "hinatazaka")
GROUP_NAMES = {
    "nogizaka": "乃木坂46",
    "sakurazaka": "樱坂46",
    "hinatazaka": "日向坂46",
}
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class GroupStats:
    group_key: str
    discovered: int = 0
    added: int = 0
    skipped: int = 0
    repaired: int = 0
    image_success: int = 0
    image_failed: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"发现 {self.discovered:,} 篇 | 新增 {self.added:,} | "
            f"已有跳过 {self.skipped:,} | 媒体补齐 {self.repaired:,} | "
            f"图片成功 {self.image_success:,} | 图片失败 {self.image_failed:,} | "
            f"异常 {self.failed:,}"
        )


def get_db() -> sqlite3.Connection:
    """复用应用的博客数据库结构，开启 WAL 并给实时监控留出等待时间。"""
    BLOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_blog_db()
    db.execute("PRAGMA busy_timeout = 60000")
    return db


def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _html_to_text(raw_html: str) -> str:
    counter = [0]

    def placeholder(_match) -> str:
        counter[0] += 1
        return f"\n【图片{counter[0]}】\n"

    text = re.sub(r"<img[^>]*>", placeholder, raw_html or "", flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def _paths_ready(paths: list[str], expected_count: int) -> bool:
    if expected_count == 0:
        return True
    if len(paths) < expected_count:
        return False
    return all((BLOG_IMAGE_DIR / path).is_file() for path in paths[:expected_count])


def _media_timestamp(post: dict) -> str:
    """为没有日期的旧文章生成稳定目录名，避免每次重试产生新目录。"""
    raw_date = str(post.get("date") or "").replace("/", "").replace(" ", "_").replace(":", "")
    if raw_date:
        return raw_date
    return hashlib.sha256(str(post.get("url") or "").encode("utf-8")).hexdigest()[:12]


async def _get(client: httpx.AsyncClient, url: str, *, headers: dict | None = None) -> httpx.Response:
    """对页面请求做有限重试，持久性 HTTP 错误不会无限循环。"""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.get(url, headers=headers or HEADERS)
            if response.status_code == 200:
                return response
            if response.status_code not in RETRY_STATUS:
                raise RuntimeError(f"HTTP {response.status_code}")
            last_error = RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001 - 单页失败由调用方记录并继续
            last_error = exc
        if attempt < 2:
            await asyncio.sleep(0.8 * (2**attempt))
    raise RuntimeError(str(last_error or "request failed"))


async def _download_images_resilient(
    client: httpx.AsyncClient,
    images: list[str],
    group_key: str,
    author: str,
    title: str,
    timestamp: str,
) -> list[str]:
    """复用公共下载器，并为临时 CDN 403/超时提供最多三轮补偿。"""
    paths: list[str] = []
    for attempt in range(3):
        paths = await _download_images(client, images, group_key, author, title, timestamp=timestamp)
        if len(paths) == len(images) and all(paths):
            return paths
        if attempt < 2:
            await asyncio.sleep(0.8 * (2**attempt))
    return paths


async def _save_post(
    db: sqlite3.Connection,
    client: httpx.AsyncClient,
    post: dict,
    stats: GroupStats,
    *,
    download_images: bool,
) -> None:
    """保存一篇规范化博客，并按需为已有记录补齐本地图片。"""
    url = str(post.get("url") or "").strip()
    if not url:
        stats.failed += 1
        return
    stats.discovered += 1
    group_key = str(post.get("group_key") or "")
    images = [str(item) for item in (post.get("images") or []) if item]
    existing = db.execute(
        "SELECT id, images_json, image_paths_json FROM blog_posts WHERE url = ?",
        (url,),
    ).fetchone()

    if existing:
        stats.skipped += 1
        old_images = _json_list(existing[1])
        old_paths = _json_list(existing[2])
        if not images:
            images = old_images
        if download_images and images and not _paths_ready(old_paths, len(images)):
            local_paths = await _download_images_resilient(
                client,
                images,
                group_key,
                str(post.get("author") or ""),
                str(post.get("title") or "无标题"),
                timestamp=_media_timestamp(post),
            )
            good = sum(bool(path) for path in local_paths)
            stats.image_success += good
            stats.image_failed += max(0, len(images) - good)
            if good:
                db.execute(
                    "UPDATE blog_posts SET images_json = ?, image_paths_json = ? WHERE id = ?",
                    (json.dumps(images, ensure_ascii=False), json.dumps(local_paths, ensure_ascii=False), existing[0]),
                )
                stats.repaired += 1
        return

    local_paths: list[str] = []
    if download_images and images:
        local_paths = await _download_images_resilient(
            client,
            images,
            group_key,
            str(post.get("author") or ""),
            str(post.get("title") or "无标题"),
            timestamp=_media_timestamp(post),
        )
        good = sum(bool(path) for path in local_paths)
        stats.image_success += good
        stats.image_failed += max(0, len(images) - good)

    raw = post.get("raw")
    raw_json = raw if isinstance(raw, str) else json.dumps(raw or post, ensure_ascii=False, default=str)
    db.execute(
        """
        INSERT OR IGNORE INTO blog_posts
        (group_key, author, title, url, date, body_html, body_text, translation,
         content_json, translation_model, translation_status, translation_error,
         translation_request_id, translation_updated_at, images_json, image_paths_json, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', 'skipped', '', '', '', ?, ?, ?)
        """,
        (
            group_key,
            str(post.get("author") or "") or f"{GROUP_NAMES.get(group_key, group_key)}成员",
            str(post.get("title") or "无标题"),
            url,
            _normalize_date(str(post.get("date") or "")),
            str(post.get("body_html") or ""),
            str(post.get("body_text") or ""),
            json.dumps(images, ensure_ascii=False),
            json.dumps(local_paths, ensure_ascii=False),
            raw_json,
        ),
    )
    stats.added += 1


async def backfill_nogizaka(
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    *,
    download_images: bool = False,
) -> GroupStats:
    stats = GroupStats("nogizaka")
    print("\n🟣 开始抓取【乃木坂46】官方博客全量历史数据 (API st 偏移)...", flush=True)
    url_base = "https://www.nogizaka46.com/s/n46/api/list/blog"
    nogi_headers = {**HEADERS, "accept": "application/json", "x-requested-with": "XMLHttpRequest"}
    rw = 30
    st = 0
    while True:
        try:
            response = await _get(client, f"{url_base}?st={st}&rw={rw}", headers=nogi_headers)
            text = re.sub(r"^\w+\(", "", response.text).rstrip(");")
            data = json.loads(text)
            items = data.get("data", [])
            if not items:
                print(f"  乃木坂46 已抓取完毕 (已达终点 st={st})", flush=True)
                break
            before = stats.added
            for item in items:
                code = str(item.get("code") or "")
                if not code:
                    stats.failed += 1
                    continue
                raw_html = str(item.get("text") or "")
                soup = BeautifulSoup(raw_html, "html.parser")
                images = [
                    urljoin("https://www.nogizaka46.com", img.get("src", ""))
                    for img in soup.find_all("img") if img.get("src")
                ]
                post_url = f"https://www.nogizaka46.com/s/n46/diary/detail/{code}?ima=0000&cd=MEMBER"
                await _save_post(db, client, {
                    "group_key": "nogizaka",
                    "author": str(item.get("name") or "乃木坂46成员").strip(),
                    "title": str(item.get("title") or "无标题").strip(),
                    "url": post_url,
                    "date": item.get("date", ""),
                    "body_html": raw_html,
                    "body_text": _html_to_text(raw_html),
                    "images": images,
                    "raw": item,
                }, stats, download_images=download_images)
            db.commit()
            print(
                f"  [乃木坂46] Offset {st:04d}: 本批新增 {stats.added - before:2d} 篇 "
                f"(累计新增 {stats.added:,} | 已存在 {stats.skipped:,})",
                flush=True,
            )
            st += rw
            await asyncio.sleep(0.12)
        except Exception as exc:  # noqa: BLE001 - 记录当前页并结束，重跑可续
            stats.failed += 1
            print(f"  [乃木坂46] Offset {st} 出错，停止本团: {exc}", flush=True)
            break
    print(f"🟣 乃木坂46 归档完成！{stats.summary()}", flush=True)
    return stats


async def backfill_hinatazaka(
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    max_pages: int = 380,
    *,
    download_images: bool = False,
) -> GroupStats:
    stats = GroupStats("hinatazaka")
    print("\n☀️ 开始抓取【日向坂46】官方博客全量数据...", flush=True)
    for page in range(max_pages):
        page_url = f"https://www.hinatazaka46.com/s/official/diary/member/list?ima=0000&page={page}"
        try:
            response = await _get(client, page_url)
            soup = BeautifulSoup(response.text, "html.parser")
            articles = soup.find_all("div", class_="p-blog-article")
            if not articles:
                print(f"  日向坂46 已抓取至最后一页 (Page {page})", flush=True)
                break
            before = stats.added
            for article in articles:
                post_url = ""
                for link in article.find_all("a"):
                    href = link.get("href", "")
                    if "diary/detail" in href:
                        post_url = urljoin("https://www.hinatazaka46.com", href)
                        break
                if not post_url:
                    stats.failed += 1
                    continue
                title_el = article.find("div", class_="c-blog-article__title")
                author_el = article.find("div", class_="c-blog-article__name")
                body_el = article.find("div", class_="c-blog-article__text")
                raw_html = str(body_el) if body_el else ""
                bound = re.search(
                    r"<(div|a|footer|section)[^>]*(p-button__blog_detail|c-button-blog-detail|"
                    r"p-blog-article|c-blog-member|p-footer|l-footer)[^>]*>",
                    raw_html,
                    flags=re.IGNORECASE,
                )
                if bound:
                    raw_html = raw_html[:bound.start()].strip()
                images = [
                    urljoin("https://www.hinatazaka46.com", img.get("src", ""))
                    for img in (body_el.find_all("img") if body_el else []) if img.get("src")
                ]
                body_text = _html_to_text(raw_html)
                author = str(author_el.text.strip() if author_el else "") or "日向坂46成员"
                if author == "日向坂46成员":
                    match = re.search(
                        r"(高瀬\s*愛奈|齊藤\s*京子|影山\s*優佳|潮\s*紗理菜|渡邉\s*美穂|"
                        r"宮田\s*愛萌|丹生\s*明里|加藤\s*史帆|佐々木\s*久美|佐々木\s*美玲|"
                        r"東村\s*芽依|高本\s*彩花|金村\s*美玖|河田\s*陽菜|小坂\s*菜緒|"
                        r"富田\s*鈴花|濱岸\s*ひより|松田\s*好花|上村\s*ひなの)",
                        body_text[:200] + " " + str(title_el.text if title_el else ""),
                    )
                    if match:
                        author = match.group(1)
                await _save_post(db, client, {
                    "group_key": "hinatazaka",
                    "author": author,
                    "title": str(title_el.text.strip() if title_el else "无标题"),
                    "url": post_url,
                    "date": date_el.text.strip() if (date_el := article.find("div", class_="c-blog-article__date")) else "",
                    "body_html": raw_html,
                    "body_text": body_text,
                    "images": images,
                    "raw": {"url": post_url, "title": title_el.text.strip() if title_el else "", "author": author},
                }, stats, download_images=download_images)
            db.commit()
            print(
                f"  [日向坂46] Page {page:03d}: 本页新增 {stats.added - before:2d} 篇 "
                f"(累计新增 {stats.added:,} | 已存在 {stats.skipped:,})",
                flush=True,
            )
            await asyncio.sleep(0.18)
        except Exception as exc:  # noqa: BLE001 - 当前页失败后允许重跑继续
            stats.failed += 1
            print(f"  [日向坂46] Page {page} 出错，停止本团: {exc}", flush=True)
            break
    print(f"☀️ 日向坂46 归档完成！{stats.summary()}", flush=True)
    return stats


async def backfill_sakurazaka(
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    max_pages: int = 470,
    *,
    download_images: bool = False,
) -> GroupStats:
    stats = GroupStats("sakurazaka")
    print("\n🌸 开始抓取【樱坂46】官方博客全量数据...", flush=True)
    sem = asyncio.Semaphore(8)

    async def fetch_detail(item):
        anchor = item.find("a")
        if not anchor:
            return None
        post_url = urljoin("https://sakurazaka46.com", anchor.get("href", ""))
        existing = db.execute(
            "SELECT images_json, image_paths_json FROM blog_posts WHERE url = ?", (post_url,)
        ).fetchone()
        if existing:
            old_images = _json_list(existing[0])
            old_paths = _json_list(existing[1])
            # 旧版全量脚本可能只保存了正文，无法仅凭空的 images_json 判断
            # “确实没有图片”；启用本地媒体模式时仍需抓一次详情页确认。
            if not download_images or (old_images and _paths_ready(old_paths, len(old_images))):
                return {"url": post_url, "_skip": True}
        title_el = item.find("h3", class_="title")
        author_el = item.find("p", class_="name")
        date_el = item.find("p", class_="date")
        async with sem:
            response = await _get(client, post_url)
        detail = BeautifulSoup(response.text, "html.parser")
        body_el = detail.find("div", class_="box-article") or detail
        raw_html = str(body_el)
        images = [
            urljoin("https://sakurazaka46.com", img.get("src", ""))
            for img in body_el.find_all("img") if img.get("src")
        ]
        foot = detail.find("div", class_="blog-foot")
        if foot and foot.find("p", class_="date"):
            date_raw = foot.find("p", class_="date").text.strip()
        else:
            date_raw = date_el.text.strip() if date_el else ""
        return {
            "group_key": "sakurazaka",
            "author": author_el.text.strip() if author_el else "樱坂46成员",
            "title": title_el.text.strip() if title_el else "无标题",
            "url": post_url,
            "date": date_raw,
            "body_html": raw_html,
            "body_text": _html_to_text(raw_html),
            "images": images,
            "raw": {"url": post_url, "title": title_el.text.strip() if title_el else "", "author": author_el.text.strip() if author_el else "", "date": date_raw},
        }

    for page in range(max_pages):
        page_url = f"https://sakurazaka46.com/s/s46/diary/blog/list?ima=0000&page={page}"
        try:
            response = await _get(client, page_url)
            soup = BeautifulSoup(response.text, "html.parser")
            container = soup.find("ul", class_="com-blog-part")
            boxes = container.find_all("li", class_="box") if container else []
            if not boxes:
                print(f"  樱坂46 已抓取至最后一页 (Page {page})", flush=True)
                break
            before = stats.added
            results = await asyncio.gather(*(fetch_detail(box) for box in boxes), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) or result is None:
                    stats.failed += 1
                    continue
                if result.get("_skip"):
                    stats.discovered += 1
                    stats.skipped += 1
                    continue
                await _save_post(db, client, result, stats, download_images=download_images)
            db.commit()
            print(
                f"  [樱坂46] Page {page:03d}: 本页新增 {stats.added - before:2d} 篇 "
                f"(累计新增 {stats.added:,} | 已存在 {stats.skipped:,})",
                flush=True,
            )
            await asyncio.sleep(0.18)
        except Exception as exc:  # noqa: BLE001 - 当前页失败后允许重跑继续
            stats.failed += 1
            print(f"  [樱坂46] Page {page} 出错，停止本团: {exc}", flush=True)
            break
    print(f"🌸 樱坂46 归档完成！{stats.summary()}", flush=True)
    return stats


def normalize_groups(values: list[str] | None) -> list[str]:
    if not values or "all" in values:
        return list(GROUP_ORDER)
    selected = set(values)
    return [group for group in GROUP_ORDER if group in selected]


async def main() -> None:
    parser = argparse.ArgumentParser(description="三大坂道全历史博客回填归档工具")
    parser.add_argument(
        "--group",
        action="append",
        choices=["all", *GROUP_ORDER],
        dest="groups",
        help="可重复指定团体；不指定或传 all 代表三团",
    )
    parser.add_argument(
        "--download-images",
        action="store_true",
        help="下载图片到 data/blog_images，并为已有记录补齐缺失本地图片",
    )
    args = parser.parse_args()
    groups = normalize_groups(args.groups)
    db = get_db()
    summaries: list[GroupStats] = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for group in groups:
            if group == "nogizaka":
                summaries.append(await backfill_nogizaka(client, db, download_images=args.download_images))
            elif group == "sakurazaka":
                summaries.append(await backfill_sakurazaka(client, db, download_images=args.download_images))
            else:
                summaries.append(await backfill_hinatazaka(client, db, download_images=args.download_images))
    db.close()
    print("\n🎉 选定团体抓取归档完成！", flush=True)
    for summary in summaries:
        print(f"  [{GROUP_NAMES[summary.group_key]}] {summary.summary()}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
