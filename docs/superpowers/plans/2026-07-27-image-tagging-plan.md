# 图片自动打标签 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对归档图片用 Gemini Flash Lite 自动生成 3-5 个中文标签（场景/物品/人物状态），在归档页支持通过标签搜索图片。

**Architecture:** 新增 `src/tagger.py` 模块。归档管道在图片下载完后异步调 Gemini Vision 分析本地图片 → 把标签写入 `messages.json` 的 `_tags` 字段。前端展示标签并支持点击搜索。

**Tech Stack:** Python 3.10+, Gemini API (vision), httpx

## Global Constraints

- 标签存储为 `_tags` 字段（空格分隔的中文字符串）
- 只在 `type` 为 `picture` / `image` 的消息上生成，跳过其他类型
- 已存在 `_tags` 的消息跳过（幂等）
- 标签覆盖「场景 + 物品 + 人物状态」三个维度，每张 3-5 个
- 主模型 `gemini-3.5-flash-lite`，fallback `gemini-3.1-flash-lite`
- 默认关闭（`image_tagging: false`），用户手动开启
- 回填脚本随时可重跑，只补没有标签的图片
- 每项变更独立 commit

---
## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `config/config.py` | 修改 | 新增 `IMAGE_TAGGING`, `GEMINI_TAG_MODELS`, `GEMINI_TAG_MIN_INTERVAL` |
| `config/config.schema.json` | 修改 | 定义 `image_tagging` schema |
| `config/config.json` | 修改 | 新增 `image_tagging` 配置段 |
| `src/tagger.py` | **创建** | 打标签核心模块 |
| `src/archive.py` | 修改 | 归档管道集成 + `search()` 同时匹配 `_tags` |
| `src/app.py` | 修改 | 在 `main()` 初始化 tagger 模块 |
| `src/webui.py` | 修改 | API 响应中加入 `tags` 字段 |
| `src/webui_static/archive.html` | 修改 | 显示标签 + 点击搜索 |
| `tests/test_tagger.py` | **创建** | 单元测试（mock） |
| `tools/tag_images.py` | **创建** | 回填脚本 |

---

### Task 1: Config — 新增 image_tagging 相关配置项

**Files:**
- Modify: `config/config.py` — 三处改动：默认值、_KEY_TO_VAR、_normalize_config
- Modify: `config/config.schema.json` — 新增 schema 定义
- Modify: `config/config.json` — 新增配置段（默认关闭）

**Interfaces:**
- Consumes: — (无上游依赖)
- Produces: `cfg.IMAGE_TAGGING`, `cfg.GEMINI_TAG_MODELS`, `cfg.GEMINI_TAG_MIN_INTERVAL`

- [ ] **Step 1: config.py — 添加默认值**

在 `_DEFAULTS` 末尾（现有 gemini 相关配置附近）添加三行：

```python
# 图片打标签
"enable_image_tagging":     False,
"gemini_tag_models": [
    {"name": "gemini-3.5-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"},
    {"name": "gemini-3.1-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"},
],
"gemini_tag_min_interval":  5.0,
```

- [ ] **Step 2: config.py — 添加 _KEY_TO_VAR 映射**

在 `_KEY_TO_VAR` 字典末尾添加：

```python
"enable_image_tagging":     "ENABLE_IMAGE_TAGGING",
"gemini_tag_models":        "GEMINI_TAG_MODELS",
"gemini_tag_min_interval":  "GEMINI_TAG_MIN_INTERVAL",
```

同时把 `"gemini_tag_models"` 加入 `_CONTAINER_KEYS` 使其支持热重载。

- [ ] **Step 3: config.py — 添加 _normalize_config 处理**

在 `_normalize_config()` 的 `# 旧的 translate 兼容` 附近，或者新建一块：

```python
# 图片打标签（image_tagging → enable_image_tagging）
if "image_tagging" in cfg:
    cfg["enable_image_tagging"] = cfg.pop("image_tagging")
```

- [ ] **Step 4: config.schema.json — 新增 schema**

在 schema 的 `properties` 末尾（`translate_timeout` 后面）添加：

```json
"image_tagging":              { "type": "boolean" },
"gemini_tag_models": {
  "type": "array",
  "items": {
    "type": "object",
    "required": ["name", "url"],
    "properties": {
      "name": { "type": "string" },
      "url":  { "type": "string", "format": "uri" }
    }
  }
},
"gemini_tag_min_interval":    { "type": "number", "minimum": 0 }
```

- [ ] **Step 5: config.json — 新增配置段**

在 `config.json` 的 `// ── 可选覆盖 ──` 区域中添加：

```json
"image_tagging": true,
"gemini_tag_models": [
  {"name": "gemini-3.5-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"},
  {"name": "gemini-3.1-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"}
],
"gemini_tag_min_interval": 5,
```

注意：这里设 `true` 开启功能，因为实验已验证可行。

- [ ] **Step 6: 提交**

```bash
git add config/config.py config/config.schema.json config/config.json
git commit -m "feat: add image_tagging config (model, interval, on/off)"
```

---

### Task 2: 核心 — 创建 src/tagger.py

**Files:**
- Create: `src/tagger.py`

**Interfaces:**
- Consumes: `cfg.ENABLE_IMAGE_TAGGING`, `cfg.GEMINI_TAG_MODELS`, `cfg.GEMINI_TAG_MIN_INTERVAL`, `cfg.GEMINI_API_KEY`
- Produces: `tagger.initialize(client)`, `tagger.tag_image(member_dir, local_file) → str`, `tagger.schedule_tag(member, msg)`

- [ ] **Step 1: 创建 `src/tagger.py` 完整实现**

```python
# ============================================================
# tagger.py — 图片自动打标签：用 Gemini Flash Lite 分析本地图片
#             生成 3-5 个中文标签（场景/物品/人物状态）
# ============================================================
import asyncio
import base64
import json
import os
from pathlib import Path

import httpx

import config.config as cfg
from src.logger import log_all
from src.utils import RateLimiter

_TAG_PROMPT = (
    "分析这张偶像照片，输出3-5个中文标签，用空格分隔。\n"
    "要求：\n"
    "- 标签覆盖：场景（舞台/室内/外景/街拍/演播室）、"
    "物品（花/食物/玩偶/麦克风/手机/饮料）、"
    "人物状态（笑容/自拍/挥手/比心/嘟嘴/看镜头/侧脸/戴耳机）\n"
    "- 不要包含成员名、团体名、品牌名\n"
    "- 严格5个以内\n"
    "- 如果是截图或海报，描述画面内容，不要提取图中文字作为标签\n"
    "只输出标签，不要多余文字。"
)

# ── 模块级状态 ──
_limiter: RateLimiter = None
_http_client: httpx.AsyncClient | None = None


def initialize(client: httpx.AsyncClient | None = None) -> None:
    """在事件循环内调用，创建 RateLimiter 并注入共享 HTTP 客户端。"""
    global _limiter, _http_client
    _limiter = RateLimiter(lambda: cfg.GEMINI_TAG_MIN_INTERVAL)
    _http_client = client


async def _post_json(url: str, payload: dict) -> httpx.Response:
    """发送 Gemini 请求。优先复用共享连接池。"""
    headers = {"Content-Type": "application/json"}
    if _http_client is not None:
        return await _http_client.post(url, json=payload, headers=headers, timeout=30)
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.post(url, json=payload, headers=headers)


async def tag_image(member_dir: str, local_file: str) -> str:
    """读取本地图片，调 Gemini Vision 打标签，返回空格分隔的标签字符串。
    失败时返回空字符串。
    
    Args:
        member_dir: 归档中的成员目录名（如 '冨里奈央'）
        local_file: 图片相对路径（如 '2026/07/images/xxx.jpg'）
    
    Returns:
        标签字符串（如 '舞台 笑容 挥手'），失败返回 ''
    """
    if not cfg.ENABLE_IMAGE_TAGGING:
        return ""
    
    # 读取本地图片
    img_path = Path(cfg.ARCHIVE_DIR) / member_dir / local_file
    if not img_path.is_file():
        log_all(f"⚠️ 图片打标签：文件不存在 {img_path}", is_debug=True)
        return ""
    
    try:
        with open(img_path, "rb") as f:
            img_data = f.read()
    except OSError as e:
        log_all(f"⚠️ 图片打标签：读取失败 {img_path}: {e}", is_debug=True)
        return ""
    
    img_b64 = base64.b64encode(img_data).decode("utf-8")
    suffix = img_path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    
    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime, "data": img_b64}},
                {"text": _TAG_PROMPT},
            ]
        }],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 64},
    }
    
    async with _limiter:
        for model in cfg.GEMINI_TAG_MODELS:
            url = f"{model['url']}?key={cfg.GEMINI_API_KEY}"
            for attempt in range(2):
                try:
                    resp = await _post_json(url, payload)
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
                        for part in parts:
                            text = part.get("text", "")
                            if text and not part.get("thought"):
                                tags = text.strip()
                                # 验证：确保不超过 5 个标签
                                count = len(tags.split())
                                if 1 <= count <= 5:
                                    return tags
                                elif count > 5:
                                    # 截取前 5 个
                                    return " ".join(tags.split()[:5])
                        # 响应结构异常，换下一个模型
                        log_all(f"⚠️ 打标签模型 {model['name']} 响应无可用文本，换下一个", is_debug=True)
                        break
                    elif resp.status_code == 429:
                        await asyncio.sleep((attempt + 1) * 3)
                        continue
                    else:
                        log_all(f"⚠️ 打标签模型 {model['name']} HTTP {resp.status_code}，换下一个", is_debug=True)
                        break
                except Exception as e:
                    log_all(f"⚠️ 打标签模型 {model['name']} 异常: {e}", is_debug=True)
                    break
    
    return ""


# 后台任务池
_bg_tasks: set = set()


def schedule_tag(member_dir: str, msg: dict) -> None:
    """后台异步打标签 + 写回归档（幂等，已存在 _tags 的跳过）。
    不阻塞调用方。
    """
    if not cfg.ENABLE_IMAGE_TAGGING:
        return
    if msg.get("_tags"):
        return  # 已有标签，跳过
    local_file = msg.get("_local_file", "")
    if not local_file:
        return
    member_dict = {"m_name": member_dir}
    task = asyncio.create_task(
        _do_tag(member_dict, msg, local_file)
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _do_tag(member: dict, msg: dict, local_file: str) -> None:
    """内部：打标签并合并写回归档。"""
    from src.archive import archive_message
    
    member_name = member.get("m_name", "")
    tags = await tag_image(member_name, local_file)
    if not tags:
        return
    
    # 写回归档（_merge_write 需要完整的 msg 字典）
    delta = {
        "id": msg.get("id"),
        "updated_at": msg.get("updated_at"),
        "_tags": tags,
    }
    # 复用 archive.py 的 _merge_write
    from src.archive import _merge_write
    from datetime import datetime as _dt
    
    utc_str = msg.get("updated_at") or msg.get("published_at", "")
    try:
        dt = _dt.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        log_all(f"⚠️ 打标签：无法解析时间戳 {utc_str!r}", is_debug=True)
        return
    
    await _merge_write(member_name, dt, delta)
    log_all(f"🏷️ [{member_name}] 图片打标签完成: {tags}")


async def wait_pending(timeout: float = 60) -> None:
    """等待后台打标签任务收尾（优雅停机用）。"""
    if _bg_tasks:
        await asyncio.wait(list(_bg_tasks), timeout=timeout)
```

- [ ] **Step 2: 提交**

```bash
git add src/tagger.py
git commit -m "feat: create tagger module for Gemini Vision image tagging"
```

---

### Task 3: 集成 — 归档管道调用 tagger + search 匹配 tags

**Files:**
- Modify: `src/archive.py`

**Interfaces:**
- Consumes: `tagger.schedule_tag()` (Task 2)
- Produces: 归档消息带 `_tags` 字段、`search()` 匹配 `_tags`

- [ ] **Step 1: 修改 `archive_message()` — 归档图片后触发打标签**

在 `archive_message()` 末尾，`if cfg.ARCHIVE_MEDIA and msg.get("file")...` 那段之后追加：

```python
# ── 图片打标签（后台）──
if translated:
    record["_translation"] = translated
```

修改为，在 `_download_media` 的后处理中加上 `schedule_tag` 调用：

找到 `archive_message()` 函数，在末尾附近加：

```python
# ── 图片打标签 ──
if msg.get("type") in ("picture", "image") and cfg.ENABLE_IMAGE_TAGGING:
    # 等 _local_file 回填后触发
    pass  # 实际触发由 fetcher 或 archive_message 完成后进行
```

更精确的做法：直接在 `archive_message()` 末尾添加：

```python
    # 图片类型且已下载媒体 → 后台触发打标签
    if msg.get("type") in ("picture", "image") and cfg.ENABLE_IMAGE_TAGGING:
        from src.tagger import schedule_tag
        # 等 merge_write 完成后触发（延迟 0.1s 让媒体先落盘）
        schedule_tag(m_name, {**record, "_local_file": record.get("_local_file", "")})
```

注意：需要在 `archive_message()` 函数里 import，避免模块级循环依赖。

实际的改动点：在 `archive_message()` 函数末尾、`schedule_archive` 之前，添加：

```python
    # ── 图片后台打标签 ──
    if msg.get("type") in ("picture", "image") and msg.get("file"):
        from src.tagger import schedule_tag as _schedule_tag
        _schedule_tag(m_name, dict(msg, _local_file=record.get("_local_file", "")))
```

- [ ] **Step 2: 修改 `search()` — 同时匹配 `_tags`**

在 `search()` 函数中，找到这一行：

```python
haystack = ((msg.get("text") or "") + "\n" + (msg.get("_translation") or "")).lower()
```

改为：

```python
haystack = (
    (msg.get("text") or "") + "\n" +
    (msg.get("_translation") or "") + "\n" +
    (msg.get("_tags") or "")
).lower()
```

- [ ] **Step 3: 提交**

```bash
git add src/archive.py
git commit -m "feat: integrate tagger into archive pipeline + search matches _tags"
```

---

### Task 4: 初始化 — 在 app.py 中初始化 tagger 模块

**Files:**
- Modify: `src/app.py`

**Interfaces:**
- Consumes: `tagger.initialize(client)` (Task 2)

- [ ] **Step 1: 添加 import 和初始化**

在 `src/app.py` 的模块顶部，在 `from src import archive` 附近添加：

```python
from src import tagger
```

在 `async def main()` 中，找到 `translator.initialize(http_client)` 和 `archive.initialize(http_client)` 那一块，在其后添加：

```python
    tagger.initialize(http_client)
```

并在 `wait_pending` 中（如果存在优雅停机逻辑）添加 `await tagger.wait_pending()`。

具体来说，找到 app.py 的 `wait_pending` 调用或最后 shutdown 逻辑，添加：

```python
    await tagger.wait_pending()
```

- [ ] **Step 2: 提交**

```bash
git add src/app.py
git commit -m "feat: initialize tagger module in app main loop"
```

---

### Task 5: WebUI — API 响应加上 tags 字段

**Files:**
- Modify: `src/webui.py`

- [ ] **Step 1: 在 messages 和 search 的 slim 转换中加 `tags`**

有两处需要修改：

**第 1 处：messages 接口（约 1015 行）**

找到 `slim = [{` 内的字典，在 `"translation"` 行后加：

```python
                "tags": m.get("_tags", ""),
```

**第 2 处：search 接口（约 1075 行）**

同样的位置，在 `"translation"` 行后加：

```python
                "tags": m.get("_tags", ""),
```

- [ ] **Step 2: 提交**

```bash
git add src/webui.py
git commit -m "feat: expose _tags in archive API responses"
```

---

### Task 6: 前端 — 归档页显示标签 + 点击搜索

**Files:**
- Modify: `src/webui_static/archive.html`

- [ ] **Step 1: 添加标签的 CSS 样式**

在 `archive.html` 的 `<style>` 块末尾（灯箱样式之前）添加：

```css
.tags { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 5px; }
.tag-chip { display: inline-block; padding: 2px 10px; border-radius: 999px;
            background: var(--accent-soft); color: var(--accent); font-size: 12px;
            cursor: pointer; border: 1px solid transparent; transition: all var(--tr); }
.tag-chip:hover { border-color: var(--accent); box-shadow: 0 1px 6px var(--accent-ring); }
```

- [ ] **Step 2: 在 renderBubble 中渲染标签**

在 `renderBubble()` 函数中，找到 `if (msg.translation)` 那一块，在其后添加：

```javascript
if (msg.tags) {
    const tagsDiv = document.createElement("div");
    tagsDiv.className = "tags";
    for (const t of msg.tags.split(" ")) {
        if (!t) continue;
        const chip = document.createElement("span");
        chip.className = "tag-chip";
        chip.textContent = "🔍 " + t;
        chip.title = "搜索「" + t + "」";
        chip.addEventListener("click", () => {
            $("searchBox").value = t;
            startSearch(t);
        });
        tagsDiv.appendChild(chip);
    }
    b.appendChild(tagsDiv);
}
```

注意：要在 `b.innerHTML = html;` 之后，`tl.appendChild(b);` 之前添加这段。

改为纯 DOM 操作（因为已经有 b 元素了）：

```javascript
// 标签行
if (msg.tags) {
    const tagsDiv = document.createElement("div");
    tagsDiv.className = "tags";
    for (const t of msg.tags.split(" ").filter(Boolean)) {
        const chip = document.createElement("span");
        chip.className = "tag-chip";
        chip.textContent = "🔍 " + t;
        chip.title = "搜索「" + t + "」";
        chip.addEventListener("click", () => { $("searchBox").value = t; startSearch(t); });
        tagsDiv.appendChild(chip);
    }
    b.appendChild(tagsDiv);
}
```

- [ ] **Step 3: 提交**

```bash
git add src/webui_static/archive.html
git commit -m "feat: display image tags in archive page with click-to-search"
```

---

### Task 7: 回填脚本 — tools/tag_images.py

**Files:**
- Create: `tools/tag_images.py`

**Interfaces:**
- Consumes: `tagger.tag_image()` (Task 2), `archive.load_month()`, `archive._merge_write()`
- Produces: 批量回填 `_tags` 到已有归档图片

- [ ] **Step 1: 创建完整的回填脚本**

```python
# ============================================================
# tools/tag_images.py — 批量图片打标签回填
# ============================================================
# 扫描归档目录，找出 type=picture/image 且 _tags 为空的图片，
# 逐张调 Gemini 打标签并写回归档。
#
# 幂等：已拥有 _tags 的图片跳过，随时可重跑。
#
# 用法：
#   python tools/tag_images.py --dry-run                    # 预览所有需处理的图片
#   python tools/tag_images.py --member 冨里奈央 --dry-run  # 预览指定成员
#   python tools/tag_images.py --member 冨里奈央 --year 2026 --month 07  # 指定月份
#   python tools/tag_images.py --member 冨里奈央            # 回填指定成员全部
#   python tools/tag_images.py                              # 回填所有成员全部
# ============================================================
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# 添加项目根到 sys.path
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import config.config as cfg
from src.archive import archive_root, list_members, member_dir_name, load_month
from src.tagger import tag_image, initialize as init_tagger


async def main():
    parser = argparse.ArgumentParser(description="批量图片打标签回填")
    parser.add_argument("--member", help="成员名（默认全部）")
    parser.add_argument("--year", type=int, help="年份（默认全部）")
    parser.add_argument("--month", type=int, help="月份（默认全部）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际调用 API")
    args = parser.parse_args()

    # 初始化 tagger
    init_tagger()

    members = args.member.split(",") if args.member else list_members()
    if not members:
        print("❌ 没有找到任何归档成员")
        return

    # 收集待处理图片
    pending = []  # [(member, year, month, msg)]
    for m_name in members:
        root = archive_root() / m_name
        if not root.is_dir():
            print(f"⚠ 成员归档不存在: {m_name}")
            continue

        year_dirs = sorted(
            (d for d in root.iterdir() if d.is_dir() and d.name.isdigit()),
            reverse=True,
        )
        for yd in year_dirs:
            year = int(yd.name)
            if args.year and year != args.year:
                continue

            month_dirs = sorted(
                (d for d in yd.iterdir() if d.is_dir() and d.name.isdigit()),
                reverse=True,
            )
            for md in month_dirs:
                month = int(md.name)
                if args.month and month != args.month:
                    continue

                msgs = load_month(m_name, year, month)
                for msg in msgs:
                    if msg.get("type") not in ("picture", "image"):
                        continue
                    if msg.get("_tags"):
                        continue  # 已有标签，跳过
                    if not msg.get("_local_file"):
                        continue  # 没有本地文件，跳过
                    pending.append((m_name, year, month, msg))

    if not pending:
        print("✅ 所有图片都已打标签，无需处理")
        return

    print(f"📊 待处理图片: {len(pending)} 张")
    if args.dry_run:
        print()
        for m_name, year, month, msg in pending:
            print(f"   [{m_name}] {year}/{month:02d}  {msg['_local_file']}  {msg.get('text', '')[:40]}")
        print()
        print(f"共 {len(pending)} 张（--dry-run 模式，未实际调用 API）")
        return

    # 逐个打标签
    ok = 0
    fail = 0
    skip = 0
    t0 = time.time()

    for i, (m_name, year, month, msg) in enumerate(pending, 1):
        local_file = msg.get("_local_file", "")
        print(f"[{i}/{len(pending)}] [{m_name}] {local_file} ... ", end="", flush=True)

        tags = await tag_image(m_name, local_file)
        if not tags:
            print("❌ 打标签失败")
            fail += 1
            continue

        print(f"✅ {tags}")

        # 写回归档
        from src.archive import _merge_write
        from datetime import datetime

        utc_str = msg.get("updated_at") or msg.get("published_at", "")
        try:
            dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            print(f"   ⚠ 时间戳解析失败: {utc_str}")
            skip += 1
            continue

        delta = {"id": msg["id"], "updated_at": utc_str, "_tags": tags}
        await _merge_write(m_name, dt, delta)
        ok += 1

        # 进度：每 10 张输出一次
        if ok % 10 == 0:
            elapsed = time.time() - t0
            print(f"\n  📊 进度: {ok}/{len(pending)}，耗时 {elapsed:.0f}s")

    elapsed = time.time() - t0
    print(f"\n{'='*40}")
    print(f"✅ 完成: 成功 {ok} 张")
    if fail:
        print(f"❌ 失败: {fail} 张")
    if skip:
        print(f"⏭️  跳过: {skip} 张（时间戳问题）")
    print(f"⏱️  总耗时: {elapsed:.0f}s（平均 {elapsed/max(ok,1):.1f}s/张）")
    print(f"{'='*40}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 提交**

```bash
git add tools/tag_images.py
git commit -m "feat: add backfill script tools/tag_images.py for idempotent batch tagging"
```

---

### Task 8: 测试 — 单元测试

**Files:**
- Create: `tests/test_tagger.py`

- [ ] **Step 1: 创建测试文件**

```python
"""测试 tagger 模块"""
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

import config.config as cfg


@pytest.fixture(autouse=True)
def setup_cfg():
    """确保测试时有合理的默认值"""
    cfg.ENABLE_IMAGE_TAGGING = True
    cfg.GEMINI_TAG_MODELS = [
        {"name": "test-model", "url": "https://example.com/model:generateContent"},
    ]
    cfg.GEMINI_TAG_MIN_INTERVAL = 0.01
    cfg.GEMINI_API_KEY = "test-key"
    cfg.ARCHIVE_DIR = "/tmp/test_archive"
    yield


@pytest.mark.asyncio
async def test_tag_image_file_not_found():
    """文件不存在时返回空字符串"""
    from src.tagger import tag_image
    result = await tag_image("冨里奈央", "2026/07/images/nonexistent.jpg")
    assert result == ""


@pytest.mark.asyncio
async def test_tag_image_disabled():
    """功能关闭时返回空"""
    cfg.ENABLE_IMAGE_TAGGING = False
    from src.tagger import tag_image
    result = await tag_image("冨里奈央", "2026/07/images/whatever.jpg")
    assert result == ""


@pytest.mark.asyncio
async def test_schedule_tag_skips_already_tagged():
    """已有 _tags 的消息跳过"""
    from src.tagger import schedule_tag
    # 不应抛异常
    schedule_tag("冨里奈央", {"_tags": "舞台 笑容"})
    schedule_tag("冨里奈央", {})
    schedule_tag("冨里奈央", {"_local_file": "test.jpg"})  # no _tags, but should be ok


@pytest.mark.asyncio
async def test_schedule_tag_disabled():
    """功能关闭时 schedule_tag 不创建任务"""
    cfg.ENABLE_IMAGE_TAGGING = False
    from src.tagger import schedule_tag
    schedule_tag("冨里奈央", {"_local_file": "test.jpg", "type": "picture"})
    # 不应抛异常


@pytest.mark.asyncio
async def test_search_matches_tags(monkeypatch):
    """search() 应匹配 _tags 字段"""
    from src.archive import search
    # Mock list_months + load_month
    def mock_list_months(member):
        return [{"year": 2026, "month": 7, "count": 1}]
    
    def mock_load_month(member, year, month):
        return [{
            "id": 1,
            "type": "picture",
            "text": "今日はいい天気",
            "_translation": "今天天气真好",
            "_tags": "笑容 自拍 海边",
        }]
    
    monkeypatch.setattr("src.archive.list_months", mock_list_months)
    monkeypatch.setattr("src.archive.load_month", mock_load_month)
    
    # 搜标签词
    hits = search("冨里奈央", "笑容")
    assert len(hits) == 1
    
    hits = search("冨里奈央", "海边")
    assert len(hits) == 1
    
    # 搜无匹配标签
    hits = search("冨里奈央", "舞台")
    assert len(hits) == 0
```

- [ ] **Step 2: 提交**

```bash
git add tests/test_tagger.py
git commit -m "test: add tagger module unit tests"
```

---

### Task 9: 回填执行 — 对近期约 100 张图片补标签

**无代码改动，纯运行脚本。**

- [ ] **Step 1: 先 dry-run 预览近几个月待处理的图片数**

```bash
python tools/tag_images.py --dry-run
```

确认数量合理（~100 张），且都是图片类型。

- [ ] **Step 2: 正式开始回填**

```bash
python tools/tag_images.py --member 冨里奈央
```

脚本逐个打标签，每张约 3-5 秒，100 张约 5-8 分钟。遵守 5 秒间隔，RPM ~12，远低于 Gemini 免费层上限。

- [ ] **Step 3: 提交回填结果**

```bash
git add data/archive/
git commit -m "chore: backfill image tags for archive pictures"
```

---

### Task 10: 验证 — 手工测试

- [ ] **Step 1: 启动项目测试实时归档打标签**

```bash
python main.py
```

确认新图片归档后日志中出现 `🏷️ [冨里奈央] 图片打标签完成: ...`。

- [ ] **Step 2: 打开归档页测试**

访问 `http://127.0.0.1:8787/archive`，确认：
- 图片气泡下方显示标签按钮（如 `🔍 笑容`）
- 点击标签 → 搜索框填入该词 → 自动搜索 → 返回匹配的图片
- 直接在搜索框输入标签词 → 能搜到图片

- [ ] **Step 3: 确认提交全部完成**

```bash
git log --oneline -10
git status
```
