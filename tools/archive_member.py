"""
坂道系列 (乃木坂46 / 樱坂46 / 日向坂46) 成员博客通用全量归档工具。
支持输入任何官方博客列表页 URL，或指定 --group / --ct 参数，全量抓取指定成员的历史博客正文与高清原图。

使用示例：
  python tools/archive_member.py "https://www.nogizaka46.com/s/n46/diary/MEMBER/list?ima=0000&ct=48017"
  python tools/archive_member.py "https://sakurazaka46.com/s/s46/diary/blog/list?ima=0000&ct=59"
  python tools/archive_member.py "https://www.hinatazaka46.com/s/official/diary/member/list?ima=0000&ct=12"
  python tools/archive_member.py --group nogizaka --ct 48017 --translate
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urljoin

import httpx
from bs4 import BeautifulSoup

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.blog_fetcher import init_blog_db, _download_images, _normalize_date  # noqa: E402
from src.sources.sakurazaka import _parse_date as sakura_parse_date  # noqa: E402
from src.sources.nogizaka import _parse_jsonp as nogi_parse_jsonp  # noqa: E402
from src import translator  # noqa: E402

def safe_log(msg: str):
    """防止 Windows 命令行编码造成的 Print 崩溃"""
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(msg.encode("ascii", errors="replace").decode("ascii"), flush=True)
        except Exception:
            pass

async def resolve_target(client: httpx.AsyncClient, url_or_ct: str, group_opt: str = None, ct_opt: str = None) -> tuple[str, str, str]:
    """
    智能解析目标 URL 或 ct 参数。
    返回 (group_key, ct, single_post_url)
    支持列表页 URL、详情页 URL（自动从详情页反向解析成员 ct 代码）、或纯 ct 数字。
    """
    group = group_opt
    ct = ct_opt
    single_url = None
    
    if url_or_ct and url_or_ct.startswith("http"):
        parsed = urlparse(url_or_ct)
        domain = parsed.netloc.lower()
        qs = parse_qs(parsed.query)
        
        if "ct" in qs:
            ct = qs["ct"][0]
            
        if "nogizaka46.com" in domain:
            group = "nogizaka"
        elif "sakurazaka46.com" in domain:
            group = "sakurazaka"
        elif "hinatazaka46.com" in domain:
            group = "hinatazaka"
            
        # 如果是详情页 URL 且没有 ct 参数，尝试从详情页自动解析成员 ct 代码
        if not ct and "/detail/" in parsed.path:
            try:
                safe_log(f"🔍 检测到详情页 URL，正在解析成员信息: {url_or_ct}")
                r = await client.get(url_or_ct, timeout=15.0)
                r.encoding = "utf-8"
                soup = BeautifulSoup(r.text, "html.parser")
                
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if ("list" in href or "ct=" in href) and "ticket" not in href:
                        sub_qs = parse_qs(urlparse(href).query)
                        if "ct" in sub_qs and sub_qs["ct"][0].isdigit():
                            ct = sub_qs["ct"][0]
                            safe_log(f"✅ 从详情页解析到成员 ct={ct}")
                            break
            except Exception as e:
                safe_log(f"⚠️ 从详情页提取成员 ID 失败: {e}")
                
            if not ct:
                single_url = url_or_ct

    elif url_or_ct and url_or_ct.isdigit():
        ct = url_or_ct

    if not group:
        group = "nogizaka" # 默认

    return group.lower(), ct, single_url

async def archive_single_post(client: httpx.AsyncClient, db: sqlite3.Connection, group_key: str, post_url: str, translate: bool = False):
    safe_log("==========================================")
    safe_log(f"📌 归档单篇博客: {post_url}")
    safe_log("==========================================")
    
    r = await client.get(post_url, timeout=25.0)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    
    body = soup.find("div", class_="box-article") or soup.find("div", class_="c-blog-article__text") or soup
    imgs = [
        urljoin(post_url, img["src"])
        for img in body.find_all("img") if img.get("src")
    ]
    body_html = str(body)
    _counter = [0]
    def _img_placeholder(m):
        _counter[0] += 1
        return f"\n【图片{_counter[0]}】\n"
    body_text = re.sub(r"<img[^>]*>", _img_placeholder, body_html, flags=re.IGNORECASE)
    body_text = re.sub(r"<br\s*/?>", "\n", body_text, flags=re.IGNORECASE)
    body_text = re.sub(r"<[^>]+>", "", body_text)
    body_text = unescape(body_text).strip()
    
    title_elem = soup.find("h3", class_="title") or soup.find("div", class_="c-blog-article__title")
    author_elem = soup.find("p", class_="name") or soup.find("div", class_="c-blog-article__name")
    date_elem = soup.find("p", class_="date") or soup.find("div", class_="c-blog-article__date")
    
    title = title_elem.text.strip() if title_elem else "无标题"
    author = author_elem.text.strip() if author_elem else "坂道成员"
    raw_date = date_elem.text.strip() if date_elem else ""
    final_date = _normalize_date(raw_date)
    
    post_item = {
        "url": post_url,
        "title": title,
        "author": author,
        "date": final_date,
        "images": imgs,
        "body_html": body_html,
        "body_text": body_text,
    }
    await _process_and_save_posts(client, db, group_key, [post_item], translate=translate)

async def archive_nogizaka(client: httpx.AsyncClient, db: sqlite3.Connection, ct: str, translate: bool = False):
    safe_log("==========================================")
    safe_log(f"💜 开始归档【乃木坂46】成员 ct={ct} 的全量博客...")
    safe_log("==========================================")
    
    api_url = "https://www.nogizaka46.com/s/n46/api/list/blog"
    headers = {"accept": "application/json", "x-requested-with": "XMLHttpRequest"}
    
    st = 0
    rw = 100
    all_posts = []
    default_name = "4期生" if ct == "40005" else "乃木坂46"
    
    while True:
        url = f"{api_url}?rw={rw}&st={st}&ct={ct}"
        safe_log(f"📄 正在获取 API st={st} -> {url}")
        try:
            r = await client.get(url, headers=headers, timeout=25.0)
            if r.status_code != 200:
                safe_log(f"⚠️ API 返回 HTTP {r.status_code}，停止列表拉取")
                break
            
            data = nogi_parse_jsonp(r.text)
            items = data.get("data", [])
            if not items:
                safe_log(f"ℹ️ 列表抓取完成。共找到 {len(all_posts)} 篇博客。")
                break
            
            for item in items:
                code = item.get("code", "")
                post_url = f"https://www.nogizaka46.com/s/n46/diary/detail/{code}?ima=0000&cd=MEMBER"
                raw_html = item.get("text", "")
                soup = BeautifulSoup(raw_html, "html.parser")
                images = [
                    urljoin("https://www.nogizaka46.com", img["src"])
                    for img in soup.find_all("img") if img.get("src")
                ]
                _counter = [0]
                def _img_placeholder(m):
                    _counter[0] += 1
                    return f"\n【图片{_counter[0]}】\n"
                body_text = re.sub(r"<img[^>]*>", _img_placeholder, raw_html, flags=re.IGNORECASE)
                body_text = re.sub(r"<br\s*/?>", "\n", body_text, flags=re.IGNORECASE)
                body_text = re.sub(r"<[^>]+>", "", body_text)
                body_text = unescape(body_text).strip()
                
                raw_date = item.get("date", "")
                final_date = _normalize_date(raw_date)
                
                author_name = "4期生" if ct == "40005" else (item.get("name") or default_name)
                
                all_posts.append({
                    "url": post_url,
                    "title": item.get("title", "无标题"),
                    "author": author_name,
                    "date": final_date,
                    "images": images,
                    "body_text": body_text,
                    "body_html": raw_html,
                    "raw_item": item,
                })
            
            if len(items) < rw:
                break
            st += rw
            await asyncio.sleep(0.3)
        except Exception as e:
            safe_log(f"💥 获取列表失败: {e}")
            break

    await _process_and_save_posts(client, db, "nogizaka", all_posts, translate=translate)

async def archive_sakurazaka(client: httpx.AsyncClient, db: sqlite3.Connection, ct: str, translate: bool = False):
    safe_log("==========================================")
    safe_log(f"🌸 开始归档【樱坂46】成员 ct={ct} 的全量博客...")
    safe_log("==========================================")
    
    page = 0
    all_posts = []
    
    while True:
        url = f"https://sakurazaka46.com/s/s46/diary/blog/list?ima=0000&ct={ct}&page={page}"
        safe_log(f"📄 正在抓取列表页 {page} -> {url}")
        try:
            r = await client.get(url, timeout=25.0)
            r.encoding = "utf-8"
            if r.status_code != 200:
                safe_log(f"⚠️ 列表页 {page} 返回 HTTP {r.status_code}，停止列表拉取")
                break
            
            soup = BeautifulSoup(r.text, "html.parser")
            container = soup.find("ul", class_="com-blog-part")
            if not container:
                safe_log(f"ℹ️ 列表抓取完成。共找到 {len(all_posts)} 篇博客。")
                break
            
            items = container.find_all("li", class_="box")
            if not items:
                break
            
            page_posts = []
            for item in items:
                a_tag = item.find("a")
                if not a_tag:
                    continue
                d_tag = item.find("p", class_="date")
                title_tag = item.find("h3", class_="title")
                author_tag = item.find("p", class_="name")
                
                post_url = urljoin("https://sakurazaka46.com", a_tag.get("href", ""))
                title = title_tag.text.strip() if title_tag else "无标题"
                author = author_tag.text.strip() if author_tag else "樱坂46成员"
                date_str = sakura_parse_date(d_tag.text.strip()) if d_tag else ""
                
                page_posts.append({
                    "url": post_url,
                    "title": title,
                    "author": author,
                    "date": date_str,
                })
            
            if not page_posts:
                break
            all_posts.extend(page_posts)
            page += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            safe_log(f"💥 抓取列表页 {page} 失败: {e}")
            break

    safe_log(f"🔎 正在逐篇解析 {len(all_posts)} 篇博客的详情正文与原图链接...")
    detail_posts = []
    for idx, p in enumerate(all_posts):
        post_url = p["url"]
        if (idx + 1) % 15 == 0 or idx == 0 or idx == len(all_posts) - 1:
            safe_log(f"   [解析进度 {idx+1}/{len(all_posts)}] {p['title']}")
        try:
            r = await client.get(post_url, timeout=25.0)
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            body = soup.find("div", class_="box-article") or soup
            imgs = [
                urljoin("https://sakurazaka46.com", img["src"])
                for img in body.find_all("img") if img.get("src")
            ]
            body_html = str(body)
            _counter = [0]
            def _img_placeholder(m):
                _counter[0] += 1
                return f"\n【图片{_counter[0]}】\n"
            body_text = re.sub(r"<img[^>]*>", _img_placeholder, body_html, flags=re.IGNORECASE)
            body_text = re.sub(r"<br\s*/?>", "\n", body_text, flags=re.IGNORECASE)
            body_text = re.sub(r"<[^>]+>", "", body_text)
            body_text = unescape(body_text).strip()
            
            foot = soup.find("div", class_="blog-foot")
            detail_date = ""
            if foot:
                d_tag = foot.find("p", class_="date")
                if d_tag:
                    detail_date = sakura_parse_date(d_tag.text.strip())
                    
            final_date = _normalize_date(detail_date or p["date"])
            detail_posts.append({
                "url": post_url,
                "title": p["title"],
                "author": p["author"],
                "date": final_date,
                "images": imgs,
                "body_html": body_html,
                "body_text": body_text,
                "raw_item": p,
            })
        except Exception as e:
            safe_log(f"⚠️ 解析详情页失败 ({post_url}): {e}")
            
    await _process_and_save_posts(client, db, "sakurazaka", detail_posts, translate=translate)

async def archive_hinatazaka(client: httpx.AsyncClient, db: sqlite3.Connection, ct: str, translate: bool = False):
    safe_log("==========================================")
    safe_log(f"☀️ 开始归档【日向坂46】成员 ct={ct} 的全量博客...")
    safe_log("==========================================")
    
    page = 0
    all_posts = []
    
    while True:
        url = f"https://www.hinatazaka46.com/s/official/diary/member/list?ima=0000&ct={ct}&page={page}"
        safe_log(f"📄 正在抓取列表页 {page} -> {url}")
        try:
            r = await client.get(url, timeout=25.0)
            if r.status_code != 200:
                safe_log(f"⚠️ 列表页 {page} 返回 HTTP {r.status_code}，停止列表拉取")
                break
            
            soup = BeautifulSoup(r.text, "html.parser")
            articles = soup.find_all("div", class_="p-blog-article")
            if not articles:
                safe_log(f"ℹ️ 列表抓取完成。共找到 {len(all_posts)} 篇博客。")
                break
            
            page_posts = []
            for art in articles:
                detail_a = art.find("a", href=re.compile(r"/s/official/diary/detail/"))
                if not detail_a:
                    continue
                post_url = urljoin("https://www.hinatazaka46.com", detail_a["href"])
                title_elem = art.find("div", class_="c-blog-article__title")
                name_elem = art.find("div", class_="c-blog-article__name")
                date_elem = art.find("div", class_="c-blog-article__date")
                body_elem = art.find("div", class_="c-blog-article__text")
                
                title = title_elem.text.strip() if title_elem else "无标题"
                author = name_elem.text.strip() if name_elem else "日向坂46成员"
                date_raw = date_elem.text.strip() if date_elem else ""
                final_date = _normalize_date(date_raw)
                body_html = str(body_elem) if body_elem else ""
                
                imgs = []
                if body_elem:
                    imgs = [
                        ("https:" + img["src"] if img.get("src", "").startswith("//") else img["src"])
                        for img in body_elem.find_all("img")
                        if "hinatazaka46.com" in img.get("src", "")
                    ]
                
                _counter = [0]
                def _img_placeholder(m):
                    _counter[0] += 1
                    return f"\n【图片{_counter[0]}】\n"
                body_text = re.sub(r"<img[^>]*>", _img_placeholder, body_html, flags=re.IGNORECASE)
                body_text = re.sub(r"<br\s*/?>", "\n", body_text, flags=re.IGNORECASE)
                body_text = re.sub(r"<[^>]+>", "", body_text)
                body_text = unescape(body_text).strip()
                
                page_posts.append({
                    "url": post_url,
                    "title": title,
                    "author": author,
                    "date": final_date,
                    "images": imgs,
                    "body_html": body_html,
                    "body_text": body_text,
                    "raw_item": {"title": title, "author": author, "url": post_url},
                })
            
            if not page_posts:
                break
            all_posts.extend(page_posts)
            page += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            safe_log(f"💥 抓取列表页 {page} 失败: {e}")
            break

    await _process_and_save_posts(client, db, "hinatazaka", all_posts, translate=translate)

async def _process_and_save_posts(client: httpx.AsyncClient, db: sqlite3.Connection, group_key: str, posts: list[dict], translate: bool = False):
    safe_log(f"\n📦 开始处理并入库 {len(posts)} 篇博客（按发布时间顺规）...")
    saved_count = 0
    skipped_count = 0
    
    for idx, p in enumerate(reversed(posts)):
        post_url = p["url"]
        
        cur = db.execute("SELECT id FROM blog_posts WHERE url = ?", (post_url,))
        if cur.fetchone():
            skipped_count += 1
            continue
        
        safe_log(f"[{idx+1}/{len(posts)}] ⬇️ 正在归档: {p['author']} - {p['title']}")
        try:
            image_paths = []
            if p.get("images"):
                image_paths = await _download_images(
                    client, p["images"], group_key,
                    p["author"], p.get("title", ""),
                    timestamp=p["date"].replace("/", "").replace(" ", "_").replace(":", "")
                )
            
            trans_result = None
            content_json = ""
            translation_model = ""
            if translate:
                safe_log("   🌐 [AI 翻译中...]")
                structured, model_name = await translator.translate_blog_structured(
                    p["body_html"], p["author"], group_key, custom_client=client
                )
                if structured:
                    content_json = json.dumps(structured, ensure_ascii=False)
                    trans_result = translator.blocks_to_html(structured)
                    translation_model = model_name or ""

            db.execute("""
                INSERT INTO blog_posts (
                    group_key, author, title, url, date, body_html, body_text,
                    translation, content_json, translation_model, images_json, image_paths_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                group_key, p["author"], p["title"], post_url, p["date"],
                p["body_html"], p["body_text"], trans_result,
                content_json,
                translation_model,
                json.dumps(p.get("images", []), ensure_ascii=False),
                json.dumps(image_paths, ensure_ascii=False),
                json.dumps(p.get("raw_item", {}), ensure_ascii=False)
            ))
            db.commit()
            saved_count += 1
            safe_log(f"   ✅ [保存成功] 含 {len(p.get('images', []))} 张图片" + (" (含 AI 双语译文)" if trans_result else ""))
        except Exception as e:
            safe_log(f"   ❌ 保存失败: {e}")
        
        await asyncio.sleep(0.2)
        
    safe_log(f"\n🎉 归档任务完成！成功保存 {saved_count} 篇，跳过已有 {skipped_count} 篇。\n")

async def main():
    parser = argparse.ArgumentParser(description="坂道系列 (乃木坂46 / 樱坂46 / 日向坂46) 成员博客通用全量归档工具")
    parser.add_argument("target", nargs="?", help="官方博客列表页 / 详情页 URL 或成员 ct 编号")
    parser.add_argument("--group", choices=["nogizaka", "sakurazaka", "hinatazaka"], help="团体名称 (nogizaka / sakurazaka / hinatazaka)")
    parser.add_argument("--ct", help="成员 ct 代码 (例如 48017 / 59 / 12)")
    parser.add_argument("--translate", action="store_true", help="是否同时调用 Gemini 进行 AI 中日双语翻译 (默认关闭，仅归档正文与图片)")
    
    args = parser.parse_args()
    
    db = init_blog_db()
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=30.0) as client:
        group_key, ct, single_url = await resolve_target(client, args.target, args.group, args.ct)
        
        if ct:
            if group_key == "nogizaka":
                await archive_nogizaka(client, db, ct, translate=args.translate)
            elif group_key == "sakurazaka":
                await archive_sakurazaka(client, db, ct, translate=args.translate)
            elif group_key == "hinatazaka":
                await archive_hinatazaka(client, db, ct, translate=args.translate)
            else:
                safe_log(f"❌ 不支持的团体类型: {group_key}")
        elif single_url:
            await archive_single_post(client, db, group_key, single_url, translate=args.translate)
        else:
            safe_log("❌ 请输入正确的博客列表页 / 详情页 URL 或 --ct 编号！示例：")
            safe_log("  python tools/archive_member.py \"https://sakurazaka46.com/s/s46/diary/detail/70526?ima=0000&cd=blog\"")
            sys.exit(1)
            
    db.close()

if __name__ == "__main__":
    asyncio.run(main())
