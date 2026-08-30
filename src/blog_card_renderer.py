"""
博客精美长图卡片渲染器 (Blog Image Card Renderer)
基于 Playwright Headless Chromium + 自适应杂志级排版模板。
支持乃木坂46、櫻坂46、日向坂46三大团体官方主题色自适应、中日双语精读对照、高清原图原位无损嵌入与无黑边视网膜级出图。
"""

import re
import json
import base64
import html as html_lib
from pathlib import Path
from typing import Optional
from PIL import Image

from src.logger import log_all

# 三团品牌视觉配置
GROUP_THEMES = {
    "nogizaka": {
        "name": "乃木坂46",
        "badge_icon": "🟣",
        "gradient": "linear-gradient(135deg, #8a2be2 0%, #742581 45%, #4a154b 100%)",
        "accent": "#c084fc",
        "tag": "Nogizaka46 Official Blog",
    },
    "sakurazaka": {
        "name": "櫻坂46",
        "badge_icon": "🌸",
        "gradient": "linear-gradient(135deg, #f472b6 0%, #ec4899 45%, #be185d 100%)",
        "accent": "#f9a8d4",
        "tag": "Sakurazaka46 Official Diary",
    },
    "hinatazaka": {
        "name": "日向坂46",
        "badge_icon": "☀️",
        "gradient": "linear-gradient(135deg, #38bdf8 0%, #0284c7 45%, #0369a1 100%)",
        "accent": "#7dd3fc",
        "tag": "Hinatazaka46 Official Diary",
    },
}

DEFAULT_THEME = {
    "name": "官方博客",
    "badge_icon": "📝",
    "gradient": "linear-gradient(135deg, #6366f1 0%, #4f46e5 45%, #3730a3 100%)",
    "accent": "#818cf8",
    "tag": "Official Member Diary",
}

_PLAYWRIGHT_AVAILABLE: Optional[bool] = None


def is_playwright_available() -> bool:
    """检查当前 Python 环境是否支持 Playwright。"""
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is not None:
        return _PLAYWRIGHT_AVAILABLE
    try:
        import playwright  # noqa: F401
        _PLAYWRIGHT_AVAILABLE = True
    except ImportError:
        _PLAYWRIGHT_AVAILABLE = False
    return _PLAYWRIGHT_AVAILABLE


def _file_to_base64(filepath: Path) -> str:
    if filepath.exists() and filepath.stat().st_size > 0:
        try:
            with open(filepath, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
                suffix = filepath.suffix.lower()
                mime = "image/png" if suffix == ".png" else "image/jpeg"
                return f"data:{mime};base64,{data}"
        except Exception:  # nosec B110
            pass
    return ""


def _bytes_to_base64(data: bytes, ext: str = "jpg") -> str:
    if data:
        b64 = base64.b64encode(data).decode("utf-8")
        mime = "image/png" if ext == "png" else "image/jpeg"
        return f"data:{mime};base64,{b64}"
    return ""


def _get_author_avatar_b64(author: str, group_key: str = "") -> str:
    """获取博客作者的官方本地缓存头像 Base64 编码。"""
    try:
        from src import avatar_manager
        # 1. 尝试从 avatar_manager 获取本地缓存文件路径
        rel_path = avatar_manager.get_member_avatar_path(author, group_key)
        if rel_path:
            full_path = Path("data/avatars") / rel_path
            b64 = _file_to_base64(full_path)
            if b64:
                return b64

        # 2. 若指定了 group_key 但没找到，尝试跨组查找
        if group_key:
            rel_path_any = avatar_manager.get_member_avatar_path(author)
            if rel_path_any:
                full_path = Path("data/avatars") / rel_path_any
                b64 = _file_to_base64(full_path)
                if b64:
                    return b64
    except Exception as e:
        log_all(f"⚠️ 获取博客作者头像失败 ({author}): {e}", is_debug=True)
    return ""


def _generate_html(post: dict, image_b64_list: list[str]) -> str:
    """根据博客数据与三团配置生成自适应 HTML 模板字符串（大字号排版 + 原文译文紧凑跟随 + 段落间距舒适）。"""
    group_key = post.get("group_key", "").lower()
    theme = GROUP_THEMES.get(group_key, DEFAULT_THEME)

    author = post.get("author", "成员")
    title = post.get("title", "无标题")
    date_str = post.get("date", "")
    trans_model = post.get("translation_model") or "AI 双语翻译引擎"

    content_json_raw = post.get("content_json") or ""
    structured_blocks = []
    if content_json_raw:
        try:
            structured_blocks = json.loads(content_json_raw) if isinstance(content_json_raw, str) else content_json_raw
        except Exception:
            structured_blocks = []

    body_elements = []
    img_idx = 0

    if structured_blocks:
        for b_idx, b in enumerate(structured_blocks):
            if b.get("type") == "img":
                if img_idx < len(image_b64_list) and image_b64_list[img_idx]:
                    body_elements.append(
                        f'<div class="blog-img-wrap">'
                        f'<img src="{image_b64_list[img_idx]}" class="blog-img" alt="写真 {img_idx + 1}"/>'
                        f'</div>'
                    )
                img_idx += 1
            else:
                jp = (b.get("jp") or "").strip()
                zh = (b.get("zh") or "").strip()
                if not jp and not zh:
                    continue
                jp_escaped = html_lib.escape(jp).replace("\n", "<br>")
                zh_escaped = html_lib.escape(zh).replace("\n", "<br>")

                is_last = (b_idx == len(structured_blocks) - 1)
                is_sig = is_last and len(jp) < 30 and (
                    any(g in jp for g in ("乃木坂", "櫻坂", "日向坂"))
                    or author in jp
                    or bool(re.search(r"[0-9]{1,2}[./月][0-9]{1,2}", jp))
                )
                block_cls = "para-block signature-block" if is_sig else "para-block"
                elem_html = f'<div class="{block_cls}">'
                if jp_escaped:
                    elem_html += f'<div class="jp-text">{jp_escaped}</div>'
                if zh_escaped and "[翻译失败]" not in zh_escaped and (not is_sig or zh_escaped != jp_escaped):
                    elem_html += f'<div class="zh-text">{zh_escaped}</div>'
                elem_html += '</div>'
                body_elements.append(elem_html)

        # 兜底：如果还有剩余未渲染的图片，附在正文后
        while img_idx < len(image_b64_list):
            if image_b64_list[img_idx]:
                body_elements.append(
                    f'<div class="blog-img-wrap">'
                    f'<img src="{image_b64_list[img_idx]}" class="blog-img" alt="写真 {img_idx + 1}"/>'
                    f'</div>'
                )
            img_idx += 1

        processed_body = "\n".join(body_elements)
    else:
        # 兼容传统 HTML 结构，去除 em 与 span 间冗余的空行 <br>
        raw_html = post.get("translation") or post.get("body_text") or ""
        raw_html = re.sub(r'</em>\s*(?:<br\s*/?>\s*)+<span>', '</em><span>', raw_html)

        img_counter = [0]

        def _replace_img(match):
            idx = img_counter[0]
            img_counter[0] += 1
            if idx < len(image_b64_list) and image_b64_list[idx]:
                return (
                    f'<div class="blog-img-wrap">'
                    f'<img src="{image_b64_list[idx]}" class="blog-img" alt="写真 {idx + 1}"/>'
                    f'</div>'
                )
            return ""

        processed_body = re.sub(r'<img\s+src="[^"]+"\s*/?>', _replace_img, raw_html)

        # 兜底：若正文中未匹配完所有图片，将剩余图片附在文末
        if img_counter[0] < len(image_b64_list):
            trailing_imgs = []
            for idx in range(img_counter[0], len(image_b64_list)):
                if image_b64_list[idx]:
                    trailing_imgs.append(
                        f'<div class="blog-img-wrap">'
                        f'<img src="{image_b64_list[idx]}" class="blog-img" alt="写真 {idx + 1}"/>'
                        f'</div>'
                    )
            if trailing_imgs:
                processed_body += "\n" + "\n".join(trailing_imgs)

    valid_images_count = sum(1 for b in image_b64_list if b)
    author_avatar_b64 = post.get("author_avatar_b64") or _get_author_avatar_b64(author, group_key)
    if author_avatar_b64:
        avatar_html = f'<img src="{author_avatar_b64}" class="author-avatar" alt="{author}"/>'
    else:
        initial = author[:1] if author else "🌸"
        avatar_html = f'<div class="author-avatar-fallback">{initial}</div>'

    html_code = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    background: transparent;
    margin: 0;
    padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", "Noto Sans JP", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}

  .card-container {{
    width: 900px;
    background: #121422;
    overflow: hidden;
    color: #e2e8f0;
    position: relative;
  }}

  /* Header Section */
  .card-header {{
    background: {theme["gradient"]};
    padding: 38px 44px 34px;
    position: relative;
    color: #ffffff;
  }}
  .card-header::after {{
    content: "";
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: 20px;
    background: #121422;
    border-radius: 24px 24px 0 0;
  }}

  .header-top {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 22px;
  }}
  .brand-badge {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(255, 255, 255, 0.25);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255, 255, 255, 0.4);
    padding: 7px 18px;
    border-radius: 30px;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 0.05em;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  }}
  .time-badge {{
    font-size: 15px;
    color: rgba(255, 255, 255, 0.95);
    font-weight: 600;
    letter-spacing: 0.03em;
  }}

  .author-row {{
    display: flex;
    align-items: center;
    gap: 20px;
    margin-bottom: 18px;
  }}
  .author-avatar {{
    width: 72px;
    height: 72px;
    border-radius: 50%;
    border: 3px solid #ffffff;
    object-fit: cover;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
    flex-shrink: 0;
  }}
  .author-avatar-fallback {{
    width: 72px;
    height: 72px;
    border-radius: 50%;
    border: 3px solid #ffffff;
    background: rgba(255, 255, 255, 0.2);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 32px;
    font-weight: bold;
    color: #ffffff;
    flex-shrink: 0;
  }}
  .author-meta {{
    display: flex;
    flex-direction: column;
    gap: 4px;
  }}
  .author-name {{
    font-size: 28px;
    font-weight: 800;
    letter-spacing: 0.03em;
    text-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
  }}
  .author-tag {{
    font-size: 14.5px;
    color: rgba(255, 255, 255, 0.9);
    font-weight: 500;
  }}

  .blog-title {{
    font-size: 32px;
    font-weight: 800;
    line-height: 1.45;
    letter-spacing: 0.02em;
    color: #ffffff;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
    margin-top: 6px;
  }}

  .ai-badge {{
    display: inline-flex;
    align-items: center;
    gap: 7px;
    margin-top: 16px;
    background: rgba(0, 0, 0, 0.28);
    border: 1px solid rgba(255, 255, 255, 0.28);
    padding: 6px 16px;
    border-radius: 10px;
    font-size: 14.5px;
    color: rgba(255, 255, 255, 0.95);
    font-weight: 500;
  }}

  /* Content Body with Crisp Large Typography & Tight Bilingual Pairing */
  .card-body {{
    padding: 24px 44px 40px;
    font-size: 24px;
    line-height: 1.8;
  }}

  .para-block {{
    margin-bottom: 28px;
  }}

  .signature-block {{
    margin-top: 36px;
    margin-bottom: 8px;
    text-align: right;
  }}
  .signature-block .jp-text, .signature-block .zh-text {{
    font-size: 21px;
    color: #94a3b8;
    font-weight: 500;
    line-height: 1.6;
  }}

  .jp-text, .card-body em {{
    display: block;
    color: #94a3b8;
    font-style: normal;
    font-size: 20px;
    line-height: 1.65;
    margin-bottom: 4px;
    opacity: 0.92;
    letter-spacing: 0.01em;
  }}

  .zh-text, .card-body span {{
    display: block;
    color: #f8fafc;
    font-weight: 600;
    font-size: 24px;
    line-height: 1.75;
    margin-bottom: 0;
    letter-spacing: 0.015em;
  }}

  .card-body p {{
    margin-bottom: 28px;
  }}

  .card-body a {{
    color: {theme["accent"]};
    text-decoration: none;
    word-break: break-all;
    font-weight: 500;
  }}

  /* Image styling */
  .blog-img-wrap {{
    margin: 28px 0;
    text-align: center;
  }}
  .blog-img {{
    width: 100%;
    border-radius: 20px;
    box-shadow: 0 12px 36px rgba(0, 0, 0, 0.5);
    display: block;
  }}

  /* Footer Section */
  .card-footer {{
    background: #0b0c16;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    padding: 26px 44px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 15px;
    color: #64748b;
  }}
  .footer-brand {{
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 600;
    color: #94a3b8;
  }}
  .footer-right {{
    font-size: 14.5px;
    color: #475569;
    font-weight: 500;
  }}
</style>
</head>
<body>

<div class="card-container" id="cardContainer">
  <div class="card-header">
    <div class="header-top">
      <div class="brand-badge">{theme["badge_icon"]} {theme["name"]} 官方博客</div>
      <div class="time-badge">{date_str} JST</div>
    </div>
    <div class="author-row">
      {avatar_html}
      <div class="author-meta">
        <div class="author-name">{author}</div>
        <div class="author-tag">{theme["tag"]}</div>
      </div>
    </div>
    <div class="blog-title">{title}</div>
    <div class="ai-badge">🤖 AI 智能双语精读对照 · {trans_model}</div>
  </div>

  <div class="card-body">
    {processed_body}
  </div>

  <div class="card-footer">
    <div class="footer-brand">🌸 坂道联合监控系统 · 自动推送归档</div>
    <div class="footer-right">写真共 {valid_images_count} 张 · 官方原图无损呈现</div>
  </div>
</div>

</body>
</html>
"""
    return html_code


_WARNED_PLAYWRIGHT_MISSING = False


async def render_blog_card(post: dict) -> Optional[Path]:
    """渲染指定博客的长图卡片，返回生成的高清 JPG 图片绝对路径。

    若 Playwright 不可用或渲染异常，返回 None 实现优雅降级。
    支持自动从本地磁盘或远程 URL 加载全量写真，确保卡片 100% 包含图片。
    """
    global _WARNED_PLAYWRIGHT_MISSING
    if not is_playwright_available():
        if not _WARNED_PLAYWRIGHT_MISSING:
            _WARNED_PLAYWRIGHT_MISSING = True
            log_all(
                "💡 Playwright 未安装，博客长图卡片功能已自动降级为标准图文推送。"
                "（如需开启长图卡片，请在环境内执行: pip install playwright && playwright install --with-deps chromium）"
            )
        return None

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    image_paths = post.get("image_paths") or []
    image_urls = post.get("images") or []
    if isinstance(image_urls, str):
        try:
            image_urls = json.loads(image_urls)
        except Exception:
            image_urls = []

    image_b64_list = []
    for p_str in image_paths:
        b64 = ""
        if isinstance(p_str, str) and p_str:
            p = Path(p_str)
            if not p.is_absolute():
                p = Path("data/blog_images") / p
            b64 = _file_to_base64(p)
        image_b64_list.append(b64)

    # 兜底：如果本地文件读取为空，但有 HTTP URL，并发获取 bytes 转换为 base64
    if (not image_b64_list or any(not b for b in image_b64_list)) and image_urls:
        import httpx
        while len(image_b64_list) < len(image_urls):
            image_b64_list.append("")
        for idx, url in enumerate(image_urls):
            if idx < len(image_b64_list) and not image_b64_list[idx] and isinstance(url, str) and url.startswith("http"):
                try:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                        r = await c.get(url, headers=headers)
                        if r.status_code == 200:
                            image_b64_list[idx] = _bytes_to_base64(r.content)
                except Exception:  # nosec B110
                    pass

    cache_dir = Path("data/cache/blog_cards")
    cache_dir.mkdir(parents=True, exist_ok=True)

    group_key = post.get("group_key", "blog")
    safe_author = re.sub(r'[\\/:*?"<>|#%&]', '', post.get("author", "author"))[:20].strip()
    safe_title = re.sub(r'[\\/:*?"<>|#%&]', '', post.get("title", "title"))[:30].strip()
    safe_ts = re.sub(r'[\\/:*?"<>|\\s-]', '', post.get("date", ""))[:12]

    base_name = f"{group_key}_{safe_author}_{safe_title}_{safe_ts}"
    tmp_html = cache_dir / f"{base_name}.html"
    tmp_png = cache_dir / f"{base_name}.png"
    final_jpg = cache_dir / f"{base_name}.jpg"

    html_content = _generate_html(post, image_b64_list)
    with open(tmp_html, "w", encoding="utf-8") as f:
        f.write(html_content)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page(
                viewport={"width": 900, "height": 1000},
                device_scale_factor=1.5
            )
            await page.goto(f"file:///{tmp_html.resolve().as_posix()}")
            await page.wait_for_load_state("networkidle")
            card_el = await page.query_selector("#cardContainer")
            if card_el:
                await card_el.screenshot(path=str(tmp_png), type="png")
            else:
                await page.screenshot(path=str(tmp_png), full_page=True, type="png")
            await browser.close()

        if tmp_png.exists():
            img = Image.open(tmp_png)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(final_jpg, "JPEG", quality=85, optimize=True)
            if final_jpg.stat().st_size > int(2.8 * 1024 * 1024):
                img.save(final_jpg, "JPEG", quality=78, optimize=True)
            if final_jpg.stat().st_size > int(3.2 * 1024 * 1024):
                img.save(final_jpg, "JPEG", quality=70, optimize=True)

            try:
                tmp_png.unlink(missing_ok=True)
                tmp_html.unlink(missing_ok=True)
            except OSError:
                pass

            log_all(
                f"🎨 已成功渲染博客长图卡片: {post.get('author')} - {post.get('title')} "
                f"({final_jpg.stat().st_size / 1024 / 1024:.2f} MB)"
            )
            return final_jpg

    except Exception as e:
        err_msg = str(e)
        if "Executable doesn't exist" in err_msg or "playwright install" in err_msg:
            log_all(
                "⚠️ 博客长图渲染失败：未安装 Chromium 浏览器内核。"
                "请在终端执行 `playwright install --with-deps chromium` 后重试，当前已自动降级为标准图文推送。",
                is_error=True,
            )
        else:
            log_all(f"⚠️ 博客长图渲染异常，已自动降级为标准图文推送: {e}", is_error=True)
        try:
            tmp_png.unlink(missing_ok=True)
            tmp_html.unlink(missing_ok=True)
        except OSError:
            pass

    return None
