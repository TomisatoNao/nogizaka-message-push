# ============================================================
# avatar_manager.py — 坂道官方成员与博客作者头像本地缓存及数据库持久化
# ============================================================
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from src.logger import log_all

AVATAR_DIR = Path("data/avatars")
AVATAR_DB_PATH = Path("data/archive/archive.db")

_lock = asyncio.Lock()


def get_avatar_db() -> sqlite3.Connection:
    """初始化并返回包含 member_avatars 的数据库连接。"""
    AVATAR_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AVATAR_DB_PATH), timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS member_avatars (
            group_key TEXT NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            avatar_url TEXT NOT NULL,
            local_file TEXT DEFAULT '',
            updated_at REAL NOT NULL,
            PRIMARY KEY (group_key, name)
        );
    """)
    conn.commit()
    return conn


def _norm_name(name: str) -> str:
    """规范化成员姓名（去空格、下划线、全角空格）。"""
    return (name or "").replace(" ", "").replace("　", "").replace("_", "").strip()


def _safe_filename(name: str) -> str:
    """生成安全的文件名。"""
    return re.sub(r'[\\/:*?"<>|#%&\s]', '_', name)


async def _download_avatar(client: httpx.AsyncClient, url: str, group_key: str, norm_name: str) -> str:
    """下载单个头像到 data/avatars/{group_key}/{norm_name}.jpg。"""
    if not url or not group_key or not norm_name:
        return ""
    try:
        dest_dir = AVATAR_DIR / group_key
        dest_dir.mkdir(parents=True, exist_ok=True)

        ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"
        fname = f"{_safe_filename(norm_name)}.{ext}"
        fpath = dest_dir / fname

        # 如果本地已存在且文件大小 > 500 字节，直接复用
        if fpath.exists() and fpath.stat().st_size > 500:
            return f"{group_key}/{fname}"

        r = await client.get(url, timeout=15, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.nogizaka46.com/" if "nogizaka" in group_key else "https://sakurazaka46.com/",
        })
        if r.status_code == 200 and len(r.content) > 200:
            fpath.write_bytes(r.content)
            return f"{group_key}/{fname}"
    except Exception as e:
        log_all(f"⚠️ 下载头像失败 ({group_key} - {norm_name}): {e}", is_debug=True)
    return ""


# ──────────────────────────────────────────────
# 官方网站头像抓取引擎
# ──────────────────────────────────────────────

async def fetch_nogizaka_avatars(client: httpx.AsyncClient) -> list[dict]:
    """从乃木坂46 官方 API 获取全部成员公式写真头像。"""
    url = "https://www.nogizaka46.com/s/n46/api/list/member"
    results = []
    try:
        r = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }, timeout=15)
        text = re.sub(r"^\w+\(", "", r.text).rstrip(");")
        data = json.loads(text)
        for m in data.get("data", []):
            name = (m.get("name") or "").strip()
            img = (m.get("img") or "").strip()
            if name and img:
                results.append({
                    "group_key": "nogizaka",
                    "name": _norm_name(name),
                    "display_name": name,
                    "avatar_url": img,
                })
    except Exception as e:
        log_all(f"⚠️ 获取乃木坂官方头像列表异常: {e}", is_debug=True)
    return results


async def fetch_sakurazaka_avatars(client: httpx.AsyncClient) -> list[dict]:
    """从樱坂46 官方成员名录获取全体成员写真头像。"""
    url = "https://sakurazaka46.com/s/s46/search/artist?ima=0000"
    results = []
    try:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        for li in soup.find_all("li", class_="box"):
            p_name = li.find(class_="name")
            img = li.find("img")
            if p_name and img:
                name = p_name.text.strip()
                src = img.get("src", "").strip()
                if src:
                    if src.startswith("/"):
                        src = "https://sakurazaka46.com" + src
                    results.append({
                        "group_key": "sakurazaka",
                        "name": _norm_name(name),
                        "display_name": name,
                        "avatar_url": src,
                    })
    except Exception as e:
        log_all(f"⚠️ 获取樱坂官方头像列表异常: {e}", is_debug=True)
    return results


async def fetch_hinatazaka_avatars(client: httpx.AsyncClient) -> list[dict]:
    """从日向坂46 官方成员名录获取全体成员写真头像。"""
    url = "https://www.hinatazaka46.com/s/official/search/artist?ima=0000"
    results = []
    try:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        for li in soup.find_all("li", class_="p-member__item"):
            div_name = li.find(class_="c-member__name")
            img = li.find("img")
            if div_name and img:
                name = div_name.text.strip()
                src = img.get("src", "").strip()
                if src:
                    if src.startswith("//"):
                        src = "https:" + src
                    elif src.startswith("/"):
                        src = "https://www.hinatazaka46.com" + src
                    results.append({
                        "group_key": "hinatazaka",
                        "name": _norm_name(name),
                        "display_name": name,
                        "avatar_url": src,
                    })
    except Exception as e:
        log_all(f"⚠️ 获取日向坂官方头像列表异常: {e}", is_debug=True)
    return results


# ──────────────────────────────────────────────
# 头像保存与批量同步
# ──────────────────────────────────────────────

def save_member_avatar_record(group_key: str, name: str, display_name: str, avatar_url: str, local_file: str = "") -> None:
    """持久化保存单条成员头像记录至 SQLite。"""
    if not group_key or not name or not avatar_url:
        return
    norm = _norm_name(name)
    disp = display_name or name
    now_ts = time.time()
    conn = get_avatar_db()
    try:
        with conn:
            conn.execute("""
                INSERT INTO member_avatars (group_key, name, display_name, avatar_url, local_file, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_key, name) DO UPDATE SET
                    display_name = excluded.display_name,
                    avatar_url = excluded.avatar_url,
                    local_file = CASE WHEN excluded.local_file != '' THEN excluded.local_file ELSE member_avatars.local_file END,
                    updated_at = excluded.updated_at;
            """, (group_key, norm, disp, avatar_url, local_file, now_ts))
    except Exception as e:
        log_all(f"⚠️ 保存头像记录失败: {e}", is_debug=True)


async def sync_all_avatars(force: bool = False) -> dict[str, int]:
    """并发同步并下载所有官方网站头像 + 消息账号头像。"""
    async with _lock:
        counts = {"total": 0, "downloaded": 0}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # 1. 抓取三坂官网头像列表
            nogi_list, sakura_list, hinata_list = await asyncio.gather(
                fetch_nogizaka_avatars(client),
                fetch_sakurazaka_avatars(client),
                fetch_hinatazaka_avatars(client),
                return_exceptions=False,
            )
            all_official = nogi_list + sakura_list + hinata_list
            counts["total"] = len(all_official)

            # 2. 并发下载与数据库更新
            sem = asyncio.Semaphore(10)

            async def _process_item(item: dict):
                async with sem:
                    g_key = item["group_key"]
                    norm = item["name"]
                    disp = item["display_name"]
                    url = item["avatar_url"]
                    local_f = await _download_avatar(client, url, g_key, norm)
                    save_member_avatar_record(g_key, norm, disp, url, local_f)
                    if local_f:
                        counts["downloaded"] += 1

            tasks = [_process_item(item) for item in all_official]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        log_all(f"✨ 成员与博客头像同步完成：已索引 {counts['total']} 位成员，成功缓存 {counts['downloaded']} 张本地头像")
        return counts


def get_member_avatar_path(name: str, group_key: str = "") -> Optional[str]:
    """查询指定成员/作者的本地头像文件路径（相对于 data/avatars/）。"""
    if not name:
        return None
    norm = _norm_name(name)
    conn = get_avatar_db()
    try:
        if group_key:
            row = conn.execute("""
                SELECT local_file, avatar_url FROM member_avatars
                WHERE name = ? AND group_key = ?
            """, (norm, group_key)).fetchone()
        else:
            row = conn.execute("""
                SELECT local_file, avatar_url FROM member_avatars
                WHERE name = ?
                ORDER BY CASE WHEN local_file != '' THEN 0 ELSE 1 END
                LIMIT 1
            """, (norm,)).fetchone()

        if row:
            local_file = row[0]
            if local_file and (AVATAR_DIR / local_file).exists() and (AVATAR_DIR / local_file).stat().st_size > 500:
                return local_file
    except Exception:
        pass

    # 兜底：直接按常规命名查找本地文件
    for g in ([group_key] if group_key else ["nogizaka", "sakurazaka", "hinatazaka", "yodel", "msg"]):
        for ext in ("jpg", "png", "webp", "jpeg"):
            cand = AVATAR_DIR / g / f"{_safe_filename(norm)}.{ext}"
            if cand.exists() and cand.stat().st_size > 500:
                return f"{g}/{_safe_filename(norm)}.{ext}"
    return None


def get_member_avatar_map() -> dict[str, str]:
    """返回全站成员规范名到本地头像 Web 路径的映射字典。"""
    conn = get_avatar_db()
    res: dict[str, str] = {}
    try:
        rows = conn.execute("""
            SELECT name, group_key, local_file, avatar_url FROM member_avatars
        """).fetchall()
        for r in rows:
            name, g_key, local_f, remote_url = r[0], r[1], r[2], r[3]
            if local_f and (AVATAR_DIR / local_f).exists() and (AVATAR_DIR / local_f).stat().st_size > 500:
                res[f"{g_key}:{name}"] = f"/api/archive/avatar?group={g_key}&name={name}"
                res[name] = f"/api/archive/avatar?group={g_key}&name={name}"
            elif remote_url:
                res[f"{g_key}:{name}"] = remote_url
                res[name] = remote_url
    except Exception:
        pass
    return res
