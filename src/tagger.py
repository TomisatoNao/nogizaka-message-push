# ============================================================
# tagger.py — 图片自动打标签：用 Gemini Flash Lite 分析本地图片
#             生成 3-5 个中文标签（场景/物品/人物状态）
# ============================================================
import asyncio
import base64
from pathlib import Path

import httpx

import config.config as cfg
from src.logger import log_all
from src.utils import RateLimiter

_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_TAG_PROMPT = (
    "这是乃木坂46成员在粉丝App上发的日常照片。\n"
    "从以下类别中选出最匹配的1-3个，用空格分隔：\n"
    "\n"
    "自拍 合照 舞台 外出 美食 玩偶 动物 花草 风景 截图\n"
    "\n"
    "各类别说明：\n"
    "- 自拍：单人自拍、大头照\n"
    "- 合照：两人或以上的合影\n"
    "- 舞台：演出、排练、工作相关\n"
    "- 外出：街拍、旅行、户外\n"
    "- 美食：食物、饮料\n"
    "- 玩偶：布偶、周边、手办\n"
    "- 动物：猫狗等宠物\n"
    "- 花草：花、植物\n"
    "- 风景：景色、天空、城市\n"
    "- 截图：海报、公告截图、专辑封面\n"
    "\n"
    "只能从上面10个类别中选，不要自创类别。\n"
    "只输出类别词，用空格分隔。"
)

# ── 模块级状态 ──
_limiter: RateLimiter | None = None
_http_client: httpx.AsyncClient | None = None


def _get_limiter() -> RateLimiter:
    """获取或自愈创建限流器（确保即使未显式调用 initialize 也能正常工作）。"""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(lambda: cfg.GEMINI_TAG_MIN_INTERVAL)
    return _limiter


def initialize(client: httpx.AsyncClient | None = None) -> None:
    """在事件循环内调用，创建 RateLimiter 并注入共享 HTTP 客户端。"""
    global _limiter, _http_client
    _limiter = RateLimiter(lambda: cfg.GEMINI_TAG_MIN_INTERVAL)
    _http_client = client


async def _post_json(url: str, payload: dict) -> httpx.Response:
    """发送 Gemini 请求。优先复用共享连接池。"""
    headers = {"Content-Type": "application/json"}
    if _http_client is not None:
        return await _http_client.post(url, json=payload, headers=headers, timeout=30)
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.post(url, json=payload, headers=headers)


async def tag_image(member_dir: str, local_file: str) -> str:
    """读取本地图片，调 Gemini Vision 打标签，返回空格分隔的标签字符串。
    失败时返回空字符串。

    Args:
        member_dir: 归档中的成员目录名（如 '冨里奈央'）
        local_file: 图片相对路径（如 '2026/07/images/xxx.jpg'）

    Returns:
        标签字符串（如 '舞台 笑容 挥手'），失败返回 ''
    """
    if not cfg.ENABLE_IMAGE_TAGGING:
        return ""

    # 读取本地图片
    from src.archive import member_dir_name
    img_path = Path(cfg.ARCHIVE_DIR) / member_dir_name(member_dir) / local_file
    if not img_path.is_file():
        log_all(f"⚠️ 图片打标签：文件不存在 {img_path}", is_debug=True)
        return ""

    try:
        with open(img_path, "rb") as f:
            img_data = f.read()
    except OSError as e:
        log_all(f"⚠️ 图片打标签：读取失败 {img_path}: {e}", is_debug=True)
        return ""

    img_b64 = base64.b64encode(img_data).decode("utf-8")
    suffix = img_path.suffix.lower()
    mime = _MIME_MAP.get(suffix, "image/jpeg")

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime, "data": img_b64}},
                {"text": _TAG_PROMPT},
            ]
        }],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 64},
    }

    limiter = _get_limiter()
    async with limiter:
        for model in cfg.GEMINI_TAG_MODELS:
            url = f"{model['url']}?key={cfg.GEMINI_API_KEY}"
            for attempt in range(2):
                try:
                    resp = await _post_json(url, payload)

                    if resp.status_code == 200:
                        data = resp.json()
                        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
                        for part in parts:
                            text = part.get("text", "")
                            if text and not part.get("thought"):
                                tags = text.strip()
                                count = len(tags.split())
                                if 1 <= count <= 5:
                                    return tags
                                elif count > 5:
                                    return " ".join(tags.split()[:5])
                        log_all(f"⚠️ 打标签模型 {model['name']} 响应无可用文本，换下一个", is_debug=True)
                        break
                    elif resp.status_code == 429:
                        await asyncio.sleep((attempt + 1) * 3)
                        continue
                    else:
                        log_all(f"⚠️ 打标签模型 {model['name']} HTTP {resp.status_code}，换下一个", is_debug=True)
                        break
                except Exception as e:
                    log_all(f"⚠️ 打标签模型 {model['name']} 异常: {e}", is_debug=True)
                    break

    return ""


# 后台任务池
_bg_tasks: set = set()


def schedule_tag(member_dir: str, msg: dict) -> None:
    """后台异步打标签 + 写回归档（幂等，已存在 _tags 的跳过）。
    不阻塞调用方。
    """
    if not cfg.ENABLE_IMAGE_TAGGING:
        return
    if msg.get("type") not in ("picture", "image"):
        return
    if msg.get("_tags"):
        return  # 已有标签，跳过
    local_file = msg.get("_local_file", "")
    if not local_file:
        return
    task = asyncio.create_task(
        _do_tag(member_dir, msg, local_file)
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _do_tag(member_dir: str, msg: dict, local_file: str) -> None:
    """内部：打标签并合并写回归档（全异常捕获保障）。"""
    try:
        tags = await tag_image(member_dir, local_file)
        if not tags:
            return

        # 写回归档（复用 archive.py 的 _merge_write）
        from src.archive import _merge_write
        from datetime import datetime as _dt

        utc_str = msg.get("updated_at") or msg.get("published_at", "")
        try:
            dt = _dt.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            log_all(f"⚠️ 打标签：无法解析时间戳 {utc_str!r}", is_debug=True)
            return

        delta = {
            "id": msg.get("id"),
            "updated_at": msg.get("updated_at"),
            "_tags": tags,
        }
        await _merge_write(member_dir, dt, delta)
        log_all(f"🏷️ [{member_dir}] 图片打标签完成: {tags}")
    except Exception as e:
        log_all(f"⚠️ 后台打标签异常 [{member_dir}/{local_file}]: {e}", is_debug=True)


async def wait_pending(timeout: float = 60) -> None:
    """等待后台打标签任务收尾（优雅停机用）。"""
    if _bg_tasks:
        await asyncio.wait(list(_bg_tasks), timeout=timeout)
