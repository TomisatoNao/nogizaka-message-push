# ============================================================
# translator.py — Gemini 翻译（串行化速率控制）
# ============================================================
import asyncio
import time

import httpx

from config.config import (
    GEMINI_API_KEY,
    GEMINI_MIN_INTERVAL,
    GEMINI_MODELS,
    TRANSLATE_MAX_LENGTH,
    TRANSLATE_TIMEOUT,
)
from src.logger import log_all

# ---- 模块级状态（由 initialize() 在事件循环内创建） ----
_lock: asyncio.Lock   = None   # type: ignore
_last_ts: float       = 0.0

_PROMPT_TEMPLATE = (
    "你是一个专业的日文翻译助手。请将以下日文内容翻译成中文（简体），要求：\n"
    "1. 翻译自然流畅，符合中文表达习惯，保留原文的语气和情感\n"
    "2. 对于偶像团体成员的对话，使用亲切的口吻\n"
    "3. 颜文字和表情符号（如 ✨💪🏻🎂）保留原样不翻译\n"
    "4. 只输出翻译结果，不要添加任何解释或说明\n"
    "5. 如果原文已经是中文，直接原样输出\n\n"
    "原文：\n{text}"
)

_CHINESE_CHARS = set("，。！？；：\u201c\u201d\u2018\u2019～（）…—\n\r\t ")


def initialize() -> None:
    """在事件循环内调用，创建 asyncio.Lock。"""
    global _lock
    _lock = asyncio.Lock()


def _is_already_chinese(text: str) -> bool:
    return all("\u4e00" <= c <= "\u9fff" or c in _CHINESE_CHARS for c in text)


async def translate_text(text: str) -> str:
    """
    将日文翻译为中文。
    _lock 覆盖「等待间隔 + HTTP 请求」全程，彻底串行化，
    杜绝并发请求触发 Gemini RPM 限制。
    """
    global _last_ts

    if not text or not text.strip():
        return text
    if _is_already_chinese(text):
        return text
    if len(text) > TRANSLATE_MAX_LENGTH:
        log_all(f"⚠️ 文本过长 ({len(text)} 字符)，跳过翻译", is_debug=True)
        return "[消息过长，暂不翻译]"

    payload = {
        "contents": [{"parts": [{"text": _PROMPT_TEMPLATE.format(text=text)}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
    }

    async with _lock:
        elapsed = time.monotonic() - _last_ts
        if elapsed < GEMINI_MIN_INTERVAL:
            await asyncio.sleep(GEMINI_MIN_INTERVAL - elapsed)

        for model in GEMINI_MODELS:
            url = f"{model['url']}?key={GEMINI_API_KEY}"
            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(timeout=TRANSLATE_TIMEOUT) as client:
                        resp = await client.post(
                            url, json=payload,
                            headers={"Content-Type": "application/json"},
                        )
                    _last_ts = time.monotonic()

                    if resp.status_code == 200:
                        data   = resp.json()
                        result = data["candidates"][0]["content"]["parts"][0]["text"]
                        return result.strip()
                    elif resp.status_code == 429:
                        await asyncio.sleep((attempt + 1) * 3)
                        continue
                    else:
                        break   # 其他错误，换下一个模型
                except Exception:
                    break

        _last_ts = time.monotonic()   # 即使全部失败也更新时间戳

    return "[翻译失败]"
