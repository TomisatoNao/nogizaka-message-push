# ============================================================
# tagger.py — 图片自动打标签：结合 Gemini 多模态视觉与消息文本
#             从发型/服装/机位动作/活动场景/焦点实体等五维提取高价值标签
# ============================================================
import asyncio
import base64
import io
import json
import re
from pathlib import Path

import httpx

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

import config.config as cfg
from src.logger import log_all
from src.utils import RateLimiter

_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# ── 模块级状态 ──
_limiter: RateLimiter | None = None
_http_client: httpx.AsyncClient | None = None


def _get_limiter() -> RateLimiter:
    """获取或自愈创建限流器（确保即使未显式调用 initialize 也能正常工作）。"""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(lambda: cfg.GEMINI_TAG_MIN_INTERVAL)
    return _limiter


def initialize(client: httpx.AsyncClient | None = None) -> None:
    """在事件循环内调用，创建 RateLimiter 并注入共享 HTTP 客户端。"""
    global _limiter, _http_client
    _limiter = RateLimiter(lambda: cfg.GEMINI_TAG_MIN_INTERVAL)
    _http_client = client


async def _post_json(url: str, payload: dict) -> httpx.Response:
    """发送 Gemini 请求。优先复用共享连接池。"""
    headers = {"Content-Type": "application/json"}
    if _http_client is not None and not _http_client.is_closed:
        return await _http_client.post(url, json=payload, headers=headers, timeout=30)
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.post(url, json=payload, headers=headers)


def _optimize_image_for_gemini(img_data: bytes, suffix: str, max_side: int = 1024) -> tuple[str, str]:
    """等比轻量化压缩图片，减少 Base64 传输体积、加快推理速度并降低 Token 消耗。"""
    mime = _MIME_MAP.get(suffix.lower(), "image/jpeg")
    if not _HAS_PIL or len(img_data) < 150 * 1024:  # 小于 150KB 直接使用
        return base64.b64encode(img_data).decode("utf-8"), mime

    try:
        with Image.open(io.BytesIO(img_data)) as img:
            # 动态动图不破坏
            if getattr(img, "is_animated", False):
                return base64.b64encode(img_data).decode("utf-8"), mime

            img = img.convert("RGB")
            w, h = img.size
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85, optimize=True)
            compressed = buf.getvalue()
            return base64.b64encode(compressed).decode("utf-8"), "image/jpeg"
    except Exception as e:
        log_all(f"⚠️ 图片轻量化压缩失败，使用原图: {e}", is_debug=True)
        return base64.b64encode(img_data).decode("utf-8"), mime


def _build_tag_prompt(member_name: str, message_text: str = "") -> str:
    """构建结合偶像领域常识的多模态视觉特征提取提示词。"""
    lines = [
        "你是一名精通乃木坂46/日向坂46/樱坂46等坂道系列偶像文化的视觉特征与图像多模态分析器。",
        f"以下是偶像成员【{member_name or '坂道成员'}】在粉丝官方 App 发送给粉丝的消息照片。",
    ]
    if message_text and message_text.strip():
        lines.append(f"\n【附带消息正文】：\n{message_text.strip()[:300]}")

    lines.extend([
        "\n【任务目标】：",
        "请深度观察图片画面视觉细节（结合附带文字），提取最利于粉丝分类与精准检索的 3-5 个高价值中文标签（用空格分隔，严禁输出任何废话/前言/总结）。",
        "\n【打标准则与核心维度】：",
        "1. 画面主体判定（至关重要）：",
        "   - 若画面为【纯静物/美食/风景/周边物品/宠物/背景】，且无人物出镜，【绝对严禁】打'自拍'或'合照'！直接打具体事物标签（如：美食/甜点 Pino冰淇淋 生火腿意面 拓麻歌子周边 钥匙扣挂件 加湿器 祝花/花篮 宠物狗 房间/夜景等）。",
        "2. 人物视觉特征（若有人物出镜）：",
        "   - 发型发饰：双马尾 / 单马尾 / 丸子头 / 盘发 / 黑长直 / 卷发披发 / 短发 / 编发 / 大蝴蝶结 / 发箍 / 眼镜 / 贝雷帽 / 猫耳",
        "   - 服装穿搭：水手服·制服 / 打歌服·舞台装 / 演出礼服·白纱裙 / 棒球球衣 / 训练背心 / 私服·针织衫·牛仔外套 / 睡衣 / 浴衣·和服",
        "   - 构图机位：面部大特写 / 半身照 / 多图拼图 / 镜前自拍 / 他拍视角 / 横屏自拍",
        "   - 动作表情：比心 / 剪刀手·比耶 / 握拳·猫爪 / 吹泡泡 / 摆造型 / 闭眼大笑 / 甜美灿笑 / 嘟嘴 / 托腮 / 回眸",
        "   - 场景活动：外景写真 / 休息室·乐屋 / 演唱会Live / 节目现场 / 见面会 / 室内房间 / 排练室",
        "   - 同框合影：若为合照，准确提炼同框成员全名或爱称（如 井上和、佐藤优羽、五期生合影等）。",
        "\n【输出约束】：",
        "只输出 3~5 个准确精炼的标签词，词与词之间用一个空格分隔。直接输出纯文本标签，严禁输出任何标点符号、JSON或额外对话解释。"
    ])
    return "\n".join(lines)


def _clean_tags_output(text: str) -> str:
    """清洗与规范化模型输出的标签字符串。"""
    raw = (text or "").strip()
    if not raw:
        return ""

    # 尝试解析 JSON 格式
    if "{" in raw and "}" in raw:
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                if isinstance(data, dict):
                    tags_val = data.get("tags") or data.get("labels") or []
                    if isinstance(tags_val, list):
                        raw = " ".join(str(t) for t in tags_val)
                    elif isinstance(tags_val, str):
                        raw = tags_val
        except Exception:
            pass

    # 过滤掉 markdown 符号与常见无用修饰词
    raw = re.sub(r"[`*#\[\]\"'、，,]", " ", raw)
    tokens = [t.strip() for t in raw.split() if t.strip()]

    # 过滤掉前缀词如 "标签：" 等
    cleaned = []
    for t in tokens:
        if t.startswith("标签") or t.startswith("tags:") or t.startswith("类别:"):
            continue
        cleaned.append(t)

    if not cleaned:
        return ""
    # 限制 3-5 个标签
    return " ".join(cleaned[:5])


async def tag_image(member_dir: str, local_file: str, text: str = "") -> str:
    """读取本地图片，调 Gemini Vision 多模态打标签，返回空格分隔的标签字符串。
    失败时返回空字符串。

    Args:
        member_dir: 归档中的成员目录名（如 '冨里奈央'）
        local_file: 图片相对路径（如 '2026/07/images/xxx.jpg'）
        text: 附带的消息日文/中文字面正文（可选）

    Returns:
        标签字符串（如 '水手服 写真集外景 吹泡泡 黑长直'），失败返回 ''
    """
    if not cfg.ENABLE_IMAGE_TAGGING:
        return ""

    # 读取本地图片
    from src.archive import member_dir_name
    img_path = Path(cfg.ARCHIVE_DIR) / member_dir_name(member_dir) / local_file
    if not img_path.is_file():
        log_all(f"⚠️ 图片打标签：文件不存在 {img_path}", is_debug=True)
        return ""

    try:
        with open(img_path, "rb") as f:
            img_data = f.read()
    except OSError as e:
        log_all(f"⚠️ 图片打标签：读取失败 {img_path}: {e}", is_debug=True)
        return ""

    img_b64, mime = _optimize_image_for_gemini(img_data, img_path.suffix)
    prompt = _build_tag_prompt(member_dir, message_text=text)

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime, "data": img_b64}},
                {"text": prompt},
            ]
        }],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 128},
    }

    limiter = _get_limiter()
    async with limiter:
        for model in cfg.GEMINI_TAG_MODELS:
            url = f"{model['url']}?key={cfg.GEMINI_API_KEY}"
            for attempt in range(2):
                try:
                    resp = await _post_json(url, payload)

                    if resp.status_code == 200:
                        data = resp.json()
                        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
                        for part in parts:
                            out_text = part.get("text", "")
                            if out_text and not part.get("thought"):
                                tags = _clean_tags_output(out_text)
                                if tags:
                                    return tags
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
    if msg.get("type") not in ("picture", "image"):
        return
    if msg.get("_tags"):
        return  # 已有标签，跳过
    local_file = msg.get("_local_file", "")
    if not local_file:
        return
    task = asyncio.create_task(
        _do_tag(member_dir, msg, local_file)
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _do_tag(member_dir: str, msg: dict, local_file: str) -> None:
    """内部：打标签并合并写回归档（全异常捕获保障）。"""
    try:
        msg_text = msg.get("text") or msg.get("_translation") or ""
        tags = await tag_image(member_dir, local_file, text=msg_text)
        if not tags:
            return

        # 写回归档（复用 archive.py 的 _merge_write）
        from src.archive import _merge_write
        from datetime import datetime as _dt

        utc_str = msg.get("updated_at") or msg.get("published_at", "")
        try:
            dt = _dt.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            log_all(f"⚠️ 打标签：无法解析时间戳 {utc_str!r}", is_debug=True)
            return

        delta = {
            "id": msg.get("id"),
            "updated_at": msg.get("updated_at"),
            "_tags": tags,
        }
        await _merge_write(member_dir, dt, delta)
        log_all(f"🏷️ [{member_dir}] 图片打标签完成: {tags}")
    except Exception as e:
        log_all(f"⚠️ 后台打标签异常 [{member_dir}/{local_file}]: {e}", is_debug=True)


async def wait_pending(timeout: float = 60) -> None:
    """等待后台打标签任务收尾（优雅停机用）。"""
    if _bg_tasks:
        await asyncio.wait(list(_bg_tasks), timeout=timeout)

