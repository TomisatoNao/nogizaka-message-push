# ============================================================
# bilibili.py — B站动态发布（串行化速率控制）
# ============================================================
import asyncio

import httpx

import config.config as cfg
from src.logger import log_all
from src.utils import RateLimiter

# ---- 模块级状态（由 initialize() 在事件循环内创建） ----
_limiter: RateLimiter = None   # type: ignore

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":    "https://www.bilibili.com/",
    "Origin":     "https://www.bilibili.com",
}


def initialize() -> None:
    """在事件循环内调用，创建 RateLimiter（lambda 确保热重载后读取最新值）。"""
    global _limiter
    _limiter = RateLimiter(lambda: cfg.BILIBILI_MIN_INTERVAL)


def extract_bili_jct(cookie_str: str) -> str:
    """从 Cookie 字符串中提取 bili_jct，失败时返回空字符串。"""
    for item in cookie_str.split(";"):
        if "bili_jct=" in item:
            return item.split("bili_jct=")[1].strip()
    return ""


def resolve_cookie(member: dict) -> tuple[str, str]:
    """
    返回该成员应使用的 (cookie, bili_jct)。
    成员未配置独立 cookie（或值为空字符串）时回退全局默认。
    """
    cookie   = member.get("bilibili_cookie") or cfg.BILIBILI_FULL_COOKIE
    bili_jct = extract_bili_jct(cookie) or cfg.BILIBILI_BILI_JCT
    return cookie, bili_jct


async def post_dynamic(text: str, cookie: str, bili_jct: str) -> bool:
    """
    发布一条 B站纯文字动态。
    RateLimiter 覆盖速率等待 + HTTP 请求全程，多成员并发时严格串行，
    杜绝竞态导致速率限制失效。
    """

    if not text or not text.strip():
        return False

    async with _limiter:

        payload = {
            "dynamic_id":  0,
            "content":     text[:4500],
            "type":        4,
            "csrf":        bili_jct,
            "csrf_token":  bili_jct,
        }

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        cfg.BILIBILI_POST_API,
                        data=payload,
                        headers={**_HEADERS, "Cookie": cookie},
                    )

                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 0:
                        dyn_id = data.get("data", {}).get("dynamic_id", "N/A")
                        log_all(f"✅ B站动态发布成功 (id: {dyn_id})")
                        return True
                    log_all(f"⚠️ B站发布失败: {data.get('message', '未知错误')}", is_error=True)
                    return False

                elif resp.status_code == 412:
                    log_all("⚠️ B站风控 412，等待重试...", is_error=True)
                    await asyncio.sleep(5 * (attempt + 1))
                    continue

                else:
                    log_all(f"🚨 B站 HTTP {resp.status_code}: {resp.text[:200]}", is_error=True)
                    if attempt < 2:
                        await asyncio.sleep(3)
                    else:
                        return False

            except Exception as e:
                log_all(f"🔥 B站发布异常: {type(e).__name__}: {e}", is_error=True)
                if attempt < 2:
                    await asyncio.sleep(3)
                else:
                    return False

        return False
