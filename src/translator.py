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

_BLOG_HTML_PROMPT_TEMPLATE = (
    "你是坂道系列偶像团体的资深粉丝翻译。"
    "以下是一篇{group_name}成员{member_name}的日文博客HTML源码。\n"
    "请按要求将内容翻译成简体中文，并返回处理后的完整HTML源码：\n"
    "1. 严格保留所有的HTML标签（如<img>, <div>, <p>, <span>等）和原有属性，绝对不能破坏图片排版和原有空行数量。\n"
    "2. 将博客正文替换为“双语对照”格式。必须遵守以下【严格排版规则】：\n"
    "   - 【段落划分规则】：\n"
    "     * 由单行或单个 <br> 连接的连续文本属于【同一个段落】。同段落内的日文合成一个 <strong> 块，对应的中文合成一个 <em> 块。\n"
    "     * 被多个空行/换行（如 <br><br> 或 <br><br><br>）隔开的文本属于【不同的段落】！不同段落必须各自独立成块，绝对不能把被空行隔开的两个段落强行合成一个 <strong> 块。\n"
    "     * 段落之间的多个空行 <br>，必须放在前一个段落的 <em> 译文之后、下一个段落的 <strong> 原文之前！\n"
    "   - 【对照格式】：\n"
    "     * 先输出当前段落的日文原文（用 <strong> 加粗包围），紧接着换行 <br>，然后输出对应的中文译文（用 <em> 斜体包围）。\n"
    "     * 译文内部的换行要和原文内部的换行一一对应（原文同段内有几行，译文同段内就有几行）。\n"
    "   [正确示范]：\n"
    "   <p>\n"
    "   <strong>少し遅くなってしまいましたが、😢<br>音楽の日 DREAMダンス企画ありがとうございました！！！</strong><br>\n"
    "   <em>虽然稍微有点迟了，😢<br>音乐之日 DREAM舞蹈企划非常感谢！！！</em>\n"
    "   <br><br><br>\n"
    "   <strong>坂道グループ選抜として、皆さんと一緒に...</strong><br>\n"
    "   <em>作为坂道集团选拔成员，和大家一起...</em>\n"
    "   </p>\n"
    "   [错误示范]（把被空行隔开的两个段落强行合成了一段）：\n"
    "   <p><strong>少し遅くなって...<br>音楽の日...<br><br><br>坂道グループ選抜として...</strong>...</p>\n"
    "3. 不认识的人名保持日文原文，专有名词参考中文粉丝圈通用译法。\n"
    "4. 直接输出纯HTML代码，不要添加任何markdown代码块语法（如```html），绝对不能有前言或结语。\n\n"
    "以下是原HTML源码：\n{html}"
)

def _get_text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def initialize(client: httpx.AsyncClient | None = None) -> None:
    """在事件循环内调用，创建 RateLimiter 并注入共享 HTTP 客户端。
    （lambda 确保热重载后读取最新间隔值）"""
    global _limiter, _http_client
    _limiter = RateLimiter(lambda: cfg.GEMINI_MIN_INTERVAL)
    _http_client = client


async def _post_json(url: str, payload: dict, custom_client: httpx.AsyncClient = None) -> httpx.Response:
    """发送翻译请求。优先复用共享连接池（超时按请求覆盖），
    未注入时（如工具脚本单独调用）退回临时客户端。"""
    headers = {"Content-Type": "application/json"}
    if custom_client is not None:
        return await custom_client.post(
            url, json=payload, headers=headers, timeout=cfg.TRANSLATE_TIMEOUT,
        )
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


async def _do_translate_gemini(text: str, is_html: bool, member_name: str, group_type: str, custom_client: httpx.AsyncClient = None) -> str:
    if not is_html:
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
    else:
        group_name = _GROUP_DISPLAY.get(group_type, group_type or "坂道系")
        prompt = _BLOG_HTML_PROMPT_TEMPLATE.format(
            group_name=group_name,
            member_name=member_name or "未知成员",
            html=text,
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
        }

    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(lambda: cfg.GEMINI_MIN_INTERVAL)

    async with _limiter:
        for model in cfg.GEMINI_MODELS:
            url = f"{model['url']}?key={cfg.GEMINI_API_KEY}"
            for attempt in range(2):
                try:
                    resp = await _post_json(url, payload, custom_client=custom_client)

                    if resp.status_code == 200:
                        result = _extract_text(resp.json(), model["name"])
                        if result:
                            if is_html:
                                if result.startswith("```html"):
                                    result = result[7:]
                                if result.startswith("```"):
                                    result = result[3:]
                                if result.endswith("```"):
                                    result = result[:-3]
                                result = result.strip()
                                return result
                            else:
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

async def translate_text(text: str, member_name: str = "", group_type: str = "", custom_client: httpx.AsyncClient = None) -> str:
    """普通文本翻译接口"""
    if not text or not getattr(cfg, "GEMINI_API_KEY", ""):
        return text
    return await _do_translate_gemini(text, False, member_name, group_type, custom_client=custom_client)

async def translate_blog_html(html: str, member_name: str = "", group_type: str = "", custom_client: httpx.AsyncClient = None) -> str:
    """HTML结构化翻译接口"""
    if not html or not getattr(cfg, "GEMINI_API_KEY", ""):
        return html
    return await _do_translate_gemini(html, True, member_name, group_type, custom_client=custom_client)


