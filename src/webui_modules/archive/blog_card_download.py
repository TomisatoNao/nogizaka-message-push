"""按归档博客 ID 生成单张、可下载的长图；不接受客户端 HTML/图片路径。"""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import threading
import time
import uuid

from PIL import Image, UnidentifiedImageError

from src.blog_card_renderer import _generate_html, is_playwright_available

CARD_DIR = Path("data/cache/blog_cards/downloads")
IMAGE_ROOT = Path("data/blog_images")
TEMPLATE_VERSION = "download-v1"
MODES = frozenset({"ja-zh", "ja-only", "zh-only"})
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
MAX_CARD_PIXELS = 30_000_000
MAX_CARD_HEIGHT = 30_000
MAX_IMAGES = 60
MAX_CONTENT_CHARS = 2_000_000
JOB_TTL_SECONDS = 3600


class CardExportError(Exception):
    """可向用户展示的导出失败原因。"""


class _JapaneseBlocks(HTMLParser):
    """把历史正文 HTML 化成纯文本段落和图片占位，绝不执行原始 HTML。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.text: list[str] = []
        self.skip_depth = 0

    def _flush(self):
        value = "".join(self.text).strip()
        if value:
            self.blocks.append({"type": "text", "jp": value, "zh": ""})
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "svg", "object"}:
            self.skip_depth += 1
        if self.skip_depth:
            return
        if tag == "img":
            self._flush()
            self.blocks.append({"type": "img"})
        elif tag in {"p", "div", "section", "article", "li", "br", "hr"}:
            self._flush()

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "svg", "object"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if not self.skip_depth and tag in {"p", "div", "section", "article", "li"}:
            self._flush()

    def handle_data(self, data):
        if not self.skip_depth:
            self.text.append(data)

    def finish(self):
        self._flush()
        return self.blocks


def _json_list(value):
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _blocks(post: dict, mode: str) -> tuple[list[dict], str]:
    translated = _json_list(post.get("content_json"))
    if mode != "ja-only" and translated:
        blocks = []
        for item in translated:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "img":
                blocks.append({"type": "img"})
            else:
                blocks.append({
                    "type": "text",
                    "jp": str(item.get("jp") or ""),
                    "zh": str(item.get("zh") or ""),
                })
        return blocks, mode
    parser = _JapaneseBlocks()
    parser.feed(str(post.get("body_html") or ""))
    blocks = parser.finish()
    if not blocks and post.get("body_text"):
        blocks = [{"type": "text", "jp": str(post["body_text"]), "zh": ""}]
    return blocks, "ja-only"


def _local_image(path_value: str) -> Path:
    root = IMAGE_ROOT.resolve()
    candidate = Path(path_value)
    # 旧档案可能存绝对路径；也必须落在归档图片目录内。
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise CardExportError("博客图片归档不完整，请先补齐图片后重试")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise CardExportError("博客中有图片过大，无法安全生成卡片")
    return path


def _image_data(path: Path) -> str:
    try:
        with Image.open(path) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise CardExportError("博客中有图片像素过大，无法安全生成卡片")
            source.thumbnail((900, 1800))
            output = BytesIO()
            source.convert("RGB").save(output, "JPEG", quality=86)
            return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except (UnidentifiedImageError, OSError) as exc:
        raise CardExportError("博客图片文件损坏，无法生成完整卡片") from exc


def prepare_card(post: dict, mode: str) -> tuple[dict, list[Path], str]:
    if mode not in MODES:
        raise CardExportError("不支持的卡片语言模式")
    for field in ("content_json", "body_html", "body_text"):
        if len(str(post.get(field) or "")) > MAX_CONTENT_CHARS:
            raise CardExportError("博客正文过长，无法完整生成单张图片")
    paths_raw = _json_list(post.get("image_paths_json"))
    urls = _json_list(post.get("images_json"))
    count = max(len(paths_raw), len(urls))
    if count > MAX_IMAGES:
        raise CardExportError("博客图片过多，无法安全生成单张卡片")
    if count and len(paths_raw) < count:
        raise CardExportError("博客图片归档不完整，请先补齐图片后重试")
    paths = [_local_image(str(value)) for value in paths_raw]
    blocks, actual_mode = _blocks(post, mode)
    if not blocks:
        raise CardExportError("博客正文为空，无法生成卡片")
    if len(blocks) > 3000:
        raise CardExportError("博客段落过多，无法安全生成单张图片")
    if sum(block.get("type") == "img" for block in blocks) > len(paths):
        raise CardExportError("博客正文中的图片尚未完整归档")
    safe_post = {
        "group_key": post.get("group_key") or "",
        "author": post.get("author") or "",
        "title": post.get("title") or "",
        "date": post.get("date") or "",
        "translation_model": post.get("translation_model") or "",
        "author_avatar_b64": "",  # 导出只用服务端受控头像查询
        "content_json": json.dumps(blocks, ensure_ascii=False),
    }
    fingerprint = {
        "version": TEMPLATE_VERSION,
        "id": post.get("id"),
        "mode": actual_mode,
        "post": safe_post,
        "images": [(str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths],
    }
    key = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return safe_post, paths, f"{key}:{actual_mode}"


async def _render(safe_post: dict, paths: list[Path], mode: str, destination: Path) -> None:
    from playwright.async_api import async_playwright

    image_data = [_image_data(path) for path in paths]
    html = _generate_html(safe_post, image_data, mode=mode, export=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        try:
            page = await browser.new_page(viewport={"width": 900, "height": 1000}, device_scale_factor=1)
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html, wait_until="load", timeout=30_000)
            await page.locator("#cardContainer img").evaluate_all(
                "imgs => Promise.all(imgs.map(img => img.decode().catch(() => null)))"
            )
            dimensions = await page.locator("#cardContainer").evaluate(
                "el => ({width: Math.ceil(el.scrollWidth), height: Math.ceil(el.scrollHeight)})"
            )
            width, height = int(dimensions["width"]), int(dimensions["height"])
            if width < 1 or height < 1 or height > MAX_CARD_HEIGHT or width * height > MAX_CARD_PIXELS:
                raise CardExportError("内容过长，无法完整生成单张图片")
            chunk = await page.locator("#cardContainer").screenshot(
                type="png", animations="disabled", timeout=60_000,
            )
            with Image.open(BytesIO(chunk)) as screenshot:
                if screenshot.width != width or abs(screenshot.height - height) > 2:
                    raise CardExportError("卡片高度不完整，已取消下载，请重试")
                result = screenshot.convert("RGB")
            tmp = destination.with_suffix(".tmp.jpg")
            try:
                result.save(tmp, "JPEG", quality=86, optimize=True)
                tmp.replace(destination)
            finally:
                tmp.unlink(missing_ok=True)
        finally:
            await browser.close()


_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blog-card-download")
_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_key_jobs: dict[str, str] = {}
_recent_requests: dict[str, deque[float]] = defaultdict(deque)
_last_cache_cleanup = 0.0


def _clean_cache(now: float) -> None:
    global _last_cache_cleanup
    if now - _last_cache_cleanup < 3600:
        return
    _last_cache_cleanup = now
    if not CARD_DIR.is_dir():
        return
    active = {job["key"] for job in _jobs.values() if job["status"] in {"queued", "rendering"}}
    files = []
    for path in CARD_DIR.glob("*.jpg"):
        try:
            st = path.stat()
            if path.stem not in active:
                files.append((path, st.st_mtime, st.st_size))
        except OSError:
            continue
    total = sum(size for _, _, size in files)
    for path, modified, size in sorted(files, key=lambda row: row[1]):
        if time.time() - modified > 14 * 86400 or total > 512 * 1024 * 1024:
            try:
                path.unlink()
                total -= size
            except OSError:
                pass


def _clean_jobs(now: float) -> None:
    for job_id, job in list(_jobs.items()):
        if job["status"] in {"ready", "failed"} and now - job["updated"] > JOB_TTL_SECONDS:
            _jobs.pop(job_id, None)
            if _key_jobs.get(job["key"]) == job_id:
                _key_jobs.pop(job["key"], None)


def submit(post: dict, mode: str, client_id: str) -> tuple[dict, int]:
    safe_post, paths, fingerprint = prepare_card(post, mode)
    key, actual_mode = fingerprint.split(":", 1)
    destination = CARD_DIR / f"{key}.jpg"
    if destination.is_file() and destination.stat().st_size > 0:
        return {"ok": True, "status": "ready", "job_id": key}, 200
    if not is_playwright_available():
        raise CardExportError("服务器尚未安装卡片渲染组件")
    now = time.monotonic()
    with _lock:
        _clean_jobs(now)
        _clean_cache(now)
        existing_id = _key_jobs.get(key)
        if existing_id and _jobs[existing_id]["status"] in {"queued", "rendering"}:
            return {"ok": True, "status": _jobs[existing_id]["status"], "job_id": existing_id}, 202
        history = _recent_requests[client_id]
        while history and now - history[0] > 600:
            history.popleft()
        if len(history) >= 4:
            raise CardExportError("生成请求过于频繁，请十分钟后重试")
        pending = sum(j["status"] in {"queued", "rendering"} for j in _jobs.values())
        if pending >= 4:
            raise CardExportError("卡片生成任务较多，请稍后重试")
        history.append(now)
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {"status": "queued", "key": key, "updated": now, "error": ""}
        _key_jobs[key] = job_id
        _executor.submit(_run_job, job_id, safe_post, paths, actual_mode, destination)
    return {"ok": True, "status": "queued", "job_id": job_id}, 202


def _run_job(job_id: str, post: dict, paths: list[Path], mode: str, destination: Path) -> None:
    with _lock:
        _jobs[job_id]["status"] = "rendering"
    try:
        asyncio.run(asyncio.wait_for(_render(post, paths, mode, destination), timeout=90))
        status, error = "ready", ""
    except CardExportError as exc:
        status, error = "failed", str(exc)
    except (Exception, asyncio.TimeoutError):
        status, error = "failed", "卡片生成失败或超时，请稍后重试"
    with _lock:
        _jobs[job_id].update(status=status, error=error, updated=time.monotonic())


def status(job_id: str) -> dict | None:
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        return None
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        return {"ok": True, "status": job["status"], "error": job["error"], "job_id": job_id}


def ready_file(job_id: str) -> Path | None:
    info = status(job_id)
    if info and info["status"] == "ready":
        with _lock:
            key = _jobs[job_id]["key"]
        path = CARD_DIR / f"{key}.jpg"
        return path if path.is_file() else None
    # 缓存命中返回内容哈希，不在内存 job 列表中。
    if len(job_id) == 64 and all(c in "0123456789abcdef" for c in job_id):
        path = CARD_DIR / f"{job_id}.jpg"
        return path if path.is_file() else None
    return None
