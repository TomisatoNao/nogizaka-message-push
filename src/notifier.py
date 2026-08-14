# ============================================================
# notifier.py — QQ 多通道推送调度 (已解耦)
# ============================================================
import config.config as cfg
from src.logger import error_logger, log_all
from src.platforms.napcat import send_qq_message
from src.platforms import qq_official
from src.platforms.qq_official import get_configured_bots, has_bots
from src.platforms import tgbot
from src import health
from src.health import ErrorTier


def enabled_channels() -> list[str]:
    channels: list[str] = []
    if getattr(cfg, "ENABLE_NAPCAT_QQ", False):
        channels.append("napcat")
    if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False) and has_bots():
        channels.append("official")
    if getattr(cfg, "ENABLE_TG_BOT", False) and getattr(cfg, "TG_BOTS", []):
        channels.append("tg")
    return channels


async def send_member_message(member: dict, message_chain: list[dict]) -> bool:
    """
    向所有启用的通道推送成员消息。
    NapCat 保持原有可靠性语义：失败会阻断时间戳记录。
    官方 Bot 和 TG Bot 作为旁路，失败只记日志。
    """
    channels = enabled_channels()
    if not channels:
        log_all("⏸️ 推送通道均未启用，本条消息仅记录状态", is_error=True)
        return True

    m_name = member.get("m_name", "")
    napcat_ok = True

    # 1. NapCat 路由
    if cfg.ENABLE_NAPCAT_QQ:
        routes = getattr(cfg, "NAPCAT_ROUTES", [])
        matched_any = False
        for route in routes:
            if not route.get("push_message", True):
                continue
            filters = route.get("member_filter") or []
            if not filters or m_name in filters:
                gid = route.get("group_id")
                if gid:
                    matched_any = True
                    ok = await send_qq_message(gid, message_chain)
                    health.get_tracker().record_channel("napcat", ok, f"群 {gid} 发送失败")
                    if not ok:
                        napcat_ok = False
                        log_all(f"⚠️ [通道: NapCat | 目标群: {gid} | 成员: {m_name}] 消息推送失败", is_error=True)
                    else:
                        log_all(f"✅ [通道: NapCat | 目标群: {gid} | 成员: {m_name}] 消息推送成功", is_debug=True)
        if not matched_any:
            napcat_ok = True

    # 2. QQ 官方 Bot 路由
    if cfg.ENABLE_QQ_OFFICIAL_BOT:
        bots = get_configured_bots()
        media_payloads = None
        if bots:
            media_payloads = await qq_official.download_media_payloads(member, message_chain)

        # 单聊目标（target_openid）—— 受 member_filter 过滤
        for bot in bots:
            if not bot.target_openid or not bot.push_message:
                continue
            if bot.member_filter and m_name not in bot.member_filter:
                continue
            ok = await bot.send_message_chain(member, message_chain, media_payloads=media_payloads)
            health.get_tracker().record_channel(f"official:{bot.name}", ok)
            if not ok:
                log_all(f"⚠️ [通道: 官方Bot:{bot.name} | 目标: {bot.target_openid[:10]}... | 成员: {m_name}] 单聊推送失败", is_error=True)
            else:
                log_all(f"✅ [通道: 官方Bot:{bot.name} | 目标: {bot.target_openid[:10]}... | 成员: {m_name}] 单聊推送成功", is_debug=True)

        # 群聊目标（group_openid）—— 受 member_filter 过滤
        for bot in bots:
            if not bot.group_openid or not bot.push_message:
                continue
            if bot.member_filter and m_name not in bot.member_filter:
                continue
            ok = await bot.send_message_chain_to_group(
                bot.group_openid, member, message_chain, media_payloads=media_payloads)
            health.get_tracker().record_channel(f"official:{bot.name}:group", ok)
            if not ok:
                log_all(f"⚠️ [通道: 官方Bot:{bot.name} | 目标群: {bot.group_openid[:10]}... | 成员: {m_name}] 群推送失败", is_error=True)
            else:
                log_all(f"✅ [通道: 官方Bot:{bot.name} | 目标群: {bot.group_openid[:10]}... | 成员: {m_name}] 群推送成功", is_debug=True)

    # 3. TG Bot 路由
    if cfg.ENABLE_TG_BOT:
        tg_bots = tgbot.get_configured_bots()
        for bot in tg_bots:
            if not bot.target_chat or not bot.push_message:
                continue
            if bot.member_filter and m_name not in bot.member_filter:
                continue
            tg_ok = await bot.send_member_message(message_chain)
            health.get_tracker().record_channel(f"tg:{bot.name}", tg_ok)
            if not tg_ok:
                log_all(f"⚠️ [通道: TG:{bot.name} | TargetChat: {bot.target_chat} | 成员: {m_name}] 推送失败", is_error=True)
            else:
                log_all(f"✅ [通道: TG:{bot.name} | TargetChat: {bot.target_chat} | 成员: {m_name}] 推送成功", is_debug=True)

    if cfg.ENABLE_NAPCAT_QQ:
        return napcat_ok
    return True


async def send_report_message(text: str) -> bool:
    """向所有已启用通道发送运行报告（每日摘要等，非警报语义、无前缀）。
    目标取告警通道。"""
    any_ok = False

    if getattr(cfg, "ENABLE_NAPCAT_QQ", False):
        routes = getattr(cfg, "NAPCAT_ROUTES", [])
        for route in routes:
            if not route.get("push_alert"):
                continue
            gid = route.get("group_id")
            if gid:
                ok = await send_qq_message(gid, [{"type": "text", "data": {"text": text}}])
                any_ok = any_ok or ok

    if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False):
        for bot in get_configured_bots():
            if not bot.target_openid or not bot.push_alert:
                continue
            if await bot.send_text(text):
                any_ok = True

    if getattr(cfg, "ENABLE_TG_BOT", False):
        for bot in tgbot.get_configured_bots():
            if not bot.target_chat or not bot.push_alert:
                continue
            if await bot.send_text(text):
                any_ok = True

    return any_ok


async def send_alert_message(target_group: int, text: str) -> bool:
    """向所有配置为推送告警的通道发送系统警报。"""
    channels = enabled_channels()
    if not channels:
        log_all(f"⏸️ 没有可用的推送通道，警报未发送: {text}", is_error=True)
        return False

    any_ok = False

    # NapCat 群告警（发给开启了 push_alert 的路由）
    if getattr(cfg, "ENABLE_NAPCAT_QQ", False):
        routes = getattr(cfg, "NAPCAT_ROUTES", [])
        for route in routes:
            if not route.get("push_alert"):
                continue
            gid = route.get("group_id")
            if gid:
                alert_chain = [{"type": "text", "data": {"text": f"📢 系统警报\n{text}"}}]
                ok = await send_qq_message(gid, alert_chain)
                if ok:
                    any_ok = True
                else:
                    health.get_tracker().record_error(
                        f"NapCat 告警发送失败 (群 {gid})", ErrorTier.TRANSIENT
                    )
                    if error_logger:
                        error_logger.error(f"NapCat 告警发送失败: {text}")

    # QQ 官方 Bot 告警 — 发给开启了 push_alert 的 Bot
    if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False):
        bots = get_configured_bots()
        for bot in bots:
            if not bot.target_openid or not bot.push_alert:
                continue
            ok = await bot.send_text(f"📢 系统警报\n{text}")
            if ok:
                any_ok = True
            else:
                health.get_tracker().record_error(
                    f"官方Bot [{bot.name}] 告警发送失败", ErrorTier.TRANSIENT
                )

    # TG Bot 告警 — 发给开启了 push_alert 的 Bot
    if getattr(cfg, "ENABLE_TG_BOT", False):
        tg_bots = tgbot.get_configured_bots()
        for bot in tg_bots:
            if not bot.push_alert or not bot.target_chat:
                continue
            ok = await bot.send_text(f"📢 系统警报\n{text}")
            if ok:
                any_ok = True
            else:
                health.get_tracker().record_error(
                    f"TG Bot [{bot.name}] 告警发送失败", ErrorTier.TRANSIENT
                )

    return any_ok


# ── 博客推送 ──

EMOJI_MAP = {"hinatazaka": "☀️", "nogizaka": "💜", "sakurazaka": "🌸"}


def _compress_photo_placeholders(items: list[tuple[str, str] | str]) -> list[tuple[str, str] | str]:
    import re
    res = []
    i = 0
    while i < len(items):
        item = items[i]
        if isinstance(item, str) and re.match(r'^\[写真\d+\]$', item):
            nums = []
            while i < len(items) and isinstance(items[i], str) and re.match(r'^\[写真\d+\]$', items[i]):
                val = int(re.match(r'^\[写真(\d+)\]$', items[i]).group(1))
                if nums and val != nums[-1] + 1:
                    break
                nums.append(val)
                i += 1
            if len(nums) == 1:
                res.append(f"[写真{nums[0]}]")
            else:
                res.append(f"[写真{nums[0]}-{nums[-1]}]")
        else:
            res.append(item)
            i += 1
    return res


def _extract_bilingual_pairs(html_or_text: str, media_urls: list[str] | None = None) -> list[tuple[str, str] | str]:
    if not html_or_text:
        return []

    if media_urls and "<img" in html_or_text:
        import re
        def _repl_img(match):
            img_tag = match.group(0)
            src_m = re.search(r'src=["\']([^"\']+)["\']', img_tag)
            if src_m:
                src = src_m.group(1)
                for i, u in enumerate(media_urls):
                    if src in u or u in src or (src and u and src.split("/")[-1] == u.split("/")[-1]):
                        return f"\n<p>[写真{i+1}]</p>\n"
            return ""
        html_or_text = re.sub(r'<img[^>]*>', _repl_img, html_or_text)

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_or_text, "html.parser")
        items = []

        elements = soup.find_all(["em", "p"])
        for el in elements:
            if el.name == "em":
                ja_text = el.get_text("\n").strip()
                next_node = el.next_sibling
                zh_text = ""
                while next_node:
                    if getattr(next_node, "name", None) == "span":
                        zh_text = next_node.get_text("\n").strip()
                        break
                    next_node = next_node.next_sibling
                if ja_text:
                    items.append((ja_text, zh_text))
            elif el.name == "p":
                txt = el.get_text().strip()
                import re
                if re.match(r'^\[写真\d+\]$', txt):
                    items.append(txt)

        if items:
            return _compress_photo_placeholders(items)
    except Exception:
        pass

    lines = [line.strip() for line in (html_or_text or "").split("\n") if line.strip()]
    return [("\n".join(lines), "")] if lines else []


async def send_blog_post(post: dict) -> bool:
    """向配置的渠道推送一篇博客（按 zakablog 顺序：1.头信息 -> 2.全量图片 -> 3.中日对照正文）。"""
    import asyncio
    import httpx
    import config.config as cfg
    from src.logger import log_all

    group_key = post.get("group_key", "")
    group_name = post.get("group_name", "")
    emoji = EMOJI_MAP.get(group_key, "🤖")
    blog_url = post.get("url", "")
    author = post.get("author", "")
    title = post.get("title", "")
    date = post.get("date", "")

    import json
    imgs_raw = post.get("images") or (json.loads(post.get("images_json")) if isinstance(post.get("images_json"), str) else [])
    media_urls = [img for img in imgs_raw if isinstance(img, str) and img.startswith("http")]
    trans_model = post.get("translation_model") or ""
    model_line = f"模型：{trans_model}\n" if trans_model else ""

    # 1. 头消息 (Header text)
    header_text = (
        f"{emoji} {group_name} ブログ更新\n\n"
        f"作者：{author}\n"
        f"标题：{title}\n"
        f"时间：{date}\n"
        f"{model_line}"
        f"照片：共 {len(media_urls)} 张\n\n"
        f"👉 博客链接：\n{blog_url}"
    )

    # 2. 提取双语段落 (Pairs)
    trans_content = post.get("translation") or post.get("body_text", "")
    pairs = _extract_bilingual_pairs(trans_content, media_urls=media_urls)

    any_ok = False

    # ----------------------------------------------------
    # Channel 1: QQ 官方 Bot (受 blog_filter 控制)
    # ----------------------------------------------------
    if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False):
        from src.platforms.qq_official import get_configured_bots
        bots = get_configured_bots()
        for bot in bots:
            if not bot.group_openid and not bot.target_openid:
                continue
            if not bot.blog_filter or group_key not in bot.blog_filter:
                continue

            try:
                scope = "groups" if bot.group_openid else "users"
                target = bot.group_openid if bot.group_openid else bot.target_openid

                # Step 1: 发送头信息
                if scope == "groups":
                    await bot.send_group_text(target, header_text)
                else:
                    await bot.send_private_text(target, header_text)
                await asyncio.sleep(0.5)

                # Step 2: 发送全量图片
                for img_url in media_urls:
                    try:
                        async with httpx.AsyncClient(timeout=30) as c:
                            r = await c.get(img_url)
                            img_bytes = r.content
                        async with bot._lock:
                            if await bot.ensure_access_token():
                                fi = await bot._upload_media("image", img_bytes, scope=scope, target_openid=target)
                                if fi:
                                    await bot._send_uploaded_media(fi, scope=scope, target_openid=target)
                    except Exception:
                        pass
                    await asyncio.sleep(0.4)

                # Step 3: 发送中日对照正文 (*日文斜体* / 中文常规体，段落间 \n​\n 分隔)
                if pairs:
                    await bot.send_translation_qq(scope, target, pairs)

                any_ok = True
            except Exception as e:
                log_all(f"⚠️ 博客推送失败 [{bot.name}]: {e}", is_error=True)

    # ----------------------------------------------------
    # Channel 2: NapCat (受 blog_filter 控制)
    # ----------------------------------------------------
    if getattr(cfg, "ENABLE_NAPCAT_QQ", False):
        routes = getattr(cfg, "NAPCAT_ROUTES", [])
        for route in routes:
            filters = route.get("blog_filter") or []
            if not filters or group_key not in filters:
                continue
            gid = route.get("group_id")
            if not gid:
                continue

            from src.platforms.napcat import send_qq_message
            try:
                # Step 1 & 2: 头信息 + 全量图片消息链
                chain = [{"type": "text", "data": {"text": header_text}}]
                for img_url in media_urls:
                    chain.append({"type": "image", "data": {"file": img_url}})
                ok = await send_qq_message(int(gid), chain)
                if ok:
                    any_ok = True

                # Step 3: 中日对照正文
                if pairs:
                    from src.platforms.qq_official import _escape_qq_md
                    blocks = [f"*{_escape_qq_md(ja)}*\n{_escape_qq_md(zh)}" if zh else f"*{_escape_qq_md(ja)}*" for ja, zh in pairs]
                    body_txt = "\n\n".join(blocks)
                    await send_qq_message(int(gid), [{"type": "text", "data": {"text": body_txt}}])
            except Exception as e:
                log_all(f"⚠️ NapCat 博客推送失败 (群 {gid}): {e}", is_error=True)

    # ----------------------------------------------------
    # Channel 3: Telegram (受 blog_filter 控制)
    # ----------------------------------------------------
    if getattr(cfg, "ENABLE_TG_BOT", False):
        from src.platforms.tgbot import get_configured_bots
        bots = get_configured_bots()
        for bot in bots:
            if not bot.target_chat:
                continue
            if not bot.blog_filter or group_key not in bot.blog_filter:
                continue

            try:
                # Step 1 & 2: 发送图片专辑组 (Media Group)，第一张附带 Header Caption
                if media_urls:
                    await bot.send_media_group_photos(media_urls, caption=header_text)
                else:
                    await bot._send_html(bot.target_chat, header_text)

                await asyncio.sleep(1.0)

                # Step 3: 发送 Telegram 中日对照正文 (<i>日文斜体</i> / 中文常规体，切分<=4000字符)
                if pairs:
                    await bot.send_translation_tg(pairs)

                any_ok = True
            except Exception as e:
                log_all(f"⚠️ TG 博客推送失败 [{bot.name}]: {e}", is_error=True)

    return any_ok
