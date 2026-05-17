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
    "你是坂道系偶像团体的日文翻译。将以下成员消息翻译成中文（简体）：\n"
    "- 翻译自然流畅，保留原文的语气、口吻和情感\n"
    "- 颜文字（如(笑)(*´∀｀*)）和emoji保留原样，URL和话题标签不翻译\n"
    "- 只输出翻译结果\n\n"
    "原文：\n{text}"
)

def initialize() -> None:
    """在事件循环内调用，创建 asyncio.Lock。"""
    global _lock
    _lock = asyncio.Lock()

def _is_already_chinese(text: str) -> bool:
    """\u68c0\u67e5\u6587\u672c\u662f\u5426\u4e0d\u9700\u8981\u7ffb\u8bd1\u3002

    \u5224\u65ad\u4f9d\u636e\uff1a\u5047\u540d\u662f\u65e5\u6587\u7684\u53ef\u9760\u4fe1\u53f7\u2014\u2014\u53ea\u8981\u51fa\u73b0\u5047\u540d\u5c31\u9700\u8981\u7ffb\u8bd1\uff1b
    \u4e0d\u542b\u5047\u540d\u7684\u6587\u672c\uff08\u7eaf\u6c49\u5b57/\u82f1\u6587/emoji\uff09\u8df3\u8fc7\uff0c\u907f\u514d\u6d6a\u8d39 API \u914d\u989d\u3002
    """
    for c in text:
        if "\u3040" <= c <= "\u309f" or "\u30a0" <= c <= "\u30ff":
            return False
    return True

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
