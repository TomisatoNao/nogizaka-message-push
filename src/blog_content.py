"""博客正文内容规范化与结构提取模块。

统一提取、校验并规范化博客正文块（文本与图片）：
- 支持自动抓取 post (body, body_html) 与数据库 row (body_text, body_html)；
- 严格解耦翻译状态与正文呈现，翻译失败或无译文时保证按原文顺序输出完整图文；
- 提供安全的 content_json 校验与降级，避免非列表/非法对象导致异常中断；
- 供长图卡片渲染 (blog_card_renderer)、WebUI 卡片导出 (blog_card_download)
  以及通知推送渠道 (notifier) 共享使用。
"""
from __future__ import annotations

from dataclasses import dataclass
import html as html_lib
import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString

_GROUP_BASE_URLS = {
    "nogizaka": "https://www.nogizaka46.com",
    "nogizaka46": "https://www.nogizaka46.com",
    "sakurazaka": "https://sakurazaka46.com",
    "sakurazaka46": "https://sakurazaka46.com",
    "hinatazaka": "https://www.hinatazaka46.com",
    "hinatazaka46": "https://www.hinatazaka46.com",
}

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "iframe", "svg", "object"})
_WS_RE = re.compile(r"[\s\u00a0\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]")
_WS_RUN_RE = re.compile(r"(?:[\s\u00a0\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff])+")


def _resolve_img_src(src: str, group_type: str = "") -> str:
    """补全相对路径或协议相对路径的图片 URL。"""
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith(("http://", "https://", "data:")):
        return src
    base = _GROUP_BASE_URLS.get(group_type.lower(), _GROUP_BASE_URLS["nogizaka"])
    return urljoin(base, src)


def _is_empty_block(node: Any) -> bool:
    """判定 <p>/<div> 是否为空块：无图片且无可读文本（仅空白/换行）。"""
    if node.find("img") is not None:
        return False
    return not _WS_RE.sub("", node.get_text(""))


def parse_blog_html_to_blocks(html: str, group_type: str = "") -> list[dict]:
    """把博客 HTML 解析为有序结构化块列表：
    - text: {"type": "text", "jp": str, "zh": ""}
    - img: {"type": "img", "src": str}
    文档序递归遍历，精确保留图文穿插顺序与自然大段落。
    """
    if not html or not isinstance(html, str):
        return []

    soup = BeautifulSoup(html, "html.parser")
    blocks: list[dict] = []
    buf: list[str] = []

    def flush() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if not text:
            return
        for part in re.split(r"\n{2,}", text):
            part = part.strip()
            if part:
                blocks.append({"type": "text", "jp": part, "zh": ""})

    def newline(force: bool) -> None:
        if force or not buf or buf[-1] != "\n":
            buf.append("\n")

    def walk(node: Any) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            s = _WS_RUN_RE.sub(" ", str(node))
            if s.strip():
                buf.append(s)
            return
        name = getattr(node, "name", None)
        if not name or name in _SKIP_TAGS:
            return
        if name == "br":
            newline(force=True)
            return
        if name == "img":
            flush()
            abs_src = _resolve_img_src(node.get("src", ""), group_type)
            blocks.append({"type": "img", "src": abs_src})
            for child in node.children:
                walk(child)
            return
        if name in ("p", "div", "section", "article", "li"):
            if _is_empty_block(node):
                newline(force=True)
                newline(force=True)
                return
            for child in node.children:
                walk(child)
            newline(force=False)
            return
        for child in node.children:
            walk(child)

    for child in soup.children:
        walk(child)
    flush()
    return blocks


def flatten_html_blocks(html: str, group_type: str = "") -> list[tuple[str, str]]:
    """返回 [(kind, content)] 形式的有序块列表，供 translator 等既有调用者复用。"""
    raw_blocks = parse_blog_html_to_blocks(html, group_type)
    result: list[tuple[str, str]] = []
    for b in raw_blocks:
        if b.get("type") == "img":
            result.append(("img", b.get("src") or ""))
        else:
            result.append(("text", b.get("jp") or ""))
    return result


def parse_plain_text_to_blocks(text: str) -> list[dict]:
    """将纯文本按照自然段落（连续空行）解析为 text 块。"""
    if not text or not isinstance(text, str):
        return []
    blocks: list[dict] = []
    parts = re.split(r"\n{2,}", text)
    for p in parts:
        p = p.strip()
        if p:
            blocks.append({"type": "text", "jp": p, "zh": ""})
    return blocks


def parse_legacy_translation_html(html_str: str) -> list[dict]:
    """解析传统 <em>jp</em><br><span>zh</span> 的翻译 HTML 为结构化块。"""
    if not html_str or not isinstance(html_str, str):
        return []
    soup = BeautifulSoup(html_str, "html.parser")
    blocks: list[dict] = []

    elements = soup.find_all(["em", "img", "p"])
    for el in elements:
        if el.name == "img":
            src = el.get("src", "")
            blocks.append({"type": "img", "src": src})
        elif el.name == "em":
            ja_text = el.get_text("\n").strip()
            next_node = el.next_sibling
            zh_text = ""
            while next_node:
                name = getattr(next_node, "name", None)
                if name in ("em", "p", "img"):
                    break
                if name == "span":
                    zh_text = next_node.get_text("\n").strip()
                    break
                next_node = next_node.next_sibling
            if ja_text:
                blocks.append({"type": "text", "jp": ja_text, "zh": zh_text})
    return blocks


def validate_and_normalize_content_json(content_json_raw: Any) -> list[dict] | None:
    """严格校验并规范化 content_json。
    必须为包含合法字典的列表；非列表、非字典、畸形结构或无法解析时返回 None。
    """
    if not content_json_raw:
        return None
    data = content_json_raw
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None
    if not isinstance(data, list):
        return None

    normalized: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip()
        if item_type == "img":
            normalized.append({"type": "img", "src": str(item.get("src") or "")})
        else:
            jp = str(item.get("jp") or "").strip()
            zh = str(item.get("zh") or "").strip()
            if not jp and not zh:
                continue
            normalized.append({"type": "text", "jp": jp, "zh": zh})

    return normalized if normalized else None


def blocks_to_interleaved_html(blocks: list[dict]) -> str:
    """把结构化块列表渲染为交织 HTML（日中对照视图，供推送/旧接口用）。
    日文 <em> 斜体、中文 <span> 常规体；zh 为空则仅日文。
    """
    if not blocks:
        return ""
    parts: list[str] = []
    for b in blocks:
        if b.get("type") == "img":
            src = html_lib.escape(str(b.get("src") or ""))
            parts.append(f'<img src="{src}" referrerpolicy="no-referrer" loading="lazy">')
            continue
        jp_html = html_lib.escape(str(b.get("jp") or "")).replace("\n", "<br>")
        zh = str(b.get("zh") or "").strip()
        if zh and "[翻译失败]" not in zh:
            zh_html = html_lib.escape(zh).replace("\n", "<br>")
            parts.append(f"<em>{jp_html}</em><br><span>{zh_html}</span>")
        else:
            parts.append(f"<em>{jp_html}</em>")
    return "<br><br>".join(parts)


@dataclass
class NormalizedBlogContent:
    blocks: list[dict]
    mode: str
    actual_mode: str
    translation_state: str  # "full" | "partial" | "none" | "image_only" | "empty"
    total_text_blocks: int
    translated_text_blocks: int
    has_images: bool
    model_name: str
    badge_text: str
    fallback_reason: str


def normalize_blog_content(post: dict, *, mode: str = "ja-zh") -> NormalizedBlogContent:
    """统一提取并规范化博客正文块。
    支持自动抓取 post (body, body_html) 与数据库 row (body_text, body_html)。
    严格解耦翻译状态与正文呈现，翻译失败时保证按原文顺序输出完整图文。
    """
    group_key = str(post.get("group_key") or "").lower()
    raw_model = str(post.get("translation_model") or "").strip()
    req_mode = mode if mode in ("ja-zh", "ja-only", "zh-only") else "ja-zh"

    body_html = post.get("body_html") or ""
    body_text = post.get("body_text") or post.get("body") or ""
    content_json_raw = post.get("content_json") or ""
    raw_translation = post.get("translation") or ""

    # 1. 尝试从 content_json 提取已校验的结构化块
    candidate_blocks = validate_and_normalize_content_json(content_json_raw)

    blocks: list[dict] = []
    fallback_reason = ""

    if candidate_blocks:
        blocks = candidate_blocks
    else:
        # 2. 从 body_html 解析有序原文及图片
        if body_html and isinstance(body_html, str):
            blocks = parse_blog_html_to_blocks(body_html, group_key)
            if not candidate_blocks and content_json_raw:
                fallback_reason = "invalid_or_truncated_content_json"
            elif not content_json_raw:
                fallback_reason = "no_content_json"

        # 3. body_html 未解析到文本时，从纯文本 body_text / body 补齐
        text_count = sum(1 for b in blocks if b.get("type") != "img")
        if text_count == 0 and body_text:
            text_blocks = parse_plain_text_to_blocks(body_text)
            if text_blocks:
                img_blocks = [b for b in blocks if b.get("type") == "img"]
                blocks = text_blocks + img_blocks
                fallback_reason = "fallback_to_plain_text"

        # 4. 若上述均无且 translation 字段有传统 HTML，尝试提取
        if not blocks and raw_translation:
            blocks = parse_legacy_translation_html(raw_translation)
            if blocks:
                fallback_reason = "fallback_to_legacy_translation_html"

    # 统计文本与有效译文
    text_blocks = [b for b in blocks if b.get("type") != "img"]
    total_text = len(text_blocks)
    translated_count = 0
    for b in text_blocks:
        zh = (b.get("zh") or "").strip()
        if zh and "[翻译失败]" not in zh:
            translated_count += 1

    img_count = sum(1 for b in blocks if b.get("type") == "img")
    has_images = img_count > 0

    # 确定翻译状态
    if total_text == 0:
        translation_state = "image_only" if has_images else "empty"
    elif translated_count == total_text:
        translation_state = "full"
    elif translated_count > 0:
        translation_state = "partial"
    else:
        translation_state = "none"

    # 确定实际模式 actual_mode
    if req_mode == "ja-only":
        actual_mode = "ja-only"
    elif req_mode == "zh-only":
        actual_mode = "zh-only" if translated_count > 0 else "ja-only"
    else:  # "ja-zh"
        actual_mode = "ja-zh" if translated_count > 0 else "ja-only"

    # 确定徽标文字 badge_text
    model_name = raw_model if translation_state in ("full", "partial") else ""
    if translation_state == "empty":
        badge_text = ""
    elif translation_state == "image_only":
        badge_text = "📷 原文写真"
    elif translation_state == "none":
        badge_text = "📖 原文 · 暂无译文"
    elif translation_state == "partial":
        if actual_mode == "zh-only":
            badge_text = f"🤖 中文译文（部分段落暂无译文） · {model_name}" if model_name else "🤖 中文译文（部分段落暂无译文）"
        else:
            badge_text = f"🤖 AI 双语精读对照（部分段落暂无译文） · {model_name}" if model_name else "🤖 AI 双语精读对照（部分段落暂无译文）"
    else:  # full
        if actual_mode == "zh-only":
            badge_text = f"🤖 中文译文 · {model_name}" if model_name else "🤖 中文译文"
        else:
            badge_text = f"🤖 AI 智能双语精读对照 · {model_name}" if model_name else "🤖 AI 智能双语精读对照"

    return NormalizedBlogContent(
        blocks=blocks,
        mode=req_mode,
        actual_mode=actual_mode,
        translation_state=translation_state,
        total_text_blocks=total_text,
        translated_text_blocks=translated_count,
        has_images=has_images,
        model_name=model_name,
        badge_text=badge_text,
        fallback_reason=fallback_reason,
    )
