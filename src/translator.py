# ============================================================
# translator.py — Gemini 翻译（串行化速率控制）
# ============================================================
import asyncio
import hashlib

import httpx

import config.config as cfg
from src.logger import log_all
from src.utils import RateLimiter

# ---- 模块级状态（由 initialize() 在事件循环内创建） ----
_limiter: RateLimiter = None   # type: ignore
_http_client: httpx.AsyncClient | None = None   # 共享连接池；未注入时按需临时创建
_trans_cache: dict[tuple[str, str], str] = {}   # (member_name, text_hash) -> translated
_MAX_CACHE_SIZE = 1000

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

def _get_text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def initialize(client: httpx.AsyncClient | None = None) -> None:
    """在事件循环内调用，创建 RateLimiter 并注入共享 HTTP 客户端。
    （lambda 确保热重载后读取最新间隔值）"""
    global _limiter, _http_client
    _limiter = RateLimiter(lambda: cfg.GEMINI_MIN_INTERVAL)
    _http_client = client


async def _post_json(url: str, payload: dict) -> httpx.Response:
    """发送翻译请求。优先复用共享连接池（超时按请求覆盖），
    未注入时（如工具脚本单独调用）退回临时客户端。"""
    headers = {"Content-Type": "application/json"}
    if _http_client is not None:
        return await _http_client.post(
            url, json=payload, headers=headers, timeout=cfg.TRANSLATE_TIMEOUT,
        )
    async with httpx.AsyncClient(timeout=cfg.TRANSLATE_TIMEOUT) as client:
        return await client.post(url, json=payload, headers=headers)

def _is_already_chinese(text: str) -> bool:
    """检查文本是否不需要翻译。

    判断依据：假名是日文的可靠信号——只要出现假名就需要翻译；
    不含假名的文本（纯汉字/英文/emoji）跳过，避免浪费 API 配额。
    """
    for c in text:
        if "\u3040" <= c <= "\u309f" or "\u30a0" <= c <= "\u30ff":
            return False
    return True

def _extract_text(data: dict, model_name: str) -> str:
    """从 Gemini 响应中取出译文，取不到时返回空字符串（由调用方降级下一个模型）。

    只取第一个**非思考**段：带思考的模型会把推理过程也放进 parts 并标记
    `thought: true`，直接取 `parts[0]` 可能拿到推理内容而不是译文。
    `MAX_TOKENS` 截断时宁可换模型，也不推送半句译文。
    """
    candidates = data.get("candidates") or []
    if not candidates:
        log_all(f"⚠️ 翻译模型 {model_name} 响应无 candidates", is_error=True)
        return ""

    candidate = candidates[0]
    finish = candidate.get("finishReason", "")
    parts = (candidate.get("content") or {}).get("parts") or []

    if finish == "MAX_TOKENS":
        log_all(f"⚠️ 翻译模型 {model_name} 输出被 MAX_TOKENS 截断，改用下一个模型", is_error=True)
        return ""

    for part in parts:
        part_text = part.get("text", "")
        if part_text and not part.get("thought"):
            return part_text.strip()

    log_all(
        f"⚠️ 翻译模型 {model_name} 未返回可用文本段 "
        f"(finishReason={finish or '?'}, parts={len(parts)})，改用下一个模型",
        is_error=True,
    )
    return ""


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

    cache_key = (member_name, _get_text_hash(text))
    if cache_key in _trans_cache:
        log_all(f"⚡ 命中翻译内存缓存 ({member_name})", is_debug=True)
        return _trans_cache[cache_key]

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
                    resp = await _post_json(url, payload)

                    if resp.status_code == 200:
                        result = _extract_text(resp.json(), model["name"])
                        if result:
                            if len(_trans_cache) >= _MAX_CACHE_SIZE:
                                _trans_cache.pop(next(iter(_trans_cache)))
                            _trans_cache[cache_key] = result
                            return result
                        break   # 响应结构异常，换下一个模型
                    elif resp.status_code == 429:
                        await asyncio.sleep((attempt + 1) * 3)
                        continue
                    else:
                        log_all(
                            f"⚠️ 翻译模型 {model['name']} 返回 HTTP {resp.status_code}，改用下一个模型",
                            is_debug=True,
                        )
                        break
                except Exception as e:
                    log_all(
                        f"⚠️ 翻译模型 {model['name']} 请求异常: {type(e).__name__}: {e}",
                        is_debug=True,
                    )
                    break

    return "[翻译失败]"

