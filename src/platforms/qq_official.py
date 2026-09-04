import asyncio

import httpx

# 统一通过 cfg.X 访问，热重载后标量值（超时、限速间隔等）才能生效
import config.config as cfg
from src.logger import log_all

# 导入所有媒体转码、压缩与载荷处理逻辑（向下兼容 re-export）
from src.platforms.qq_official_media import (
    MediaPayload,
    _AUDIO_EXTENSIONS,
    _IMAGE_EXTENSIONS,
    _MEDIA_DEFAULT_EXTENSIONS,
    _MEDIA_FILE_TYPES,
    _QQ_VOICE_EXTENSION,
    _VIDEO_EXTENSIONS,
    _VOICE_CACHE,
    _VOICE_CACHE_BYTES,
    _VOICE_CACHE_LIMIT,
    _VOICE_CACHE_LOCK,
    _cache_voice_result,
    _compress_image_if_needed,
    _compress_video_if_needed,
    _download_media,
    _filename_candidate,
    _looks_like_silk,
    _media_extension,
    _media_items_with_metadata,
    _resolve_media_type,
    _safe_media_filename,
    _transcode_audio_to_silk,
    _transcode_audio_to_silk_uncached,
    download_media_payloads,
    media_items,
)


from src.platforms.qq_official_client import QQOfficialClient


# ──────────────────────────────────────────────
# QQ 官方 Bot 实例类
# ──────────────────────────────────────────────
class QQOfficialBot(QQOfficialClient):
    """单个 QQ 官方 Bot 实例，管理推送目标过滤与消息链分发。"""

    def __init__(self, name: str, app_id: str, client_secret: str, target_openid: str,
                 group_openid: str = "", remark: str = "", member_filter: list[str] | None = None,
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
        super().__init__(app_id=app_id, client_secret=client_secret, name=name, target_openid=target_openid)
        self.remark = remark
        self.group_openid = group_openid
        self.member_filter: list[str] = member_filter or []
        self.blog_filter: list[str] = blog_filter or []
        self.social_filter: list[str] = social_filter or []
        self.push_message: bool = push_message
        self.push_blog: bool = push_blog
        self.push_x: bool = push_x
        self.push_instagram: bool = push_instagram
        self.push_tiktok: bool = push_tiktok
        self.push_live: bool = push_live
        self.push_alert: bool = push_alert
        self.blog_card_mode: str = blog_card_mode

    async def send_text(self, text: str, max_retries: int = 3) -> bool:
        """向配置的目标 openid 发送单聊纯文本消息。"""
        if not text.strip():
            return False

        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False

            url = f"{cfg.QQ_OFFICIAL_API_BASE}/v2/users/{self.target_openid}/messages"
            payload = {
                "content": text[:1900],
                "msg_type": 0,
            }
            return await self._post_json(url, payload, max_retries) is not None

    async def _send_chain(self, scope: str, target_openid: str, member: dict,
                          message_chain: list[dict],
                          media_payloads: list[MediaPayload | tuple[str, bytes | None]] | None = None) -> bool:
        """共享的链式发送核心：文字 + 媒体。scope='users'|'groups'。"""
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False

            ok = True
            text = chain_to_text(message_chain)
            if text:
                url = f"{self._target_base(scope, target_openid)}/messages"
                if await self._post_json(url, {"content": text[:1900], "msg_type": 0}) is None:
                    ok = False

            if media_payloads is None:
                media_payloads = await download_media_payloads(member, message_chain)
            for raw_payload in media_payloads:
                if isinstance(raw_payload, MediaPayload):
                    media_type, content, filename = (
                        raw_payload.media_type,
                        raw_payload.content,
                        raw_payload.filename,
                    )
                else:
                    # 保持旧版二元组调用方兼容；三元组调用方也可直接提供文件名。
                    media_type = raw_payload[0]
                    content = raw_payload[1]
                    filename = raw_payload[2] if len(raw_payload) > 2 else ""
                    mime_type = raw_payload[3] if len(raw_payload) > 3 else ""
                if isinstance(raw_payload, MediaPayload):
                    mime_type = raw_payload.mime_type
                if content is None:
                    ok = False
                    continue
                file_info = await self._upload_media(
                    media_type,
                    content,
                    scope=scope,
                    target_openid=target_openid,
                    filename=str(filename or ""),
                    mime_type=str(mime_type or ""),
                )
                if not file_info or not await self._send_uploaded_media(file_info, scope=scope, target_openid=target_openid):
                    ok = False

            return ok

    async def send_message_chain(self, member: dict, message_chain: list[dict],
                                 media_payloads: list[MediaPayload | tuple[str, bytes | None]] | None = None) -> bool:
        """向配置的目标 openid 发送单聊完整消息链。"""
        return await self._send_chain("users", self.target_openid, member, message_chain, media_payloads)

    async def send_message_chain_to_group(self, group_openid: str, member: dict,
                                          message_chain: list[dict],
                                          media_payloads: list[MediaPayload | tuple[str, bytes | None]] | None = None) -> bool:
        """向指定群聊发送完整消息链。"""
        return await self._send_chain("groups", group_openid, member, message_chain, media_payloads)

    async def send_group_text(self, group_openid: str, text: str, max_retries: int = 3) -> bool:
        """向指定群聊发送纯文本消息。"""
        if not text.strip():
            return False
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False
            url = f"{self._target_base('groups', group_openid)}/messages"
            return await self._post_json(url, {"content": text[:1900], "msg_type": 0}, max_retries) is not None

    async def send_private_text(self, target_openid: str, text: str, max_retries: int = 3) -> bool:
        """向指定用户发送纯文本消息。"""
        if not text.strip():
            return False
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False
            url = f"{self._target_base('users', target_openid)}/messages"
            return await self._post_json(url, {"content": text[:1900], "msg_type": 0}, max_retries) is not None

    # 兼容别名
    _send_c2c_text = send_private_text
    _send_group_text = send_group_text

    async def send_media_file(self, scope: str, target_openid: str, media_type: str, content: bytes,
                              filename: str = "", mime_type: str = "") -> bool:
        """向指定用户/群聊发送单个图片、视频或音频媒体。scope: 'users' | 'groups'。"""
        if not content or not target_openid:
            return False
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False
            file_info = await self._upload_media(
                media_type,
                content,
                scope=scope,
                target_openid=target_openid,
                filename=filename,
                mime_type=mime_type,
            )
            if not file_info:
                return False
            return await self._send_uploaded_media(file_info, scope=scope, target_openid=target_openid)

    async def send_translation_qq(self, scope: str, target_openid: str, pairs: list[tuple[str, str]]) -> bool:
        """发送 QQ 中日对照正文（日文斜体*，中文常规体，双语对之间零宽空格行，切分<=1800字符）。"""
        if not pairs or not target_openid:
            return True
            
        import re
        def _esc_md(t: str) -> str:
            t = t.replace('*', '＊').replace('_', '＿').replace('`', '｀')
            t = re.sub(r'^#', '＃', t, flags=re.MULTILINE)
            t = re.sub(r'^> ', '＞ ', t, flags=re.MULTILINE)
            t = re.sub(r'^- ', '－ ', t, flags=re.MULTILINE)
            return t
            
        def _format_lines(text: str, symbol: str) -> str:
            lines = text.split('\n')
            res = []
            for line in lines:
                l_str = line.strip()
                if not l_str:
                    continue
                if re.match(r'^\[写真\d+\]$', l_str):
                    res.append(l_str)
                else:
                    res.append(f"{symbol}{_esc_md(l_str)}{symbol}")
            return "\n".join(res)
            
        blocks = []
        for item in pairs:
            if isinstance(item, tuple):
                ja, zh = item
                ja_fmt = _format_lines(ja, "*")
                zh_fmt = _format_lines(zh, "")
                block = ja_fmt
                if zh_fmt:
                    block += f"\n{zh_fmt}"
                blocks.append(block)
            elif isinstance(item, str):
                blocks.append(item)
            
        MAX_LEN = 1800
        parts = []
        buf = ""
        for block in blocks:
            if len(buf) + len(block) + 4 <= MAX_LEN:
                buf = (buf + "\n\u200b\n" + block) if buf else block
            else:
                if buf:
                    parts.append(buf)
                buf = block
        if buf:
            parts.append(buf)
            
        all_ok = True
        async with self._send_limiter:
            if not await self.ensure_access_token():
                return False
            url = f"{self._target_base(scope, target_openid)}/messages"
            for part in parts:
                resp = await self._post_json(url, {"msg_type": 2, "markdown": {"content": part}})
                if resp is None:
                    plain_part = part.replace("**", "").replace("*", "")
                    resp = await self._post_json(url, {"msg_type": 0, "content": plain_part})
                    if resp is None:
                        all_ok = False
                await asyncio.sleep(0.5)
        return all_ok


# ──────────────────────────────────────────────
# 模块级 Bot 注册表
# ──────────────────────────────────────────────
_bots: list[QQOfficialBot] = []
_client: httpx.AsyncClient | None = None   # 媒体下载用（与各 Bot 共享同一实例）


def initialize(client: httpx.AsyncClient) -> None:
    """初始化所有配置的官方 Bot 实例。"""
    global _bots, _client
    _client = client
    _bots = []
    for i, bot_cfg in enumerate(cfg.QQ_OFFICIAL_BOTS):
        if not bot_cfg.get("app_id"):
            continue  # 跳过未配置的 Bot
        bot = QQOfficialBot(
            name=bot_cfg.get("name", f"official_{i}"),
            app_id=bot_cfg["app_id"],
            client_secret=bot_cfg.get("client_secret", ""),
            target_openid=bot_cfg.get("target_openid", ""),
            group_openid=bot_cfg.get("group_openid", ""),
            remark=bot_cfg.get("remark", ""),
            member_filter=bot_cfg.get("member_filter"),
            blog_filter=bot_cfg.get("blog_filter"),
            social_filter=bot_cfg.get("social_filter"),
            push_message=bool(bot_cfg.get("push_message", True)),
            push_blog=bool(bot_cfg.get("push_blog", False)),
            push_x=bool(bot_cfg.get("push_x", True)),
            push_instagram=bool(bot_cfg.get("push_instagram", True)),
            push_tiktok=bool(bot_cfg.get("push_tiktok", True)),
            push_live=bool(bot_cfg.get("push_live", True)),
            push_alert=bool(bot_cfg.get("push_alert", False)),
            blog_card_mode=bot_cfg.get("blog_card_mode", "card_and_images")
        )
        bot.initialize(client)
        _bots.append(bot)
        display_name = f"{bot.name} ({bot.remark})" if bot.remark else bot.name
        log_all(f"📝 注册官方 QQ Bot: {display_name}")


def reload() -> None:
    """热重载：更新配置但继承已有的 client 和 access_token。"""
    global _bots
    old_bots = {b.app_id: b for b in _bots}
    new_bots = []
    for i, bot_cfg in enumerate(cfg.QQ_OFFICIAL_BOTS):
        if not bot_cfg.get("app_id"):
            continue
        bot = QQOfficialBot(
            name=bot_cfg.get("name", f"official_{i}"),
            app_id=bot_cfg["app_id"],
            client_secret=bot_cfg.get("client_secret", ""),
            target_openid=bot_cfg.get("target_openid", ""),
            group_openid=bot_cfg.get("group_openid", ""),
            remark=bot_cfg.get("remark", ""),
            member_filter=bot_cfg.get("member_filter"),
            blog_filter=bot_cfg.get("blog_filter"),
            social_filter=bot_cfg.get("social_filter"),
            push_message=bool(bot_cfg.get("push_message", True)),
            push_blog=bool(bot_cfg.get("push_blog", False)),
            push_x=bool(bot_cfg.get("push_x", True)),
            push_instagram=bool(bot_cfg.get("push_instagram", True)),
            push_tiktok=bool(bot_cfg.get("push_tiktok", True)),
            push_live=bool(bot_cfg.get("push_live", True)),
            push_alert=bool(bot_cfg.get("push_alert", False)),
            blog_card_mode=bot_cfg.get("blog_card_mode", "card_and_images")
        )
        bot.initialize(_client)
        if bot.app_id in old_bots:
            old = old_bots[bot.app_id]
            bot._access_token = old._access_token
            bot._token_expire_at = old._token_expire_at
        new_bots.append(bot)
    _bots = new_bots
    log_all("📝 官方 QQ Bot 已热重载")


def get_bots() -> list[QQOfficialBot]:
    """返回所有已初始化的 Bot 实例。"""
    return _bots


def get_configured_bots() -> list[QQOfficialBot]:
    """返回所有配置完整、可用于发送的 Bot 实例。"""
    return [bot for bot in _bots if bot.is_configured()]


def has_bots() -> bool:
    """返回是否存在至少一个配置完整的 Bot。"""
    return any(bot.is_configured() for bot in _bots)


# ──────────────────────────────────────────────
# 工具函数（保持原有接口兼容）
# ──────────────────────────────────────────────
def chain_to_text(message_chain: list[dict]) -> str:
    """提取官方 Bot 文本消息内容。"""
    parts: list[str] = []
    for item in message_chain:
        msg_type = item.get("type")
        data = item.get("data", {})
        if msg_type == "text":
            text = data.get("text", "")
            if text:
                parts.append(text)
    return "".join(parts).strip()


async def health_check() -> bool:
    """启动时检查所有 Bot 凭证是否可用。"""
    if not _bots:
        return False
    all_ok = True
    for bot in _bots:
        if await bot.ensure_access_token():
            log_all(f"🟢 官方 QQ Bot [{bot.name}] 凭证正常")
        else:
            log_all(f"🔴 官方 QQ Bot [{bot.name}] 凭证无效", is_error=True)
            all_ok = False
    return all_ok


async def send_text(text: str, max_retries: int = 3) -> bool:
    """向所有 Bot 发送纯文本消息（用于报警）。"""
    if not _bots:
        return False
    all_ok = True
    for bot in _bots:
        if not await bot.send_text(text, max_retries):
            all_ok = False
    return all_ok


__all__ = [
    # Bot 类与生命周期 / 状态
    "QQOfficialClient",
    "QQOfficialBot",
    "initialize",
    "reload",
    "get_bots",
    "get_configured_bots",
    "has_bots",
    "health_check",
    "send_text",
    "chain_to_text",
    # 媒体层 re-export（保持 100% 向下兼容）
    "MediaPayload",
    "_MEDIA_FILE_TYPES",
    "_MEDIA_DEFAULT_EXTENSIONS",
    "_IMAGE_EXTENSIONS",
    "_VIDEO_EXTENSIONS",
    "_AUDIO_EXTENSIONS",
    "_QQ_VOICE_EXTENSION",
    "_filename_candidate",
    "_safe_media_filename",
    "_media_extension",
    "_looks_like_silk",
    "_resolve_media_type",
    "_VOICE_CACHE",
    "_VOICE_CACHE_BYTES",
    "_VOICE_CACHE_LIMIT",
    "_VOICE_CACHE_LOCK",
    "_cache_voice_result",
    "_transcode_audio_to_silk",
    "_transcode_audio_to_silk_uncached",
    "_compress_video_if_needed",
    "_compress_image_if_needed",
    "_download_media",
    "download_media_payloads",
    "media_items",
    "_media_items_with_metadata",
]
