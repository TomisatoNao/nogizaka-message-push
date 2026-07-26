# ============================================================
# tgbot.py — Telegram Bot 推送（HTML 格式化，支持媒体直传 URL）
# ============================================================
import asyncio
import re

import config.config as cfg
from src.constants import ROLE_KEY, ROLE_TRANSLATION
from src.logger import log_all

# ---- 模块级状态（由 initialize() 在事件循环内创建） ----
_bot = None       # telegram.Bot
_request = None   # telegram.request.HTTPXRequest

# Telegram 文本上限 4096、caption 上限 1024（均按转义后的 HTML 计），各留一点余量
_TELEGRAM_MAX_LENGTH = 4000
_TELEGRAM_CAPTION_MAX = 1000


def initialize() -> None:
    """在事件循环内调用，创建 telegram.Bot 实例。"""
    global _bot, _request

    if not cfg.ENABLE_TG_BOT:
        return

    token = cfg.TG_BOT_TOKEN
    if not token:
        log_all("⚠️ TG Bot Token 未配置，Telegram 推送不可用", is_error=True)
        return

    try:
        from telegram import Bot
        from telegram.request import HTTPXRequest
    except ImportError:
        log_all("⚠️ python-telegram-bot 未安装，Telegram 推送不可用", is_error=True)
        return

    _request = HTTPXRequest(read_timeout=30, connect_timeout=10, write_timeout=15)
    _bot = Bot(token=token, request=_request)
    log_all("📝 TG Bot 已初始化", is_debug=True)


# ──────────────────────────────────────────────
# message_chain → Telegram 转换
# ──────────────────────────────────────────────

def _chain_extract(message_chain: list[dict]) -> tuple[str, list[tuple[str, str]], str]:
    """
    从 message_chain 中提取三部分：
      - caption: 正文文本段（成员名 + 时间 + 原文）
      - media:   媒体段列表 [(type, file_url), ...]
      - translation: 翻译段（由 build_message_chain 打上 ROLE_TRANSLATION 标记）
    返回 (caption, media_list, translation_text)
    """
    text_parts: list[str] = []
    media: list[tuple[str, str]] = []
    translation = ""

    for item in message_chain:
        msg_type = item.get("type")
        data = item.get("data", {})

        if msg_type == "text":
            text = data.get("text", "")
            if item.get(ROLE_KEY) == ROLE_TRANSLATION:
                translation = text
            else:
                text_parts.append(text)
        elif msg_type in {"image", "video", "record"}:
            file_url = data.get("file", "")
            if file_url:
                media.append((msg_type, file_url))

    return "\n".join(text_parts), media, translation


def _escape_html(text: str) -> str:
    """转义 Telegram HTML 的三个特殊字符。

    这里**不做**任何标签还原 —— 成员消息和翻译结果都是不可信文本，
    需要真标签的调用方应先转义正文，再拼接自己的标签（见 send_alert）。
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _to_html(text: str, limit: int) -> str:
    """转义为 HTML，并保证结果长度不超过 limit。

    裁剪发生在**明文**层面（对转义后的长度做二分），避免把 &amp; 之类的
    实体切成两半导致 Telegram 报 400。
    """
    safe = _escape_html(text)
    if len(safe) <= limit:
        return safe

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_escape_html(text[:mid])) <= limit - 3:
            lo = mid
        else:
            hi = mid - 1
    return _escape_html(text[:lo]) + "..."


def _retry_wait(exc: Exception) -> float:
    """从异常中提取 Telegram flood control 等待秒数，无法提取时返回 0。
    来源：RetryAfter.retry_after 属性，或错误消息中的 'Retry in N' 文本。"""
    retry_attr = getattr(exc, "retry_after", None)
    if retry_attr is not None:
        return float(retry_attr) + 0.5
    m = re.search(r"Retry in (\d+(?:\.\d+)?)", str(exc))
    if m:
        return float(m.group(1)) + 0.5
    return 0.0


# ──────────────────────────────────────────────
# 发送
# ──────────────────────────────────────────────

async def _send_with_retry(label: str, action) -> bool:
    """执行一次 Telegram API 调用，带超时容忍与 flood control 重试。

    action 为无参调用，每次重试都重新构造协程。
    超时视为成功（服务端很可能已收到，重发会导致频道里出现重复消息）。
    """
    from telegram.error import RetryAfter, TimedOut

    for attempt in range(1, 4):
        try:
            await action()
            return True
        except TimedOut:
            log_all(f"⚠️ TG Bot {label}超时（服务端可能已收到），视为成功", is_error=True)
            return True
        except RetryAfter as e:
            wait = _retry_wait(e)
            if attempt < 3:
                log_all(f"⚠️ TG Bot {label}触发 flood control，{wait:.1f}s 后重试 ({attempt}/3)", is_error=True)
                await asyncio.sleep(wait)
            else:
                log_all(f"❌ TG Bot {label}flood control 重试耗尽 ({attempt}/3)", is_error=True)
                return False
        except Exception as e:
            if attempt < 3:
                wait = _retry_wait(e) or (2 ** attempt)
                log_all(f"⚠️ TG Bot {label}失败 ({attempt}/3): {e}，{wait:.1f}s 后重试", is_error=True)
                await asyncio.sleep(wait)
            else:
                log_all(f"❌ TG Bot {label}彻底失败 (已重试 3 次): {e}", is_error=True)
                return False

    return False


async def _send_html(chat_id: str, html: str) -> bool:
    """发送一条**已转义并拼装完毕**的 HTML 消息（长度须由调用方保证）。"""
    if not html or not html.strip():
        return True

    from telegram.constants import ParseMode

    return await _send_with_retry("发送", lambda: _bot.send_message(
        chat_id=chat_id,
        text=html,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    ))


async def _post_message(chat_id: str, text: str) -> bool:
    """发送纯文本消息（内部负责转义与长度裁剪）。"""
    return await _send_html(chat_id, _to_html(text, _TELEGRAM_MAX_LENGTH))


async def _post_media(chat_id: str, media_type: str, file_url: str, caption: str = "") -> bool:
    """通过 URL 直接发送单个媒体文件，可附带 caption。"""
    from telegram.constants import ParseMode

    safe_caption = _to_html(caption, _TELEGRAM_CAPTION_MAX) if caption else ""
    kwargs = {
        "chat_id": chat_id,
        "caption": safe_caption or None,
        "parse_mode": ParseMode.HTML if safe_caption else None,
    }

    if media_type == "image":
        action = lambda: _bot.send_photo(photo=file_url, **kwargs)          # noqa: E731
    elif media_type == "video":
        action = lambda: _bot.send_video(video=file_url, **kwargs)          # noqa: E731
    elif media_type == "record":
        action = lambda: _bot.send_audio(audio=file_url, **kwargs)          # noqa: E731
    else:
        # 未知媒体类型，退化为带链接的文本消息
        link = f'\n\n📎 <a href="{_escape_html(file_url)}">媒体文件</a>'
        return await _send_html(chat_id, safe_caption + link)

    return await _send_with_retry("媒体发送", action)


# ──────────────────────────────────────────────
# 公开 API
# ──────────────────────────────────────────────

def _resolve_chat_id(member: dict) -> str:
    """获取成员的 tg_chat_id。未配置时返回空字符串。"""
    return (member.get("tg_chat_id") or "").strip()


async def send_member_message(member: dict, message_chain: list[dict]) -> bool:
    """
    向该成员配置的 Telegram 频道发送消息。
    - 纯文本：一条 HTML 消息
    - 含媒体且正文不超 caption 上限：首个媒体带 caption（原文 + 翻译）
    - 含媒体且正文超上限：先发完整文本，再发不带 caption 的媒体（避免翻译被截断丢失）
    - 成员未配置 tg_chat_id 时静默跳过
    """
    if _bot is None:
        return False

    chat_id = _resolve_chat_id(member)
    if not chat_id:
        return True  # 未配置 TG 推送，不算失败

    caption, media_list, translation = _chain_extract(message_chain)

    # 原文与翻译合并为一条消息，中间是 TRANSLATION_SEPARATOR
    full_text = caption + translation

    ok = True

    if not media_list:
        return await _post_message(chat_id, full_text) if full_text else True

    # 正文（含翻译）塞不进 caption 时，单独发一条文本，媒体不带 caption
    caption_fits = len(_escape_html(full_text)) <= _TELEGRAM_CAPTION_MAX
    if not caption_fits and full_text:
        if not await _post_message(chat_id, full_text):
            ok = False
        await asyncio.sleep(0.5)

    for i, (media_type, file_url) in enumerate(media_list):
        cap = full_text if (caption_fits and i == 0) else ""
        if not await _post_media(chat_id, media_type, file_url, cap):
            ok = False
        if i < len(media_list) - 1:
            await asyncio.sleep(0.5)  # 媒体间短暂间隔

    return ok


async def send_text(chat_id: str, text: str, title: str = "") -> bool:
    """发送纯文本消息到指定 TG 频道（可选加粗标题行）。"""
    if _bot is None or not chat_id:
        return False
    body = _to_html(text, _TELEGRAM_MAX_LENGTH - 64)
    prefix = f"<b>{_escape_html(title)}</b>\n" if title else ""
    return await _send_html(chat_id, prefix + body)


async def send_alert(chat_id: str, text: str) -> bool:
    """发送系统警报消息到指定 TG 频道。"""
    return await send_text(chat_id, text, title="📢 系统警报")


async def health_check() -> bool:
    """通过 getMe() 验证 Bot Token 有效。"""
    if _bot is None:
        # 静默返回会让启动检查里 TG 那一行凭空消失，必须明确报告原因
        log_all(
            "🔴 TG Bot 未初始化（TG_BOT_TOKEN 缺失或 python-telegram-bot 未安装）",
            is_error=True,
        )
        return False

    try:
        me = await _bot.get_me()
        log_all(f"🟢 TG Bot 连通正常 (@{me.username})")
        return True
    except Exception as e:
        log_all(f"🔴 TG Bot 无法连接: {e}", is_error=True)
        return False
