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

_GROUP_DISPLAY: dict[str, str] = {
    "nogizaka46": "乃木坂46",
    "hinatazaka46": "日向坂46",
    "sakurazaka46": "櫻坂46",
    "yodel": "吉本坂46",
}

_PROMPT_TEMPLATE = (
    "你是坂道系偶像团体的日文翻译。"
    "以下消息来自{group_name}成员{member_name}，请翻译成中文（简体）：\n"
    "- 翻译自然流畅，保留原文的语气、口吻和情感\n"
    "- 颜文字（如(笑)(*´∀｀*)）和emoji保留原样，URL和话题标签不翻译\n"
    "- 不认识的日语人名保持原文，不要猜测或音译\n"
    "- 日语俚语、网络梗、团内用语等在译文后以「（注：…）」形式简要解释\n"
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

async def translate_text(text: str, member_name: str = "", group_type: str = "") -> str:
    """
    将日文翻译为中文。
    member_name / group_type 用于给翻译 AI 提供成员和团体的上下文，
    帮助更准确地翻译成员特有的语气和用词。
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

    group_name = _GROUP_DISPLAY.get(group_type, group_type or "坂道系")
    prompt = _PROMPT_TEMPLATE.format(
        group_name=group_name,
        member_name=member_name or "未知成员",
        text=text,
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
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
