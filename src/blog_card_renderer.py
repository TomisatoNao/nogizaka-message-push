"""
博客精美长图卡片渲染器 (Blog Image Card Renderer)
基于 Playwright Headless Chromium + 自适应杂志级排版模板。
支持乃木坂46、櫻坂46、日向坂46三大团体官方主题色自适应、中日双语精读对照、高清原图原位无损嵌入与无黑边视网膜级出图。
"""

import re
import base64
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
    if filepath.exists():
        try:
            with open(filepath, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
                suffix = filepath.suffix.lower()
                mime = "image/png" if suffix == ".png" else "image/jpeg"
                return f"data:{mime};base64,{data}"
        except Exception:
            pass
    return ""


def _generate_html(post: dict, image_b64_list: list[str]) -> str:
    """根据博客数据与三团配置生成自适应 HTML 模板字符串。"""
    group_key = post.get("group_key", "").lower()
    theme = GROUP_THEMES.get(group_key, DEFAULT_THEME)

    author = post.get("author", "成员")
    title = post.get("title", "无标题")
    date_str = post.get("date", "")
    trans_model = post.get("translation_model") or "AI 双语翻译引擎"

    raw_html = post.get("translation") or post.get("body_text") or ""

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

    hero_b64 = image_b64_list[0] if image_b64_list else ""
    if hero_b64:
        avatar_html = f'<img src="{hero_b64}" class="author-avatar" alt="{author}"/>'
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
    padding: 34px 40px 30px;
    position: relative;
    color: #ffffff;
  }}
  .card-header::after {{
    content: "";
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    height: 18px;
    background: #121422;
    border-radius: 20px 20px 0 0;
  }}

  .header-top {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
  }}
  .brand-badge {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(255, 255, 255, 0.25);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255, 255, 255, 0.4);
    padding: 6px 16px;
    border-radius: 30px;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.05em;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  }}
  .time-badge {{
    font-size: 14px;
    color: rgba(255, 255, 255, 0.95);
    font-weight: 600;
    letter-spacing: 0.03em;
  }}

  .author-row {{
    display: flex;
    align-items: center;
    gap: 18px;
    margin-bottom: 16px;
  }}
  .author-avatar {{
    width: 64px;
    height: 64px;
    border-radius: 50%;
    border: 2.5px solid #ffffff;
    object-fit: cover;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
    flex-shrink: 0;
  }}
  .author-avatar-fallback {{
    width: 64px;
    height: 64px;
    border-radius: 50%;
    border: 2.5px solid #ffffff;
    background: rgba(255, 255, 255, 0.2);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 28px;
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
    font-size: 24px;
    font-weight: 800;
    letter-spacing: 0.03em;
    text-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
  }}
  .author-tag {{
    font-size: 13px;
    color: rgba(255, 255, 255, 0.9);
    font-weight: 500;
  }}

  .blog-title {{
    font-size: 26px;
    font-weight: 800;
    line-height: 1.45;
    letter-spacing: 0.02em;
    color: #ffffff;
    text-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
    margin-top: 4px;
  }}

  .ai-badge {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-top: 14px;
    background: rgba(0, 0, 0, 0.25);
    border: 1px solid rgba(255, 255, 255, 0.25);
    padding: 5px 14px;
    border-radius: 8px;
    font-size: 13px;
    color: rgba(255, 255, 255, 0.95);
    font-weight: 500;
  }}

  /* Content Body */
  .card-body {{
    padding: 16px 40px 32px;
    font-size: 17px;
    line-height: 1.8;
  }}

  .card-body p {{
    margin-bottom: 24px;
  }}
  .card-body em {{
    display: block;
    color: #94a3b8;
    font-style: normal;
    font-size: 16px;
    line-height: 1.7;
    margin-bottom: 6px;
    opacity: 0.9;
  }}
  .card-body span {{
    display: block;
    color: #f8fafc;
    font-weight: 600;
    font-size: 18px;
    line-height: 1.75;
    margin-bottom: 18px;
  }}
  .card-body a {{
    color: {theme["accent"]};
    text-decoration: none;
    word-break: break-all;
    font-weight: 500;
  }}

  /* Image styling */
  .blog-img-wrap {{
    margin: 24px 0 26px;
    text-align: center;
  }}
  .blog-img {{
    width: 100%;
    border-radius: 18px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
    display: block;
  }}

  /* Footer Section */
  .card-footer {{
    background: #0b0c16;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    padding: 22px 40px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 13.5px;
    color: #64748b;
  }}
  .footer-brand {{
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 600;
    color: #94a3b8;
  }}
  .footer-right {{
    font-size: 13px;
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
    <div class="footer-right">写真共 {len(image_b64_list)} 张 · 官方原图无损呈现</div>
  </div>
</div>

</body>
</html>
"""
    return html_code


async def render_blog_card(post: dict) -> Optional[Path]:
    """渲染指定博客的长图卡片，返回生成的高清 JPG 图片绝对路径。

    若 Playwright 不可用或渲染异常，返回 None 实现优雅降级。
    """
    if not is_playwright_available():
        log_all("💡 Playwright 未安装或不可用，跳过长图卡片渲染并自动降级为标准推送", is_debug=True)
        return None

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    image_paths = post.get("image_paths") or []
    image_b64_list = []
    for p_str in image_paths:
        if isinstance(p_str, str):
            p = Path(p_str)
            if not p.is_absolute():
                p = Path("data/blog_images") / p
            image_b64_list.append(_file_to_base64(p))

    cache_dir = Path("data/cache/blog_cards")
    cache_dir.mkdir(parents=True, exist_ok=True)

    group_key = post.get("group_key", "blog")
    safe_author = re.sub(r'[\\/:*?"<>|]', '', post.get("author", "author"))[:20].strip()
    safe_title = re.sub(r'[\\/:*?"<>|]', '', post.get("title", "title"))[:30].strip()
    safe_ts = re.sub(r'[\\/:*?"<>|\s-]', '', post.get("date", ""))[:12]

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
            img.save(final_jpg, "JPEG", quality=92, optimize=True)

            try:
                tmp_png.unlink(missing_ok=True)
                tmp_html.unlink(missing_ok=True)
            except Exception:
                pass

            log_all(
                f"🎨 已成功渲染博客长图卡片: {post.get('author')} - {post.get('title')} "
                f"({final_jpg.stat().st_size / 1024 / 1024:.2f} MB)"
            )
            return final_jpg

    except Exception as e:
        log_all(f"⚠️ 博客长图渲染异常，自动优雅降级: {e}", is_error=True)
        try:
            tmp_png.unlink(missing_ok=True)
            tmp_html.unlink(missing_ok=True)
        except Exception:
            pass

    return None
