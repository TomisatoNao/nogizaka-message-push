"""
social/formatter.py — 社交平台 QQ 推送格式化

**只服务新增平台**，既有 melink / showroom / youtube 的推送格式完全不变。

统一动态格式：
    平台：X
    作者：xxx
    发布时间：xxx
    内容（原文）：
    xxx
    内容（中文）：
    xxx
    媒体数量：2
    链接：https://...

直播通知格式见 build_live_start_message / build_live_end_message。
"""

from datetime import datetime, timezone, timedelta

from src.social.models import Post

_JST = timezone(timedelta(hours=9))
_CST = timezone(timedelta(hours=8))

# 平台展示名与 Emoji 图标
PLATFORM_ICONS = {
    "x": "𝕏 X",
    "instagram": "📷 Instagram",
    "tiktok": "🎵 TikTok",
    "tiktok_live": "🔴 TikTok Live",
}

PLATFORM_LABELS = {
    "x": "X",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "tiktok_live": "TikTok Live",
}

# 内容形态展示名（附在平台后，如「📷 Instagram (Story)」）
KIND_LABELS = {
    "post": "",
    "feed": "",
    "photo": "",
    "carousel": "",
    "reel": "Reel",
    "story": "Story",
    "retweet": "转推",
    "quote": "引用推文",
    "video": "",
    "live": "直播",
}

# ── 社交平台专用翻译提示词 ─────────────────────────────────
# 与 translator.py 既有的 _SYSTEM_PROMPT 相互独立：
# 既有提示词会为日文梗补充注释，而这里按需求要求「不润色、不总结、不解释」。
SOCIAL_TRANSLATE_PROMPT = """\
你是一名专业的社交媒体文本翻译，把输入文本翻译成简体中文。

严格遵循以下规则：
1. 只输出译文本身，不要任何前后缀、说明、标题或引号。
2. 完整保留所有 Emoji，位置与原文一致。
3. 完整保留所有 Hashtag（#标签），标签内容不翻译、不改写。
4. 完整保留所有 @用户名，不翻译、不改写、不删除。
5. 完整保留所有 URL 链接，原样输出。
6. **人名与昵称一律原样保留原文写法**，绝不翻译、绝不音译、绝不改写、
   绝不换成其它写法。范围包括：
   - 本名：鈴木瞳美 → 鈴木瞳美
   - 昵称／爱称，含 ちゃん・くん・さん・たん・っち 等后缀：
     おぎちゃん → おぎちゃん（**不可**译成「小荻」）
     さやちゃん → さやちゃん（**不可**译成「小纱」）
   - 团体名及其爱称：ノイミー → ノイミー（**不可**换写成「≠ME」），
     イコラブ → イコラブ（**不可**换写成「=LOVE」），≠ME → ≠ME
   - 账号名、品牌名、专辑/歌曲名
   即使某个昵称在中文圈另有惯用译法，也一律保持原文不动。
   ⚠️ 只有「人名/昵称」这几个词保持原文，**句子其余部分必须照常译成简体中文**，
   绝不允许把整句原样返回。示例：
     おぎちゃんがイヤリング貸してくれた🥹 → おぎちゃん借给我耳环了🥹
7. 不润色、不美化、不扩写、不总结、不解释、不添加注释。
8. 只做自然、忠实的翻译，保持原文语气与断行结构。
9. 如果原文本身已经是中文，原样返回原文。"""


def platform_label(post_or_platform, kind: str = "") -> str:
    """生成平台名称（含形态标识）。"""
    if isinstance(post_or_platform, Post):
        platform = post_or_platform.platform
        kind = kind or post_or_platform.extra.get("kind", "")
    else:
        platform = str(post_or_platform)
    label = PLATFORM_ICONS.get(platform, platform.upper())
    suffix = KIND_LABELS.get(kind, "")
    return f"{label} ({suffix})" if suffix else label


def fmt_ts(ts: float | int | None, tz=_JST, suffix: str = "JST") -> str:
    """时间戳 → 人类可读字符串（沿用项目 JST 惯例）。"""
    if not ts:
        return ""
    try:
        s = datetime.fromtimestamp(float(ts), tz=tz).strftime("%Y-%m-%d %H:%M:%S")
        return f"{s} {suffix}".strip()
    except (ValueError, OSError, OverflowError):
        return ""


def fmt_now_cst() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S CST")


def fmt_duration(seconds: float | int) -> str:
    """秒 → "2h 13m 05s" 形式。"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "未知"
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_size(num_bytes: float | int) -> str:
    """字节 → 人类可读体积。"""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def collect_alts(post: Post) -> list[tuple]:
    """收集图片的 alt 描述，返回 [(图片序号, alt 文本), ...]。

    X 的无障碍描述（alt）常常包含正文没有的信息，值得连同正文一起展示。
    """
    out = []
    idx = 0
    for m in post.media:
        if m.type != "image":
            continue
        idx += 1
        if m.alt_text and m.alt_text.strip():
            out.append((idx, m.alt_text.strip()))
    return out


def build_post_message(post: Post, translated: str | None = None,
                       alt_translations: dict | None = None) -> str:
    """按统一格式渲染一条社交动态。

    与 Message 推送和 Blog 推送风格保持一致：
    1. 顶部 Header：平台图标 + 平台名 + 账号/成员名 + 时间戳
    2. 正文原文直接输出（无冗余字段标签）
    3. 译文段以统一分割线分隔（与 Message 保持一致）
    4. 底部输出精简链接
    """
    kind = post.extra.get("kind", "")
    plat_str = platform_label(post, kind)

    author_name = post.author or ""
    member_name = post.extra.get("member_name", "")
    if member_name and member_name != author_name:
        clean_acc = author_name.lstrip("@")
        display_author = f"{member_name} (@{clean_acc})"
    else:
        display_author = author_name

    header_parts = [plat_str]
    if display_author:
        header_parts.append(display_author)
    header = " · ".join(header_parts)
    if post.timestamp:
        header += f" {post.timestamp}"

    sections = [header]

    # 正文原文
    orig_text = (post.text or "").strip()
    if orig_text:
        sections.append(orig_text)

        # 译文段（仅在译文存在且与原文不完全相同时展示）
        trans_text = (translated or "").strip()
        if trans_text and trans_text != orig_text:
            sections.append(f"─── 🌐 译文 ───\n\n{trans_text}")

    # 图片无障碍描述 (alt)
    alts = collect_alts(post)
    if alts:
        zh_map = alt_translations or {}
        alt_lines = []
        for i, text in alts:
            zh = zh_map.get(i)
            if zh and zh.strip() and zh.strip() != text:
                alt_lines.append(f"[图{i}] {zh.strip()} ({text})")
            else:
                alt_lines.append(f"[图{i}] {text}")
        if alt_lines:
            sections.append("🖼️ 图片描述：\n" + "\n".join(alt_lines))

    # 引用推文 / 转推
    quoted = post.extra.get("quoted_text", "")
    if quoted:
        qa = post.extra.get("quoted_author", "")
        q_header = f"🔁 引用推文 (@{qa.lstrip('@')}):" if qa else "🔁 引用推文:"
        sections.append(f"{q_header}\n{str(quoted).strip()}")

    # 链接
    url = post.extra.get("url", "")
    if url:
        sections.append(f"🔗 {url}")

    return "\n\n".join(sections)


def build_live_start_message(*, author: str, start_time: str, live_url: str,
                             platform: str = "TikTok Live") -> str:
    """开播提醒。"""
    return "\n\n".join([
        f"🔴 {platform} 开播提醒 · {author}\n开播时间：{start_time}",
        "⏺️ 状态：已开启自动录制",
        f"🔗 直播链接：{live_url}",
    ])


def build_live_end_message(*, author: str, start_time: str, end_time: str,
                           duration: str, size: str, save_path: str,
                           part_count: int = 0, note: str = "") -> str:
    """直播录制完成通知。"""
    lines = [
        f"🔴 TikTok 直播录制完成 · {author}",
        f"⏱️ 直播时间：{start_time} ~ {end_time}（{duration}）",
        f"📁 录像大小：{size}",
    ]
    if part_count:
        lines.append(f"📦 分段数量：{part_count}")
    if note:
        lines.append(f"📝 备注：{note}")
    lines.append(f"💾 保存路径：{save_path}")
    return "\n\n".join(lines)


def chunk_text(text: str, max_chars: int = 1500) -> list[str]:
    """把长文本按行边界切成多条，避免超过 QQ 单条消息长度上限。"""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        # 单行本身就超长 → 硬切
        while len(line) > max_chars:
            if buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            chunks.append(line[:max_chars])
            line = line[max_chars:]
        if size + len(line) + 1 > max_chars and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks
