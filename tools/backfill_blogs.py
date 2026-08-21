"""
官方博客全量回填归档工具 (Sakamichi All-Member Blog Backfiller)
支持 乃木坂46、櫻坂46、日向坂46 三大团体全历史官方博客的高速分页抓取、并发详情解析与 SQLite 数据库增量入库。
"""

import asyncio
import json
import re
import sys
import argparse
import sqlite3
from pathlib import Path
from html import unescape
from urllib.parse import urljoin

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from bs4 import BeautifulSoup
from src.blog_fetcher import BLOG_DB_PATH, _normalize_date

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

def get_db():
    BLOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BLOG_DB_PATH), timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS blog_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key TEXT NOT NULL,
            author TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            date TEXT,
            body_html TEXT,
            body_text TEXT,
            translation TEXT,
            content_json TEXT,
            translation_model TEXT,
            images_json TEXT,
            image_paths_json TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn

# ── 乃木坂46 ──
async def backfill_nogizaka(client: httpx.AsyncClient, db: sqlite3.Connection):
    print("\n🟣 开始抓取【乃木坂46】官方博客全量历史数据 (API st 偏移)...", flush=True)
    url_base = "https://www.nogizaka46.com/s/n46/api/list/blog"
    nogi_headers = {**HEADERS, "accept": "application/json", "x-requested-with": "XMLHttpRequest"}
    
    total_added = 0
    total_skipped = 0
    rw = 30
    st = 0

    while True:
        api_url = f"{url_base}?st={st}&rw={rw}"
        try:
            r = await client.get(api_url, headers=nogi_headers)
            text = re.sub(r"^\w+\(", "", r.text).rstrip(");")
            data = json.loads(text)
            items = data.get("data", [])
            if not items:
                print(f"  乃木坂46 已抓取完毕 (已达终点 st={st})", flush=True)
                break

            page_added = 0
            for item in items:
                code = item.get("code", "")
                post_url = f"https://www.nogizaka46.com/s/n46/diary/detail/{code}?ima=0000&cd=MEMBER"
                
                row = db.execute("SELECT id FROM blog_posts WHERE url=?", (post_url,)).fetchone()
                if row:
                    total_skipped += 1
                    continue

                raw_html = item.get("text", "")
                soup = BeautifulSoup(raw_html, "html.parser")
                images = [
                    urljoin("https://www.nogizaka46.com", img["src"])
                    for img in soup.find_all("img") if img.get("src")
                ]
                
                _c = [0]
                def _ph(m):
                    _c[0] += 1
                    return f"\n【图片{_c[0]}】\n"
                body_text = re.sub(r"<img[^>]*>", _ph, raw_html, flags=re.IGNORECASE)
                body_text = re.sub(r"<br\s*/?>", "\n", body_text, flags=re.IGNORECASE)
                body_text = re.sub(r"<[^>]+>", "", body_text)
                body_text = unescape(body_text).strip()

                norm_date = _normalize_date(item.get("date", ""))
                author = (item.get("name") or "乃木坂46成员").strip()
                title = (item.get("title") or "无标题").strip()

                db.execute("""
                    INSERT INTO blog_posts (group_key, author, title, url, date, body_html, body_text, images_json, image_paths_json, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    "nogizaka", author, title, post_url, norm_date,
                    raw_html, body_text, json.dumps(images, ensure_ascii=False), "[]",
                    json.dumps(item, ensure_ascii=False)
                ))
                page_added += 1
                total_added += 1

            db.commit()
            if (st // rw) % 5 == 0 or page_added > 0:
                print(f"  [乃木坂46] Offset {st:04d} (Page {st//rw:03d}): 本批新增 {page_added:2d} 篇 (累计新入库: {total_added:,} | 已存在跳过: {total_skipped:,})", flush=True)
            st += rw
            await asyncio.sleep(0.12)
        except Exception as e:
            print(f"  [乃木坂46] Offset {st} 出错: {e}", flush=True)
            await asyncio.sleep(2)

    print(f"🟣 乃木坂46 归档完成！本次新录入 {total_added:,} 篇，跳过已有 {total_skipped:,} 篇。", flush=True)

# ── 日向坂46 ──
async def backfill_hinatazaka(client: httpx.AsyncClient, db: sqlite3.Connection, max_pages: int = 380):
    print("\n☀️ 开始抓取【日向坂46】官方博客全量数据...", flush=True)
    total_added = 0
    total_skipped = 0

    for page in range(max_pages):
        page_url = f"https://www.hinatazaka46.com/s/official/diary/member/list?ima=0000&page={page}"
        try:
            r = await client.get(page_url, headers=HEADERS)
            if r.status_code != 200:
                print(f"  日向坂46 Page {page} HTTP {r.status_code}，停止抓取", flush=True)
                break
            
            soup = BeautifulSoup(r.text, "html.parser")
            articles = soup.find_all("div", class_="p-blog-article")
            if not articles:
                print(f"  日向坂46 已抓取至最后一页 (Page {page})", flush=True)
                break

            page_added = 0
            for a in articles:
                links = a.find_all("a")
                post_url = ""
                for l in links:
                    href = l.get("href", "")
                    if "diary/detail" in href:
                        post_url = urljoin("https://www.hinatazaka46.com", href)
                        break
                if not post_url:
                    continue

                row = db.execute("SELECT id FROM blog_posts WHERE url=?", (post_url,)).fetchone()
                if row:
                    total_skipped += 1
                    continue

                title_el = a.find("div", class_="c-blog-article__title")
                title = title_el.text.strip() if title_el else "无标题"
                name_el = a.find("div", class_="c-blog-article__name")
                author = name_el.text.strip() if name_el else ""
                
                body_el = a.find("div", class_="c-blog-article__text")
                raw_html = str(body_el) if body_el else ""
                m_bound = re.search(r'<(div|a|footer|section)[^>]*(p-button__blog_detail|c-button-blog-detail|p-blog-article|c-blog-member|p-footer|l-footer)[^>]*>', raw_html, flags=re.IGNORECASE)
                if m_bound:
                    raw_html = raw_html[:m_bound.start()].strip()

                _c = [0]
                def _ph(m):
                    _c[0] += 1
                    return f"\n【图片{_c[0]}】\n"
                body_text = re.sub(r"<img[^>]*>", _ph, raw_html, flags=re.IGNORECASE)
                body_text = re.sub(r"<br\s*/?>", "\n", body_text, flags=re.IGNORECASE)
                body_text = re.sub(r"<[^>]+>", "", body_text)
                body_text = unescape(body_text).strip()

                # Author fallback for graduated members whose member name is blank
                if not author:
                    # Try to find name in body_text or title
                    m_name = re.search(r'(高瀬\s*愛奈|齊藤\s*京子|影山\s*優佳|潮\s*紗理菜|渡邉\s*美穂|宮田\s*愛萌|丹生\s*明里|加藤\s*史帆|佐々木\s*久美|佐々木\s*美玲|東村\s*芽依|高本\s*彩花|金村\s*美玖|河田\s*陽菜|小坂\s*菜緒|富田\s*鈴花|丹生\s*明里|濱岸\s*ひより|松田\s*好花|宮田\s*愛萌|渡邉\s*美穂|上村\s*ひなの)', body_text[:200] + " " + title)
                    author = m_name.group(1) if m_name else "日向坂46成员"

                date_el = a.find("div", class_="c-blog-article__date")
                date_raw = date_el.text.strip() if date_el else ""
                norm_date = _normalize_date(date_raw)

                images = [
                    urljoin("https://www.hinatazaka46.com", img["src"]) if not img["src"].startswith("http") else img["src"]
                    for img in body_el.find_all("img") if img.get("src")
                ] if body_el else []

                db.execute("""
                    INSERT INTO blog_posts (group_key, author, title, url, date, body_html, body_text, images_json, image_paths_json, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    "hinatazaka", author, title, post_url, norm_date,
                    raw_html, body_text, json.dumps(images, ensure_ascii=False), "[]",
                    json.dumps({"url": post_url, "title": title, "author": author, "date": date_raw}, ensure_ascii=False)
                ))
                page_added += 1
                total_added += 1

            db.commit()
            if page % 5 == 0 or page_added > 0:
                print(f"  [日向坂46] Page {page:03d}: 本页新增 {page_added:2d} 篇 (累计新入库: {total_added:,} | 已存在跳过: {total_skipped:,})", flush=True)
            await asyncio.sleep(0.18)
        except Exception as e:
            print(f"  [日向坂46] Page {page} 出错: {e}", flush=True)
            await asyncio.sleep(2)

    print(f"☀️ 日向坂46 归档完成！本次新录入 {total_added:,} 篇，跳过已有 {total_skipped:,} 篇。", flush=True)

# ── 櫻坂46 ──
async def backfill_sakurazaka(client: httpx.AsyncClient, db: sqlite3.Connection, max_pages: int = 470):
    print("\n🌸 开始抓取【櫻坂46】官方博客全量数据...", flush=True)
    total_added = 0
    total_skipped = 0

    sem = asyncio.Semaphore(8)

    async def fetch_one_sakura(item, page_idx):
        nonlocal total_added, total_skipped
        a_tag = item.find("a")
        if not a_tag:
            return None
        post_url = urljoin("https://sakurazaka46.com", a_tag.get("href", ""))
        
        row = db.execute("SELECT id FROM blog_posts WHERE url=?", (post_url,)).fetchone()
        if row:
            total_skipped += 1
            return None

        title = item.find("h3", class_="title").text.strip() if item.find("h3", class_="title") else "无标题"
        author = item.find("p", class_="name").text.strip() if item.find("p", class_="name") else "櫻坂46成员"
        date_tag = item.find("p", class_="date")
        date_raw = date_tag.text.strip() if date_tag else ""

        async with sem:
            try:
                detail_r = await client.get(post_url, headers=HEADERS)
                detail_soup = BeautifulSoup(detail_r.text, "html.parser")
                body_el = detail_soup.find("div", class_="box-article") or detail_soup
                raw_html = str(body_el)

                images = [
                    urljoin("https://sakurazaka46.com", img["src"]) if not img["src"].startswith("http") else img["src"]
                    for img in body_el.find_all("img") if img.get("src")
                ]

                foot = detail_soup.find("div", class_="blog-foot")
                if foot and foot.find("p", class_="date"):
                    date_raw = foot.find("p", class_="date").text.strip()
                norm_date = _normalize_date(date_raw)

                _c = [0]
                def _ph(m):
                    _c[0] += 1
                    return f"\n【图片{_c[0]}】\n"
                body_text = re.sub(r"<img[^>]*>", _ph, raw_html, flags=re.IGNORECASE)
                body_text = re.sub(r"<br\s*/?>", "\n", body_text, flags=re.IGNORECASE)
                body_text = re.sub(r"<[^>]+>", "", body_text)
                body_text = unescape(body_text).strip()

                return (
                    "sakurazaka", author, title, post_url, norm_date,
                    raw_html, body_text, json.dumps(images, ensure_ascii=False), "[]",
                    json.dumps({"url": post_url, "title": title, "author": author, "date": date_raw}, ensure_ascii=False)
                )
            except Exception:
                return None

    for page in range(max_pages):
        page_url = f"https://sakurazaka46.com/s/s46/diary/blog/list?ima=0000&page={page}"
        try:
            r = await client.get(page_url, headers=HEADERS)
            if r.status_code != 200:
                print(f"  櫻坂46 Page {page} HTTP {r.status_code}，停止抓取", flush=True)
                break
            
            soup = BeautifulSoup(r.text, "html.parser")
            container = soup.find("ul", class_="com-blog-part")
            if not container:
                print(f"  櫻坂46 已抓取至最后一页 (Page {page})", flush=True)
                break

            boxes = container.find_all("li", class_="box")
            if not boxes:
                print(f"  櫻坂46 已抓取至最后一页 (Page {page})", flush=True)
                break

            tasks = [fetch_one_sakura(b, page) for b in boxes]
            results = await asyncio.gather(*tasks)
            valid_results = [res for res in results if res is not None]

            for entry in valid_results:
                db.execute("""
                    INSERT INTO blog_posts (group_key, author, title, url, date, body_html, body_text, images_json, image_paths_json, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, entry)
                total_added += 1

            db.commit()
            if page % 5 == 0 or len(valid_results) > 0:
                print(f"  [櫻坂46] Page {page:03d}: 本页新增 {len(valid_results):2d} 篇 (累计新入库: {total_added:,} | 已存在跳过: {total_skipped:,})", flush=True)
            await asyncio.sleep(0.18)
        except Exception as e:
            print(f"  [櫻坂46] Page {page} 出错: {e}", flush=True)
            await asyncio.sleep(2)

    print(f"🌸 櫻坂46 归档完成！本次新录入 {total_added:,} 篇，跳过已有 {total_skipped:,} 篇。", flush=True)

async def main():
    parser = argparse.ArgumentParser(description="三大坂道全历史博客回填归档工具")
    parser.add_argument("--group", choices=["all", "nogizaka", "sakurazaka", "hinatazaka"], default="all")
    args = parser.parse_args()

    db = get_db()
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        if args.group in ("all", "nogizaka"):
            await backfill_nogizaka(client, db)
        if args.group in ("all", "hinatazaka"):
            await backfill_hinatazaka(client, db, max_pages=380)
        if args.group in ("all", "sakurazaka"):
            await backfill_sakurazaka(client, db, max_pages=470)

    print("\n🎉 全部抓取归档完成！", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
