# ============================================================
# translator.py — Gemini 翻译（串行化速率控制）
# ============================================================
import asyncio

import httpx

import config.config as cfg
from src.logger import log_all
from src.utils import RateLimiter

# ---- 模块级状态（由 initialize() 在事件循环内创建） ----
_limiter: RateLimiter = None   # type: ignore

_PROMPT_TEMPLATE = (
    "你是坂道系偶像团体的日文翻译。将以下成员消息翻译成中文（简体）：\n"
    "- 翻译自然流畅，保留原文的语气、口吻和情感\n"
    "- 颜文字（如(笑)(*´∀｀*)）和emoji保留原样，URL和话题标签不翻译\n"
    "- 只输出翻译结果\n\n"
    "原文：\n{text}"
)

def initialize() -> None:
    """在事件循环内调用，创建 RateLimiter（lambda 确保热重载后读取最新值）。"""
    global _limiter
    _limiter = RateLimiter(lambda: cfg.GEMINI_MIN_INTERVAL)

def _is_already_chinese(text: str) -> bool:
    """检查文本是否不需要翻译。

    判断依据：假名是日文的可靠信号——只要出现假名就需要翻译；
    不含假名的文本（纯汉字/英文/emoji）跳过，避免浪费 API 配额。
    """
    for c in text:
        if "\u3040" <= c <= "\u309f" or "\u30a0" <= c <= "\u30ff":
            return False
    return True

async def translate_text(text: str) -> str:
    """
    将日文翻译为中文。
    RateLimiter 覆盖「等待间隔 + HTTP 请求」全程，彻底串行化，
    杜绝并发请求触发 Gemini RPM 限制。
    """

    if not text or not text.strip():
        return text
    if _is_already_chinese(text):
        return text
    if len(text) > cfg.TRANSLATE_MAX_LENGTH:
        log_all(f"⚠️ 文本过长 ({len(text)} 字符)，跳过翻译", is_debug=True)
        return "[消息过长，暂不翻译]"

    payload = {
        "contents": [{"parts": [{"text": _PROMPT_TEMPLATE.format(text=text)}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
    }

    async with _limiter:
        for model in cfg.GEMINI_MODELS:
            url = f"{model['url']}?key={cfg.GEMINI_API_KEY}"
            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(timeout=cfg.TRANSLATE_TIMEOUT) as client:
                        resp = await client.post(
                            url, json=payload,
                            headers={"Content-Type": "application/json"},
                        )

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

    return "[翻译失败]"
