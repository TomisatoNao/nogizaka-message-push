# ============================================================
# tgbot.py — Telegram Bot 推送（HTML 格式化，支持多 Bot 实例）
# ============================================================
import asyncio
import re

import config.config as cfg
from src.constants import ROLE_KEY, ROLE_TRANSLATION
from src.logger import log_all

# ---- 模块级状态 ----
_bots: list["TGBot"] = []

# Telegram 文本上限 4096、caption 上限 1024（均按转义后的 HTML 计），各留一点余量
_TELEGRAM_MAX_LENGTH = 4000
_TELEGRAM_CAPTION_MAX = 1000

# ──────────────────────────────────────────────
# TGBot 实例类
# ──────────────────────────────────────────────
class TGBot:
    """单个 Telegram Bot 实例，独立管理 token 和发送目标。"""
    
    def __init__(self, name: str, token: str, target_chat: str, 
                 remark: str = "",
                 member_filter: list[str] | None = None,
                 blog_filter: list[str] | None = None,
                 social_filter: list[str] | None = None,
                 push_message: bool = True,
                 push_blog: bool = False,
                 push_x: bool = True,
                 push_instagram: bool = True,
                 push_tiktok: bool = True,
                 push_live: bool = True,
                 push_alert: bool = False,
                 blog_card_mode: str = "card_and_images"):
        self.name = name
        self.remark = remark
        self.token = token
        self.target_chat = target_chat
        self.member_filter = member_filter or []
        self.blog_filter = blog_filter or []
        self.social_filter = social_filter or []
        self.push_message = push_message
        self.push_blog = push_blog
        self.push_x = push_x
        self.push_instagram = push_instagram
        self.push_tiktok = push_tiktok
        self.push_live = push_live
        self.push_alert = push_alert
        self.blog_card_mode = blog_card_mode
        
        self._bot = None
        self._request = None
        
    def initialize(self) -> None:
        if not self.token:
            return
            
        try:
            from telegram import Bot
            from telegram.request import HTTPXRequest
            
            self._request = HTTPXRequest(read_timeout=30, connect_timeout=10, write_timeout=15)
            self._bot = Bot(token=self.token, request=self._request)
        except ImportError:
            log_all("⚠️ python-telegram-bot 未安装，Telegram 推送不可用", is_error=True)
            self._bot = None
            
    async def get_me(self):
        if not self._bot:
            raise Exception("Bot not initialized")
        return await self._bot.get_me()

    async def _send_with_retry(self, label: str, action) -> bool:
        """执行一次 Telegram API 调用，带超时容忍与 flood control 重试。"""
        from telegram.error import RetryAfter, TimedOut

        for attempt in range(1, 4):
            try:
                await action()
                return True
            except TimedOut:
                log_all(f"⚠️ TG Bot [{self.name}] {label}超时（服务端可能已收到），视为成功", is_error=True)
                return True
            except RetryAfter as e:
                wait = _retry_wait(e)
                if attempt < 3:
                    log_all(f"⚠️ TG Bot [{self.name}] {label}触发 flood control，{wait:.1f}s 后重试 ({attempt}/3)", is_error=True)
                    await asyncio.sleep(wait)
                else:
                    log_all(f"❌ TG Bot [{self.name}] {label} flood control 重试耗尽 ({attempt}/3)", is_error=True)
                    return False
            except Exception as e:
                if attempt < 3:
                    wait = _retry_wait(e) or (2 ** attempt)
                    log_all(f"⚠️ TG Bot [{self.name}] {label}失败 ({attempt}/3): {e}，{wait:.1f}s 后重试", is_error=True)
                    await asyncio.sleep(wait)
                else:
                    log_all(f"❌ TG Bot [{self.name}] {label}彻底失败 (已重试 3 次): {e}", is_error=True)
                    return False

        return False

    async def _send_html(self, chat_id: str, html: str) -> bool:
        if not html or not html.strip() or not self._bot:
            return True

        from telegram.constants import ParseMode
        return await self._send_with_retry("发送", lambda: self._bot.send_message(
            chat_id=chat_id,
            text=html,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        ))

    async def _post_message(self, chat_id: str, text: str) -> bool:
        return await self._send_html(chat_id, _to_html(text, _TELEGRAM_MAX_LENGTH))

    async def _post_media(self, chat_id: str, media_type: str, file_url: str, caption: str = "") -> bool:
        from telegram.constants import ParseMode
        
        if not self._bot:
            return False

        safe_caption = _to_html(caption, _TELEGRAM_CAPTION_MAX) if caption else ""
        kwargs = {
            "chat_id": chat_id,
            "caption": safe_caption or None,
            "parse_mode": ParseMode.HTML if safe_caption else None,
        }

        if media_type == "image":
            async def action():
                try:
                    await self._bot.send_photo(photo=file_url, **kwargs)
                except Exception as ex:
                    if "invalid_dimensions" in str(ex).lower():
                        log_all(f"ℹ️ TG Bot [{self.name}] 图片超出 Telegram 比例/尺寸限制，自动转为文档发送", is_debug=True)
                        await self._bot.send_document(document=file_url, **kwargs)
                    else:
                        raise
        elif media_type == "video":
            action = lambda: self._bot.send_video(video=file_url, **kwargs)          # noqa: E731
        elif media_type == "record":
            action = lambda: self._bot.send_audio(audio=file_url, **kwargs)          # noqa: E731
        else:
            link = f'\n\n📎 <a href="{_escape_html(file_url)}">媒体文件</a>'
            return await self._send_html(chat_id, safe_caption + link)

        return await self._send_with_retry("媒体发送", action)

    async def send_photo_file(self, file_path_or_bytes: str | bytes, caption: str = "") -> bool:
        """发送本地图片文件或二进制数据。若遇到 Telegram 超长图片限制自动降级为 document 发送。"""
        if not self._bot or not self.target_chat or not file_path_or_bytes:
            return False
        from telegram.constants import ParseMode
        safe_caption = _to_html(caption, _TELEGRAM_CAPTION_MAX) if caption else ""
        kwargs = {
            "chat_id": self.target_chat,
            "caption": safe_caption or None,
            "parse_mode": ParseMode.HTML if safe_caption else None,
        }
        try:
            if isinstance(file_path_or_bytes, str):
                with open(file_path_or_bytes, "rb") as f:
                    data = f.read()
            else:
                data = file_path_or_bytes
            async def action():
                try:
                    await self._bot.send_photo(photo=data, **kwargs)
                except Exception as ex:
                    if "invalid_dimensions" in str(ex).lower():
                        log_all(f"ℹ️ TG Bot [{self.name}] 博客长图超出 Telegram 比例/尺寸限制，自动转为无损文档格式发送", is_debug=True)
                        await self._bot.send_document(document=data, filename="blog_card.jpg", **kwargs)
                    else:
                        raise
            return await self._send_with_retry("本地图片发送", action)
        except Exception as e:
            log_all(f"⚠️ TG Bot [{self.name}] 本地图片读取或发送失败: {e}", is_error=True)
            return False

    async def send_media_group_photos(self, photos: list[str], caption: str = "") -> bool:
        """发送 Telegram 图片专辑组（支持全量图片分批，第一张附带 Caption）。"""
        if not self._bot or not self.target_chat or not photos:
            return False
            
        from telegram import InputMediaPhoto
        from telegram.constants import ParseMode
        
        batches = [photos[i:i+10] for i in range(0, len(photos), 10)]
        all_ok = True
        
        for bi, batch in enumerate(batches):
            media = []
            for i, url in enumerate(batch):
                cap = caption if (bi == 0 and i == 0) else None
                pm = ParseMode.HTML if cap else None
                media.append(InputMediaPhoto(media=url, caption=cap, parse_mode=pm))
                
            def action(b=media):
                return self._bot.send_media_group(chat_id=self.target_chat, media=b)

            ok = await self._send_with_retry("Telegram 媒体组", action)
            if not ok:
                all_ok = False
                for i, url in enumerate(batch):
                    cap = caption if (bi == 0 and i == 0) else ""
                    await self._post_media(self.target_chat, "image", url, cap)
            await asyncio.sleep(1.0)
        return all_ok

    async def send_translation_tg(self, pairs: list[tuple[str, str] | str]) -> bool:
        """发送 Telegram 中日对照正文（日文斜体<i>，中文常规体，照片占位符[写真X]，自动切分<=4000字符）。"""
        if not self._bot or not self.target_chat or not pairs:
            return True
            
        import html as _html
        blocks = []
        for item in pairs:
            if isinstance(item, tuple):
                ja, zh = item
                ja_esc = _html.escape(ja)
                zh_esc = _html.escape(zh)
                block = f"<i>{ja_esc}</i>"
                if zh_esc:
                    block += f"\n{zh_esc}"
                blocks.append(block)
            elif isinstance(item, str):
                blocks.append(item)
            
        TELEGRAM_MAX = 3900
        parts = []
        buf = ""
        for block in blocks:
            if len(buf) + len(block) + 2 <= TELEGRAM_MAX:
                buf = (buf + "\n\n" + block) if buf else block
            else:
                if buf:
                    parts.append(buf)
                buf = block
        if buf:
            parts.append(buf)
            
        all_ok = True
        for part in parts:
            sent = await self._send_html(self.target_chat, part)
            if not sent:
                all_ok = False
            await asyncio.sleep(0.5)
        return all_ok

    async def send_member_message(self, message_chain: list[dict]) -> bool:
        if not self._bot or not self.target_chat:
            return True

        caption, media_list, translation = _chain_extract(message_chain)
        full_text = caption + translation
        ok = True

        if not media_list:
            return await self._post_message(self.target_chat, full_text) if full_text else True

        caption_fits = len(_escape_html(full_text)) <= _TELEGRAM_CAPTION_MAX
        if not caption_fits and full_text:
            if not await self._post_message(self.target_chat, full_text):
                ok = False
            await asyncio.sleep(0.5)

        for i, (media_type, file_url) in enumerate(media_list):
            cap = full_text if (caption_fits and i == 0) else ""
            if not await self._post_media(self.target_chat, media_type, file_url, cap):
                ok = False
            if i < len(media_list) - 1:
                await asyncio.sleep(0.5)

        return ok

    async def send_text(self, text: str, title: str = "") -> bool:
        if not self._bot or not self.target_chat:
            return False
        body = _to_html(text, _TELEGRAM_MAX_LENGTH - 64)
        prefix = f"<b>{_escape_html(title)}</b>\n" if title else ""
        return await self._send_html(self.target_chat, prefix + body)


# ──────────────────────────────────────────────
# 全局 API
# ──────────────────────────────────────────────

def initialize() -> None:
    """在事件循环内调用，初始化所有配置的 TG Bot 实例。"""
    global _bots
    _bots = []
    
    if getattr(cfg, "ENABLE_TG_BOT", False) and getattr(cfg, "TG_BOTS", []):
        for bot_cfg in cfg.TG_BOTS:
            if not bot_cfg.get("token"):
                continue
            
            bot = TGBot(
                name=bot_cfg.get("name", "tg_unnamed"),
                remark=bot_cfg.get("remark", ""),
                token=bot_cfg.get("token", ""),
                target_chat=bot_cfg.get("target_chat", ""),
                member_filter=bot_cfg.get("member_filter") or [],
                blog_filter=bot_cfg.get("blog_filter") or [],
                social_filter=bot_cfg.get("social_filter") or [],
                push_message=bool(bot_cfg.get("push_message", True)),
                push_blog=bool(bot_cfg.get("push_blog", False)),
                push_x=bool(bot_cfg.get("push_x", True)),
                push_instagram=bool(bot_cfg.get("push_instagram", True)),
                push_tiktok=bool(bot_cfg.get("push_tiktok", True)),
                push_live=bool(bot_cfg.get("push_live", True)),
                push_alert=bool(bot_cfg.get("push_alert", False)),
                blog_card_mode=bot_cfg.get("blog_card_mode", "card_and_images")
            )
            bot.initialize()
            if bot._bot:
                _bots.append(bot)
                display_name = f"{bot.name} ({bot.remark})" if bot.remark else bot.name
                log_all(f"📝 注册 TG Bot: {display_name}")

def get_configured_bots() -> list[TGBot]:
    return _bots

def _chain_extract(message_chain: list[dict]) -> tuple[str, list[tuple[str, str]], str]:
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
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _to_html(text: str, limit: int) -> str:
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
    retry_attr = getattr(exc, "retry_after", None)
    if retry_attr is not None:
        return float(retry_attr) + 0.5
    m = re.search(r"Retry in (\d+(?:\.\d+)?)", str(exc))
    if m:
        return float(m.group(1)) + 0.5
    return 0.0

async def health_check() -> bool:
    """验证所有 Bot Token 有效性。"""
    if getattr(cfg, "ENABLE_TG_BOT", False) and not _bots:
        log_all("🔴 TG Bot 未初始化（TG_BOTS 配置缺失或 python-telegram-bot 未安装）", is_error=True)
        return False
        
    any_ok = False
    for bot in _bots:
        display_name = f"{bot.name} ({bot.remark})" if bot.remark else bot.name
        try:
            me = await bot.get_me()
            log_all(f"🟢 TG Bot [{display_name}] 连通正常 (@{me.username})")
            any_ok = True
        except Exception as e:
            log_all(f"🔴 TG Bot [{display_name}] 无法连接: {e}", is_error=True)
            
    return any_ok
