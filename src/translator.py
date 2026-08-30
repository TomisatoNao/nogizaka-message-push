# ============================================================
# translator.py — Gemini 翻译（解耦段落级对照引擎与模型 Failover）
# ============================================================
import asyncio
import hashlib
import html as html_lib
import json
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup, NavigableString, Comment
import httpx

import config.config as cfg
from src.logger import log_all
from src.utils import RateLimiter

# ---- 模块级状态（由 initialize() 在事件循环内创建） ----
_limiter: RateLimiter = None   # type: ignore
_http_client: httpx.AsyncClient | None = None   # 共享连接池；未注入时按需临时创建
_trans_cache: dict[tuple[str, str], str] = {}   # (member_name, text_hash) -> translated
_blog_cache: dict[tuple[str, str], str] = {}    # (member_name, html_hash) -> translated_html
_blog_structured_cache: dict[tuple[str, str], tuple] = {}   # (member_name, html_hash) -> (结构化块列表, 模型名)
_MAX_CACHE_SIZE = 1000
_round_robin_counter: int = 0

_GROUP_DISPLAY: dict[str, str] = {
    "nogizaka46": "乃木坂46",
    "hinatazaka46": "日向坂46",
    "sakurazaka46": "櫻坂46",
    "yodel": "吉本坂46",
}

_PROMPT_TEMPLATE = (
    "你是一名专业、精准的日中翻译器。\n"
    "以下是{group_name}成员{member_name}发送的一条消息正文，请将其翻译为简体中文。\n\n"
    "【核心翻译原则（极其重要，违者视为严重错误）】：\n"
    "1. 忠实直译，严禁脑补扩写（绝对硬性红线）：\n"
    "   - 严格忠实于原文：原文说了什么就翻译什么，字面含义是什么就翻译什么。\n"
    "   - 【绝对严禁】自行脑补、续写、扩写、编造或添加任何原文中不存在的问候、天气、反问、寒暄或对话内容！\n"
    "   - 当原文仅为简短词句、拟声词或叹词（如「いひひ」「えへへ」「うーん」）时，【仅需翻译该词本身】（如「嘻嘻」「嘿嘿」「唔……」），严禁添加任何额外句子！\n"
    "2. 角色定位：\n"
    "   - 你只是中立的文本翻译器，绝不是偶像本人也不是粉丝，严禁以第一人称或反问口吻与用户聊天互动。\n"
    "3. 专有名词与人名爱称规范：\n"
    "   - 假名爱称：平假名/片假名昵称（如「xxちゃん」「xxたん」）保留日文假名+酱（如「xx酱」），严禁擅自硬译为生僻汉字。\n"
    "   - 汉字全名：仅在原文出现完整汉字姓名时才使用标准汉字。\n"
    "   - 节目与活动通用译名：如「ミーグリ」➔「线上见面会」，「乃木坂工事中」等。\n"
    "4. 格式与符号：\n"
    "   - 原文中的 Emoji、颜文字、空格及换行排版 1:1 完整保留。\n"
    "5. 纯净输出：\n"
    "   - 绝对不要输出任何前言、后记、翻译说明或注释，直接输出纯净译文！\n\n"
    "原文：\n{text}"
)

_BLOG_JSON_PROMPT_TEMPLATE = (
    "你是一名精通坂道系列偶像文化（乃木坂46/櫻坂46/日向坂46）的官方博客资深翻译官。\n"
    "以下是{group_name}成员{member_name}最新博客的正文段落列表（以 JSON 列表格式提供）。\n\n"
    "【核心任务】：\n"
    "请全局通读整篇博客，在充分理解上下文语境、叙事逻辑和偶像感情色彩的前提下，将列表中每一项的 text 翻译为自然流畅、亲切诚挚的简体中文（Strict Simplified Chinese）。\n\n"
    "【翻译与润色规范（极其重要）】：\n"
    "1. 严防错位与漏项（绝对硬性要求）：\n"
    "   - 待翻译列表中每一项包含 id, prefix, text。输出必须为一个合法的 JSON 列表（List of Objects），每个对象必须包含与输入【1:1 完全一致】的 id 与 prefix，以及翻译好的 zh。\n"
    "   - 绝对禁止将两个或多个段落合并为一个！每一项的 zh 必须精准对应翻译该项的 text！\n"
    "2. 语言与用词规范：\n"
    "   - 必须严格且统一使用标准简体中文（Simplified Chinese），严禁混入繁体字（如「總是」「憂鬱」「電話」等），严禁直接复制日文原文！\n"
    "   - 严禁将发语词「まず」机械直译为「率先」，必须根据语境翻译为「首先 / 这一次 / 首先我想」等符合中文表达习惯的词汇。\n"
    "   - 假名爱称硬性规则：平假名/片假名昵称（如「あやめちゃん」「なぎちゃん」）必须保留日文假名+酱（如「あやめ酱」「なぎ酱」）或使用罗马字（如「ayame酱」），【严禁】擅自查字典硬译为汉字（如严禁将あやめ译为菖蒲）！\n"
    "   - 汉字全名规则：仅在原文出现完整汉字姓名（如「筒井あやめ」「冨里奈央」）时才使用标准汉字。\n"
    "   - 结合前后文准确翻译活动（如「ミーグリ」➔「线上见面会」）、节目与曲名。\n"
    "3. 边缘字符与格式：\n"
    "   - 纯颜文字/Emoji/符号段落保留原样，切勿自行添加解释。\n"
    "   - 遇到段落内的换行符 \\n，译文中请务必在对应位置保留换行符 \\n。\n\n"
    "【JSON 输出严格约束】：\n"
    "直接输出合法 JSON 列表（允许 ```json ... ``` 包裹），绝对禁止输出任何前言、总结、注释或额外对话。\n\n"
    "待翻译博客段落列表：\n{json_payload}"
)

def _get_text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def initialize(client: httpx.AsyncClient | None = None) -> None:
    """在事件循环内调用，创建 RateLimiter 并注入共享 HTTP 客户端。"""
    global _limiter, _http_client
    _limiter = RateLimiter(lambda: cfg.GEMINI_MIN_INTERVAL)
    _http_client = client


async def _post_json(url: str, payload: dict, headers: dict | None = None, custom_client: httpx.AsyncClient = None, timeout: float | None = None) -> httpx.Response:
    """发送翻译请求。优先复用当前事件循环内可用的共享连接池。"""
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req_timeout = timeout if timeout is not None else getattr(cfg, "TRANSLATE_TIMEOUT", 90)

    if custom_client is not None and not custom_client.is_closed:
        return await custom_client.post(url, json=payload, headers=req_headers, timeout=req_timeout)

    try:
        curr_loop = asyncio.get_running_loop()
    except RuntimeError:
        curr_loop = None

    if _http_client is not None and not _http_client.is_closed and curr_loop is not None and curr_loop.is_running():
        # 尝试检查 client transport 绑定的 loop
        transport = getattr(_http_client, "_transport", None)
        t_loop = getattr(transport, "_loop", None)
        if t_loop is None or t_loop is curr_loop:
            try:
                return await _http_client.post(url, json=payload, headers=req_headers, timeout=req_timeout)
            except RuntimeError as ex:
                if "Event loop" in str(ex):
                    pass  # 跨 loop 导致异常，进入下方独立客户端 fallback
                else:
                    raise

    proxy_url = getattr(cfg, "PROXY", "") or None
    async with httpx.AsyncClient(timeout=req_timeout, proxy=proxy_url) as client:
        return await client.post(url, json=payload, headers=req_headers)


def _is_already_chinese(text: str) -> bool:
    """检查文本是否不需要翻译。判断依据：出现假名则必须翻译；纯汉字/英文/符号跳过。"""
    for c in text:
        if "\u3040" <= c <= "\u309f" or "\u30a0" <= c <= "\u30ff":
            return False
    return True

def _get_active_models() -> list[dict]:
    """获取当前已配置有效 API Key 的可用模型列表（支持 Gemini 与 智谱 GLM 等多平台）。"""
    has_gemini = bool(getattr(cfg, "GEMINI_API_KEY", ""))
    has_zhipu = bool(getattr(cfg, "ZHIPU_API_KEY", ""))

    active: list[dict] = []
    models = getattr(cfg, "GEMINI_MODELS", []) or []
    for m in models:
        name = m.get("name", "")
        url = m.get("url", "")
        provider = m.get("provider", "")
        if not provider:
            if "bigmodel.cn" in url or name.lower().startswith("glm"):
                provider = "zhipu"
            else:
                provider = "gemini"

        if provider == "zhipu" and has_zhipu:
            active.append({**m, "provider": "zhipu"})
        elif provider == "gemini" and has_gemini:
            active.append({**m, "provider": "gemini"})

    return active

def _get_round_robin_models() -> list[dict]:
    """按 Round-Robin 算法选取本次请求的模型尝试序列（各平台智能轮流交替，失败自动 Failover）。"""
    global _round_robin_counter
    models = _get_active_models()
    if not models:
        return []
    start_idx = _round_robin_counter % len(models)
    _round_robin_counter += 1
    return models[start_idx:] + models[:start_idx]

def _extract_text_gemini(data: dict, model_name: str) -> str:
    """从 Gemini 响应中取出译文。"""
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

def _parse_json_response(res_text: str) -> dict[str, str]:
    """安全解析模型返回的 JSON 响应（支持 List[Object] 锚点列表 与 Dict[str, str] 字典）。"""
    res_text = res_text.strip()
    if res_text.startswith("```json"):
        res_text = res_text[7:]
    elif res_text.startswith("```"):
        res_text = res_text[3:]
    if res_text.endswith("```"):
        res_text = res_text[:-3]
    res_text = res_text.strip()
    try:
        data = json.loads(res_text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        if isinstance(data, list):
            res_dict = {}
            for item in data:
                if isinstance(item, dict):
                    k = str(item.get("id", item.get("key", len(res_dict))))
                    v = item.get("zh", item.get("text", ""))
                    res_dict[k] = str(v)
            return res_dict
    except Exception as e:
        log_all(f"⚠️ 解析翻译模型 JSON 响应失败: {e}", is_debug=True)
    return {}

async def _call_model_text(model: dict, prompt: str, custom_client: httpx.AsyncClient = None) -> str:
    """按 provider 规范请求单条文本翻译（带 15s 快速超时以保障多模型快速 Failover）。"""
    provider = model.get("provider", "gemini")
    text_timeout = getattr(cfg, "TRANSLATE_TEXT_TIMEOUT", 15.0)
    if provider == "zhipu":
        url = model.get("url") or "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.ZHIPU_API_KEY}"}
        payload = {
            "model": model["name"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 4096,
        }
        resp = await _post_json(url, payload, headers=headers, custom_client=custom_client, timeout=text_timeout)
        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices") or []
            if choices:
                return (choices[0].get("message", {}).get("content") or "").strip()
        elif resp.status_code == 429:
            raise httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
        else:
            log_all(f"⚠️ 智谱模型 {model['name']} 返回 HTTP {resp.status_code}", is_debug=True)
            return ""
    else:
        url = f"{model['url']}?key={cfg.GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
        }
        resp = await _post_json(url, payload, custom_client=custom_client, timeout=text_timeout)
        if resp.status_code == 200:
            return _extract_text_gemini(resp.json(), model["name"])
        elif resp.status_code == 429:
            raise httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
        else:
            log_all(f"⚠️ Gemini 模型 {model['name']} 返回 HTTP {resp.status_code}", is_debug=True)
            return ""
    return ""

async def _call_model_json(model: dict, prompt: str, custom_client: httpx.AsyncClient = None) -> dict[str, str]:
    """按 provider 规范请求结构化 JSON 字典/列表翻译。"""
    provider = model.get("provider", "gemini")
    if provider == "zhipu":
        url = model.get("url") or "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.ZHIPU_API_KEY}"}
        payload = {
            "model": model["name"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 4096,
        }
        resp = await _post_json(url, payload, headers=headers, custom_client=custom_client)
        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices") or []
            if choices:
                raw_text = choices[0].get("message", {}).get("content") or ""
                return _parse_json_response(raw_text)
        elif resp.status_code == 429:
            raise httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
        else:
            log_all(f"⚠️ 智谱模型 {model['name']} 返回 HTTP {resp.status_code}", is_debug=True)
    else:
        url = f"{model['url']}?key={cfg.GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
        }
        resp = await _post_json(url, payload, custom_client=custom_client)
        if resp.status_code == 200:
            raw_text = _extract_text_gemini(resp.json(), model["name"])
            if raw_text:
                return _parse_json_response(raw_text)
        elif resp.status_code == 429:
            raise httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
        else:
            log_all(f"⚠️ Gemini 模型 {model['name']} 返回 HTTP {resp.status_code}", is_debug=True)
    return {}

async def _do_translate_gemini_json(
    items: dict[str, str],
    member_name: str,
    group_type: str,
    custom_client: httpx.AsyncClient = None
) -> tuple[dict[str, str], str]:
    """纯文本段落组批量整体翻译（日文前缀语义锚点绑定 + 双引擎智能轮流 Round-Robin + 自动 Failover 降级）。

    返回 (译文映射 {str(id): zh}, 成功使用的模型名)；无可用结果时返回 ({}, "")。
    """
    if not items:
        return {}, ""

    try_models = _get_round_robin_models()
    if not try_models:
        return {}, ""

    # 构建带 prefix 原文前缀字符锚点的结构化列表
    items_list = []
    for k, text in items.items():
        clean_text = text.replace("\n", " ").strip()
        prefix = clean_text[:8] if clean_text else "空"
        items_list.append({
            "id": k,
            "prefix": prefix,
            "text": text
        })

    group_name = _GROUP_DISPLAY.get(group_type, group_type or "坂道系")
    payload_text = json.dumps(items_list, ensure_ascii=False, indent=2)
    prompt = _BLOG_JSON_PROMPT_TEMPLATE.format(
        group_name=group_name,
        member_name=member_name or "未知成员",
        json_payload=payload_text,
    )

    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(lambda: cfg.GEMINI_MIN_INTERVAL)

    async with _limiter:
        for model in try_models:
            for attempt in range(2):
                try:
                    parsed_json = await _call_model_json(model, prompt, custom_client=custom_client)
                    if parsed_json:
                        return parsed_json, model["name"]
                    break  # 未返回有效 JSON，切换下一个模型
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    break
                except Exception as e:
                    log_all(f"⚠️ 翻译模型 {model['name']} 请求异常: {type(e).__name__}: {e}", is_debug=True)
                    break

    return {}, ""

async def translate_text_with_model(
    text: str, member_name: str = "", group_type: str = "", custom_client: httpx.AsyncClient = None
) -> tuple[str, str]:
    """普通文本消息翻译接口（返回 (译文, 翻译模型名)），支持多引擎智能轮番调度与 Failover。"""
    if not text or not text.strip():
        return text, ""
    if _is_already_chinese(text):
        return text, ""
    try_models = _get_round_robin_models()
    if not try_models:
        return text, ""
    if len(text) > cfg.TRANSLATE_MAX_LENGTH:
        log_all(f"⚠️ 文本过長 ({len(text)} 字符)，跳过翻译", is_debug=True)
        return "[消息过长，暂不翻译]", ""

    cache_key = (member_name, _get_text_hash(text))
    if cache_key in _trans_cache:
        cached = _trans_cache[cache_key]
        log_all(f"⚡ 命中翻译内存缓存 ({member_name})", is_debug=True)
        if isinstance(cached, tuple):
            return cached
        return cached, ""

    group_name = _GROUP_DISPLAY.get(group_type, group_type or "坂道系")
    prompt = _PROMPT_TEMPLATE.format(
        group_name=group_name,
        member_name=member_name or "未知成员",
        text=text,
    )

    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(lambda: cfg.GEMINI_MIN_INTERVAL)

    async with _limiter:
        for model in try_models:
            for attempt in range(2):
                try:
                    result = await _call_model_text(model, prompt, custom_client=custom_client)
                    if result:
                        model_name = model.get("name", "")
                        entry = (result, model_name)
                        if len(_trans_cache) >= _MAX_CACHE_SIZE:
                            _trans_cache.pop(next(iter(_trans_cache)))
                        _trans_cache[cache_key] = entry
                        return entry
                    break
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        await asyncio.sleep((attempt + 1) * 2)
                        continue
                    break
                except Exception as e:
                    log_all(f"⚠️ 翻译模型 {model['name']} 请求异常: {type(e).__name__}: {e}", is_debug=True)
                    break

    return "[翻译失败]", ""

async def translate_text(text: str, member_name: str = "", group_type: str = "", custom_client: httpx.AsyncClient = None) -> str:
    """普通文本消息翻译接口（兼容旧接口，直接返回译文字符串）"""
    res, _ = await translate_text_with_model(text, member_name, group_type, custom_client=custom_client)
    return res

# ── 博客正文节点化拆解：保留 <img> 原位 + 段落级切分 ──

_GROUP_BASE_URLS = {
    "nogizaka": "https://www.nogizaka46.com",
    "nogizaka46": "https://www.nogizaka46.com",
    "sakurazaka": "https://sakurazaka46.com",
    "sakurazaka46": "https://sakurazaka46.com",
    "hinatazaka": "https://www.hinatazaka46.com",
    "hinatazaka46": "https://www.hinatazaka46.com",
}

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

# 空白判定：普通空白 + 全角空格(U+3000) + 不间断空格(U+00A0) + 各类零宽/窄空格
_WS_RE = re.compile(r"[\s\u00a0\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]")
_WS_RUN_RE = re.compile(r"(?:[\s\u00a0\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff])+")


def _resolve_img_src(src: str, group_type: str) -> str:
    """把相对/协议相对路径的图片 src 补全为绝对 URL（供前端替换 + 防盗链处理）。"""
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith(("http://", "https://")):
        return src
    base = _GROUP_BASE_URLS.get(group_type, _GROUP_BASE_URLS["nogizaka"])
    return urljoin(base, src)


def _is_empty_block(node) -> bool:
    """判定 <p>/<div> 是否为「空块」：无图片且无可读文本（仅空白/换行）。"""
    if node.find("img") is not None:
        return False
    return not _WS_RE.sub("", node.get_text(""))


def _flatten_html_blocks(html: str, group_type: str) -> list[tuple[str, str]]:
    """把博客 HTML 解析为有序块列表，按「视效大段落」粒度切分，逐图保留原位。

    返回 [(kind, content)]，kind ∈ {"text", "img"}：
      - "text": 一个视效大段落的日文文本（段落内换行保留为 \\n）
      - "img":  补全后的图片绝对 URL

    官方博客 DOM 的段落分隔实际有 5 种形态，统一归一为「空行 = 段落边界」：
      * br-based（约 60%）：单容器内 <br> 换行，连续 >=2 个 <br> 为空行；
      * p-per-line（约 27%）：连续非空 <p> 为同一段，空 <p> 为空行；
      * div-based（约 12%）：同 p-per-line，但载体是 <div>；
      * img-mostly / mixed：仅少量图片+文本。

    实现：文档序递归走树，<br> 与「非空块结束」都只记一个换行（块边界去重，
    避免 <br></p> 被算成空行）；空块记两个换行（空行）；<img> 先 flush 当前段再
    独立成块。最后按「两个及以上换行」把缓冲切成若干大段落。
    """
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if not text:
            return
        # 两个及以上换行 = 空行 → 切出大段落；段落内单换行保留
        for part in re.split(r"\n{2,}", text):
            part = part.strip()
            if part:
                blocks.append(("text", part))

    def newline(force: bool) -> None:
        # force=True：<br> 本身（连续 <br> 要累计成空行，不能去重）
        # force=False：非空块结束（去重，避免紧跟 <br></p> 被算成两个换行）
        if force or not buf or buf[-1] != "\n":
            buf.append("\n")

    def walk(node) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            s = _WS_RUN_RE.sub(" ", str(node))
            if s.strip():
                buf.append(s)
            return
        name = getattr(node, "name", None)
        if not name:
            return
        if name in _SKIP_TAGS:
            return
        if name == "br":
            newline(force=True)
            return
        if name == "img":
            flush()
            abs_src = _resolve_img_src(node.get("src", ""), group_type)
            if abs_src:
                blocks.append(("img", abs_src))
            # 不能 return：畸形 HTML（如源码里多出的 </img>）会让 BeautifulSoup
            # 把后续内容错误地嵌套进 <img> 子节点，继续递归才能不丢后续文字/图片
            for child in node.children:
                walk(child)
            return
        if name in ("p", "div"):
            if _is_empty_block(node):
                newline(force=True)   # 空行
                newline(force=True)
                return
            for child in node.children:
                walk(child)
            newline(force=False)      # 非空块结束 = 换行（与下一个非空块合并成段）
            return
        # 其它内联/未知标签透明透传
        for child in node.children:
            walk(child)

    for child in soup.children:
        walk(child)
    flush()
    return blocks


def blocks_to_html(blocks: list[dict]) -> str:
    """把结构化块列表渲染为交织 HTML（日中对照视图，供推送/旧接口用）。

    块结构：[{"type":"text","jp":str,"zh":str}, {"type":"img","src":str}]
    日文 <em> 斜体、中文 <span> 常规体；zh 为空则仅日文。
    """
    if not blocks:
        return ""
    parts: list[str] = []
    for b in blocks:
        if b.get("type") == "img":
            parts.append(f'<img src="{b["src"]}" referrerpolicy="no-referrer" loading="lazy">')
            continue
        jp_html = html_lib.escape(b.get("jp", "")).replace("\n", "<br>")
        zh = (b.get("zh") or "").strip()
        if zh and "[翻译失败]" not in zh:
            zh_html = html_lib.escape(zh).replace("\n", "<br>")
            parts.append(f"<em>{jp_html}</em><br><span>{zh_html}</span>")
        else:
            parts.append(f"<em>{jp_html}</em>")
    return "<br><br>".join(parts)


async def translate_blog_structured(html: str, member_name: str = "", group_type: str = "", custom_client: httpx.AsyncClient = None) -> tuple[list[dict], str]:
    """
    结构化博客翻译接口（解耦存储：绝不把日中文本硬拼接成单一文本）。

    返回 (有序块列表, 翻译模型名)，jp / zh 分离存储、图片原位：
      [{"type": "text", "jp": "日文原文", "zh": "中文译文"},  # zh="" 表示该段暂无译文
       {"type": "img",  "src": "https://..."}]
    无 API key / 空输入 / 无需翻译时返回 ([], "")。
    """
    if not html or (not getattr(cfg, "GEMINI_API_KEY", "") and not getattr(cfg, "ZHIPU_API_KEY", "")):
        return [], ""

    cache_key = (member_name, _get_text_hash(html))
    if cache_key in _blog_structured_cache:
        log_all(f"⚡ 命中博客结构化翻译内存缓存 ({member_name})", is_debug=True)
        return _blog_structured_cache[cache_key]

    # 1. 节点化拆解：段落文本块 + 图片块（按原文顺序交替）
    blocks = _flatten_html_blocks(html, group_type)

    # 2. 收集需翻译的文本块（跳过纯中文/符号块），顺序键 → 全局块索引
    items_to_translate: dict[str, str] = {}
    seq_map: dict[int, str] = {}
    counter = 0
    for idx, (kind, content) in enumerate(blocks):
        if kind == "text" and content and not _is_already_chinese(content):
            key = str(counter)
            items_to_translate[key] = content
            seq_map[idx] = key
            counter += 1

    if not items_to_translate:
        return [], ""  # 无需翻译

    # 3. 动态自适应大批次（支持全篇单次吞吐，最大 5000 字符 / 60 段，兼顾全篇全局文脉与大模型高吞吐量）
    batches = []
    curr_batch = []
    curr_len = 0
    all_keys = list(items_to_translate.keys())
    for k in all_keys:
        t_len = len(items_to_translate[k])
        if curr_batch and (curr_len + t_len > 5000 or len(curr_batch) >= 60):
            batches.append(curr_batch)
            curr_batch = [k]
            curr_len = t_len
        else:
            curr_batch.append(k)
            curr_len += t_len
    if curr_batch:
        batches.append(curr_batch)

    translated_map: dict[str, str] = {}
    model_name = ""

    for batch_keys in batches:
        batch_items = {k: items_to_translate[k] for k in batch_keys}
        res_map, mname = await _do_translate_gemini_json(batch_items, member_name, group_type, custom_client=custom_client)
        translated_map.update(res_map)
        if not model_name and mname:
            model_name = mname

    # 3.5 模型偶发漏项/空值/串行重复校验自愈
    missing = []
    keys_sorted = sorted(items_to_translate.keys(), key=lambda x: int(x) if x.isdigit() else 999)
    for i, k in enumerate(keys_sorted):
        zh = (translated_map.get(k) or "").strip()
        if not zh or "[翻译失败]" in zh:
            missing.append(k)
        elif i > 0:
            prev_k = keys_sorted[i - 1]
            prev_zh = (translated_map.get(prev_k) or "").strip()
            # 若两段非简短符号译文完全一致，而日文原文明显不同，判定为串行复制，加入补译队列
            if zh == prev_zh and len(zh) > 8 and items_to_translate[k].strip() != items_to_translate[prev_k].strip():
                missing.append(k)

    if missing:
        retry_items = {k: items_to_translate[k] for k in missing}
        res_map, mname = await _do_translate_gemini_json(retry_items, member_name, group_type, custom_client=custom_client)
        translated_map.update(res_map)
        if not model_name and mname:
            model_name = mname

    # 4. 组装结构化块（jp / zh 解耦，图片原位）
    structured: list[dict] = []
    for idx, (kind, content) in enumerate(blocks):
        if kind == "img":
            structured.append({"type": "img", "src": content})
            continue
        key = seq_map.get(idx)
        zh_text = translated_map.get(key, "") if key is not None else ""
        if zh_text and "[翻译失败]" not in zh_text:
            structured.append({"type": "text", "jp": content, "zh": zh_text})
        else:
            structured.append({"type": "text", "jp": content, "zh": ""})

    result = (structured, model_name)
    if len(_blog_structured_cache) >= _MAX_CACHE_SIZE:
        _blog_structured_cache.pop(next(iter(_blog_structured_cache)))
    _blog_structured_cache[cache_key] = result
    return result


async def translate_blog_html(html: str, member_name: str = "", group_type: str = "", custom_client: httpx.AsyncClient = None) -> str:
    """HTML 博客翻译接口（兼容旧调用）：返回交织的日中对照 HTML。"""
    blocks, _ = await translate_blog_structured(html, member_name, group_type, custom_client)
    return blocks_to_html(blocks)
