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
from src.utils import match_member_filter


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
    """向已启用的通道广播成员消息。

    解耦模式下：各通道按自己的 target/group 和 member_filter 进行独立路由。
    返回 True 表示至少一个通道发送成功，或没有启用任何通道（不应重试）；
    返回 False 表示启用的通道全部失败（需要按重试逻辑处理）。
    """
    m_name = member.get("name", "未知成员")
    m_id = member.get("id")
    napcat_ok = True

    # 1. NapCat QQ 路由
    if cfg.ENABLE_NAPCAT_QQ:
        routes = getattr(cfg, "NAPCAT_ROUTES", [])
        matched_any = False
        for route in routes:
            if not route.get("push_message", True):
                continue
            filters = route.get("member_filter") or []
            if match_member_filter(m_name, filters, m_id):
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
            if not match_member_filter(m_name, bot.member_filter, m_id):
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
            if not match_member_filter(m_name, bot.member_filter, m_id):
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
            if not match_member_filter(m_name, bot.member_filter, m_id):
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
                    name = getattr(next_node, "name", None)
                    if name in ("em", "p", "img"):
                        break
                    if name == "span":
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
    """向配置的渠道推送一篇博客。

    无论何种模式，第一条均统一发送博客提醒头（作者/标题/时间/链接）；
    后续内容根据通道配置的 blog_card_mode 推送：
    1. card_and_images: 提醒头 -> 精美长图卡片 -> 紧随推送全量高清原始写真（方便存图）；
    2. card_only: 提醒头 -> 精美长图卡片（极简防刷屏，不发单张原图，不发正文纯文本）；
    3. text_and_images: 提醒头 -> 全量高清原图 -> 中日对照正文。
    """
    import asyncio
    import json
    import os
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

    imgs_raw = post.get("images") or (json.loads(post.get("images_json")) if isinstance(post.get("images_json"), str) else [])
    media_urls = [img for img in imgs_raw if isinstance(img, str) and img.startswith("http")]
    trans_model = post.get("translation_model") or ""
    model_line = f"模型：{trans_model}\n" if trans_model else ""

    # 1. 统一提醒头消息 (Header text)
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

    # 3. 预渲染精美长图卡片（若 Playwright 不可用或渲染异常则自动降级为 None）
    card_path = None
    try:
        from src.blog_card_renderer import render_blog_card
        card_path = await render_blog_card(post)
    except Exception as e:
        log_all(f"💡 长图卡片预渲染跳过或失败，自动降级为标准图文: {e}", is_debug=True)

    any_ok = False

    # ----------------------------------------------------
    # Channel 1: QQ 官方 Bot (受 push_blog + blog_filter 控制)
    # ----------------------------------------------------
    if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False):
        from src.platforms.qq_official import get_configured_bots
        bots = get_configured_bots()
        for bot in bots:
            if not bot.group_openid and not bot.target_openid:
                continue
            if not getattr(bot, "push_blog", False):
                continue
            if bot.blog_filter and group_key not in bot.blog_filter:
                continue

            try:
                scope = "groups" if bot.group_openid else "users"
                target = bot.group_openid if bot.group_openid else bot.target_openid
                mode = getattr(bot, "blog_card_mode", "") or getattr(cfg, "BLOG_CARD_MODE", "card_and_images")

                # Step 1: 所有模式统一发送博客提醒头
                if scope == "groups":
                    await bot.send_group_text(target, header_text)
                else:
                    await bot.send_private_text(target, header_text)
                await asyncio.sleep(0.5)

                # Step 2: 内容推送
                if card_path and card_path.exists() and mode in ("card_and_images", "card_only"):
                    # 发送精美长图卡片
                    with open(card_path, "rb") as f:
                        card_bytes = f.read()
                    await bot.send_media_file(scope, target, "image", card_bytes)
                    await asyncio.sleep(0.5)

                    # 若为 card_and_images 模式，后续推送全量高清原始写真供存图
                    if mode == "card_and_images" and media_urls:
                        local_paths = post.get("image_paths") or []
                        for idx, img_url in enumerate(media_urls):
                            try:
                                img_bytes = None
                                if idx < len(local_paths) and local_paths[idx]:
                                    lp = local_paths[idx]
                                    if not os.path.isabs(lp):
                                        lp = os.path.join("data/blog_images", lp)
                                    if os.path.exists(lp):
                                        try:
                                            with open(lp, "rb") as f:
                                                img_bytes = f.read()
                                        except OSError:
                                            pass

                                if not img_bytes:
                                    headers = {
                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                        "Referer": blog_url or img_url,
                                    }
                                    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                                        r = await c.get(img_url, headers=headers)
                                        if r.status_code == 200:
                                            img_bytes = r.content

                                if img_bytes:
                                    await bot.send_media_file(scope, target, "image", img_bytes)
                            except Exception as ex:
                                log_all(f"⚠️ 官方 Bot [{bot.name}] 博客图片推送异常: {ex}", is_debug=True)
                            await asyncio.sleep(0.4)
                else:
                    # 传统图文模式 (text_and_images 或长图失败 fallback)
                    local_paths = post.get("image_paths") or []
                    for idx, img_url in enumerate(media_urls):
                        try:
                            img_bytes = None
                            if idx < len(local_paths) and local_paths[idx]:
                                lp = local_paths[idx]
                                if not os.path.isabs(lp):
                                    lp = os.path.join("data/blog_images", lp)
                                if os.path.exists(lp):
                                    try:
                                        with open(lp, "rb") as f:
                                            img_bytes = f.read()
                                    except OSError:
                                        pass

                            if not img_bytes:
                                headers = {
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                    "Referer": blog_url or img_url,
                                }
                                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                                    r = await c.get(img_url, headers=headers)
                                    if r.status_code == 200:
                                        img_bytes = r.content

                            if img_bytes:
                                sent = await bot.send_media_file(scope, target, "image", img_bytes)
                                if not sent:
                                    log_all(f"⚠️ 官方 Bot [{bot.name}] 博客图片推送未成功 (第 {idx+1}/{len(media_urls)} 张)", is_debug=True)
                        except Exception as ex:
                            log_all(f"⚠️ 官方 Bot [{bot.name}] 博客图片下载或发送异常: {ex}", is_debug=True)
                        await asyncio.sleep(0.4)

                    if pairs:
                        await bot.send_translation_qq(scope, target, pairs)

                any_ok = True
            except Exception as e:
                log_all(f"⚠️ 博客推送失败 [{bot.name}]: {e}", is_error=True)

    # ----------------------------------------------------
    # Channel 2: NapCat (受 push_blog + blog_filter 控制)
    # ----------------------------------------------------
    if getattr(cfg, "ENABLE_NAPCAT_QQ", False):
        routes = getattr(cfg, "NAPCAT_ROUTES", [])
        for route in routes:
            if not route.get("push_blog", False):
                continue
            filters = route.get("blog_filter") or []
            if filters and group_key not in filters:
                continue
            gid = route.get("group_id")
            if not gid:
                continue

            from src.platforms.napcat import send_qq_message
            try:
                mode = route.get("blog_card_mode") or getattr(cfg, "BLOG_CARD_MODE", "card_and_images")

                # Step 1: 所有模式统一发送博客提醒头
                await send_qq_message(int(gid), [{"type": "text", "data": {"text": header_text}}])
                await asyncio.sleep(0.5)

                # Step 2: 内容推送
                if card_path and card_path.exists() and mode in ("card_and_images", "card_only"):
                    # 发送精美长图卡片
                    card_file_uri = f"file:///{card_path.resolve().as_posix()}"
                    ok = await send_qq_message(int(gid), [{"type": "image", "data": {"file": card_file_uri}}])
                    if ok:
                        any_ok = True
                    await asyncio.sleep(0.5)

                    # 若为 card_and_images 模式，后续推送全量高清原始写真供存图
                    if mode == "card_and_images" and media_urls:
                        raw_chain = [{"type": "image", "data": {"file": u}} for u in media_urls]
                        await send_qq_message(int(gid), raw_chain)
                else:
                    # 传统图文模式 (text_and_images 或长图失败 fallback)
                    if media_urls:
                        raw_chain = [{"type": "image", "data": {"file": u}} for u in media_urls]
                        await send_qq_message(int(gid), raw_chain)
                        await asyncio.sleep(0.5)

                    if pairs:
                        from src.platforms.qq_official import _escape_qq_md
                        blocks = [f"*{_escape_qq_md(ja)}*\n{_escape_qq_md(zh)}" if zh else f"*{_escape_qq_md(ja)}*" for ja, zh in pairs]
                        body_txt = "\n\n".join(blocks)
                        await send_qq_message(int(gid), [{"type": "text", "data": {"text": body_txt}}])

                any_ok = True
            except Exception as e:
                log_all(f"⚠️ NapCat 博客推送失败 (群 {gid}): {e}", is_error=True)

    # ----------------------------------------------------
    # Channel 3: Telegram (受 push_blog + blog_filter 控制)
    # ----------------------------------------------------
    if getattr(cfg, "ENABLE_TG_BOT", False):
        from src.platforms.tgbot import get_configured_bots
        bots = get_configured_bots()
        for bot in bots:
            if not bot.target_chat:
                continue
            if not getattr(bot, "push_blog", False):
                continue
            if bot.blog_filter and group_key not in bot.blog_filter:
                continue

            try:
                mode = getattr(bot, "blog_card_mode", "") or getattr(cfg, "BLOG_CARD_MODE", "card_and_images")

                # Step 1: 所有模式统一发送博客提醒头
                await bot._send_html(bot.target_chat, header_text)
                await asyncio.sleep(0.8)

                # Step 2: 内容推送
                if card_path and card_path.exists() and mode in ("card_and_images", "card_only"):
                    # 发送精美长图卡片
                    await bot.send_photo_file(str(card_path))
                    await asyncio.sleep(0.8)

                    # 若为 card_and_images 模式，后续推送全量高清原始写真相册供存图
                    if mode == "card_and_images" and media_urls:
                        await bot.send_media_group_photos(media_urls)
                else:
                    # 传统图文模式 (text_and_images 或长图失败 fallback)
                    if media_urls:
                        await bot.send_media_group_photos(media_urls)
                        await asyncio.sleep(0.8)

                    if pairs:
                        await bot.send_translation_tg(pairs)

                any_ok = True
            except Exception as e:
                log_all(f"⚠️ TG 博客推送失败 [{bot.name}]: {e}", is_error=True)

    return any_ok
