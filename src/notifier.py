# ============================================================
# notifier.py — QQ 多通道推送调度 (已解耦)
# ============================================================
import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

import config.config as cfg
from src.logger import log_all
from src.platforms.napcat import send_qq_message
from src.platforms import qq_official
from src.platforms.qq_official import get_configured_bots, has_bots
from src.platforms import tgbot
from src import health
from src.health import ErrorTier
from src.utils import match_member_filter


@dataclass(frozen=True)
class DeliveryAttempt:
    """一次路由投递的安全结果摘要。

    ``error_code`` 只保留可展示、可聚合的分类，不携带上游响应或凭证，
    这样既能诊断部分失败，也不会把 Bot Token 等内容带进管理端日志。
    """

    channel: str
    route_id: str
    label: str
    target: str
    ok: bool
    error_code: str | None = None


@dataclass(frozen=True)
class DeliveryReport:
    """一条消息在多个已匹配目标上的投递结果。"""

    attempts: tuple[DeliveryAttempt, ...]

    @property
    def ok(self) -> bool:
        """至少一个目标已成功接收；保持旧接口的去重/重试语义。"""
        return any(attempt.ok for attempt in self.attempts)

    @property
    def partial(self) -> bool:
        return self.ok and any(not attempt.ok for attempt in self.attempts)

    @property
    def success_count(self) -> int:
        return sum(attempt.ok for attempt in self.attempts)

    @property
    def failure_count(self) -> int:
        return sum(not attempt.ok for attempt in self.attempts)


def _classify_delivery_exception(exc: Exception) -> str:
    """将跨通道异常压缩为稳定、安全的错误码。"""
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.RequestError):
        return "network_error"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_response"
    return "unexpected_error"


async def _run_delivery(
    channel: str,
    route_id: str,
    label: str,
    target: str,
    sender: Callable[[], Awaitable[bool]],
    *,
    known_error: str | None = None,
) -> DeliveryAttempt:
    """执行单个路由，确保一个协程异常不会吞掉同批其他通道。"""
    if known_error:
        return DeliveryAttempt(channel, route_id, label, target, False, known_error)
    try:
        ok = bool(await sender())
    except Exception as exc:  # 外部 SDK / 网络调用的边界，必须隔离单路失败
        error_code = _classify_delivery_exception(exc)
        return DeliveryAttempt(channel, route_id, label, target, False, error_code)
    return DeliveryAttempt(
        channel, route_id, label, target, ok, None if ok else "delivery_failed"
    )


def _record_delivery_report(context: str, report: DeliveryReport) -> None:
    """写入通道健康状态和可读日志，不记录不可信的原始异常文本。"""
    for attempt in report.attempts:
        health.get_tracker().record_channel(
            attempt.channel, attempt.ok, attempt.error_code
        )
        if attempt.ok:
            log_all(
                f"✅ [{context} | 通道: {attempt.label} | 目标: {attempt.target}] 推送成功",
                is_debug=True,
            )

    failed = [attempt for attempt in report.attempts if not attempt.ok]
    if not failed:
        return

    details = "; ".join(
        f"{attempt.label} -> {attempt.target} ({attempt.error_code})"
        for attempt in failed
    )
    state = "部分目标失败" if report.ok else "全部目标失败"
    log_all(
        f"⚠️ [{context}] {state}，成功 {report.success_count}/{len(report.attempts)}；{details}",
        is_error=True,
    )
    health.get_tracker().record_error(
        f"{context}{state}: {details}", ErrorTier.TRANSIENT
    )


def enabled_channels() -> list[str]:
    channels: list[str] = []
    if getattr(cfg, "ENABLE_NAPCAT_QQ", False):
        channels.append("napcat")
    if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False) and has_bots():
        channels.append("official")
    # 只有成功初始化且具备专属 Token 的 Bot 才算可用通道；
    # 声明了路由但凭证缺失时不能把 Telegram 误报为已启用。
    if getattr(cfg, "ENABLE_TG_BOT", False) and tgbot.get_configured_bots():
        channels.append("tg")
    return channels


async def send_member_message_detailed(
    member: dict, message_chain: list[dict], *, skip_route_ids: set[str] | None = None
) -> DeliveryReport:
    """向已匹配的通道并发广播成员消息，并返回每个目标的结果。

    这是新调用方可使用的细粒度接口；旧的 ``send_member_message`` 保持 bool
    返回值，避免把部分成功误当作需要重试的整条消息，从而造成重复推送。
    """
    m_name = member.get("m_name") or member.get("name") or "未知成员"
    m_id = member.get("m_id") or member.get("id")
    skip_route_ids = skip_route_ids or set()
    tasks: list[Awaitable[DeliveryAttempt]] = []

    # 1. NapCat QQ 路由并发任务
    if cfg.ENABLE_NAPCAT_QQ:
        routes = getattr(cfg, "NAPCAT_ROUTES", [])
        for route in routes:
            if not route.get("push_message", True):
                continue
            filters = route.get("member_filter") or []
            if match_member_filter(m_name, filters, m_id):
                gid = route.get("group_id")
                if gid:
                    route_id = f"napcat:{gid}"
                    if route_id in skip_route_ids:
                        continue
                    r_label = f"NapCat:{route['remark']}" if route.get("remark") else "NapCat"
                    tasks.append(_run_delivery(
                        "napcat", route_id, r_label, f"群 {gid}",
                        lambda target_gid=gid: send_qq_message(target_gid, message_chain),
                    ))

    # 2. QQ 官方 Bot 路由并发任务
    if cfg.ENABLE_QQ_OFFICIAL_BOT:
        bots = get_configured_bots()
        media_payloads = None
        media_error = None
        if bots:
            try:
                media_payloads = await qq_official.download_media_payloads(member, message_chain)
            except (asyncio.TimeoutError, httpx.RequestError, OSError, ValueError):
                # 媒体预处理本身也属于官方 Bot 通道失败；不可阻塞 NapCat/TG 投递。
                media_error = "media_prepare_failed"
            for bot in bots:
                b_label = f"官方Bot:{bot.remark} ({bot.name})" if getattr(bot, "remark", None) else f"官方Bot:{bot.name}"
                # 单聊目标（target_openid）
                if bot.target_openid and bot.push_message and match_member_filter(m_name, bot.member_filter, m_id):
                    route_id = f"official:{bot.name}:private"
                    if route_id in skip_route_ids:
                        continue
                    tasks.append(_run_delivery(
                        f"official:{bot.name}", route_id, b_label,
                        f"用户 {bot.target_openid[:10]}...",
                        lambda b=bot: b.send_message_chain(
                            member, message_chain, media_payloads=media_payloads
                        ),
                        known_error=media_error,
                    ))

                # 群聊目标（group_openid）
                if bot.group_openid and bot.push_message and match_member_filter(m_name, bot.member_filter, m_id):
                    route_id = f"official:{bot.name}:group"
                    if route_id in skip_route_ids:
                        continue
                    tasks.append(_run_delivery(
                        f"official:{bot.name}:group", route_id, b_label,
                        f"群 {bot.group_openid[:10]}...",
                        lambda b=bot: b.send_message_chain_to_group(
                            b.group_openid, member, message_chain,
                            media_payloads=media_payloads,
                        ),
                        known_error=media_error,
                    ))

    # 3. TG Bot 路由并发任务
    if cfg.ENABLE_TG_BOT:
        tg_bots = tgbot.get_configured_bots()
        for bot in tg_bots:
            if bot.target_chat and bot.push_message and match_member_filter(m_name, bot.member_filter, m_id):
                route_id = f"tg:{bot.name}"
                if route_id in skip_route_ids:
                    continue
                tg_label = f"TG:{bot.remark} ({bot.name})" if getattr(bot, "remark", None) else f"TG:{bot.name}"
                tasks.append(_run_delivery(
                    f"tg:{bot.name}", route_id, tg_label, f"Chat {bot.target_chat}",
                    lambda b=bot: b.send_member_message(message_chain),
                ))

    if not tasks:
        return DeliveryReport(())

    report = DeliveryReport(tuple(await asyncio.gather(*tasks)))
    _record_delivery_report(f"成员消息 | {m_name}", report)
    return report


async def send_member_message(member: dict, message_chain: list[dict]) -> bool:
    """兼容旧调用方：无匹配目标视为已处理，至少一个成功才确认消息。"""
    report = await send_member_message_detailed(member, message_chain)
    return report.ok or not report.attempts


async def send_report_message(text: str) -> bool:
    """向所有已启用通道并发发送运行报告（每日摘要等，非警报语义、无前缀）。"""
    tasks: list[Awaitable[DeliveryAttempt]] = []

    if getattr(cfg, "ENABLE_NAPCAT_QQ", False):
        for route in getattr(cfg, "NAPCAT_ROUTES", []):
            if route.get("push_alert") and route.get("group_id"):
                gid = route["group_id"]
                label = f"NapCat:{route.get('remark')}" if route.get("remark") else "NapCat"
                tasks.append(_run_delivery(
                    "napcat", f"napcat:{gid}", label, f"群 {gid}",
                    lambda target_gid=gid: send_qq_message(
                        target_gid, [{"type": "text", "data": {"text": text}}]
                    ),
                ))

    if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False):
        for bot in get_configured_bots():
            if bot.target_openid and bot.push_alert:
                label = f"官方Bot:{bot.remark} ({bot.name})" if getattr(bot, "remark", None) else f"官方Bot:{bot.name}"
                tasks.append(_run_delivery(
                    f"official:{bot.name}", f"official:{bot.name}:private", label, f"用户 {bot.target_openid[:10]}...",
                    lambda b=bot: b.send_text(text),
                ))

    if getattr(cfg, "ENABLE_TG_BOT", False):
        for bot in tgbot.get_configured_bots():
            if bot.target_chat and bot.push_alert:
                label = f"TG:{bot.remark} ({bot.name})" if getattr(bot, "remark", None) else f"TG:{bot.name}"
                tasks.append(_run_delivery(
                    f"tg:{bot.name}", f"tg:{bot.name}", label, f"Chat {bot.target_chat}",
                    lambda b=bot: b.send_text(text),
                ))

    if not tasks:
        return False
    report = DeliveryReport(tuple(await asyncio.gather(*tasks)))
    _record_delivery_report("运行报告", report)
    return report.ok


async def send_alert_message(target_group: int, text: str) -> bool:
    """向所有配置为推送告警的通道并发发送系统警报。"""
    channels = enabled_channels()
    if not channels:
        log_all(f"⏸️ 没有可用的推送通道，警报未发送: {text}", is_error=True)
        return False

    tasks: list[Awaitable[DeliveryAttempt]] = []

    # NapCat 群告警
    if getattr(cfg, "ENABLE_NAPCAT_QQ", False):
        for route in getattr(cfg, "NAPCAT_ROUTES", []):
            if route.get("push_alert") and route.get("group_id"):
                gid = route["group_id"]
                label = f"NapCat:{route.get('remark')}" if route.get("remark") else "NapCat"
                tasks.append(_run_delivery(
                    "napcat", f"napcat:{gid}", label, f"群 {gid}",
                    lambda target_gid=gid: send_qq_message(
                        target_gid,
                        [{"type": "text", "data": {"text": f"📢 系统警报\n{text}"}}],
                    ),
                ))

    # QQ 官方 Bot 告警
    if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False):
        for bot in get_configured_bots():
            if bot.target_openid and bot.push_alert:
                label = f"官方Bot:{bot.remark} ({bot.name})" if getattr(bot, "remark", None) else f"官方Bot:{bot.name}"
                tasks.append(_run_delivery(
                    f"official:{bot.name}", f"official:{bot.name}:private", label, f"用户 {bot.target_openid[:10]}...",
                    lambda b=bot: b.send_text(f"📢 系统警报\n{text}"),
                ))

    # TG Bot 告警
    if getattr(cfg, "ENABLE_TG_BOT", False):
        for bot in tgbot.get_configured_bots():
            if bot.push_alert and bot.target_chat:
                label = f"TG:{bot.remark} ({bot.name})" if getattr(bot, "remark", None) else f"TG:{bot.name}"
                tasks.append(_run_delivery(
                    f"tg:{bot.name}", f"tg:{bot.name}", label, f"Chat {bot.target_chat}",
                    lambda b=bot: b.send_text(f"📢 系统警报\n{text}"),
                ))

    if not tasks:
        return False
    report = DeliveryReport(tuple(await asyncio.gather(*tasks)))
    _record_delivery_report("系统警报", report)
    return report.ok


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
    except Exception:  # nosec B110
        pass

    lines = [line.strip() for line in (html_or_text or "").split("\n") if line.strip()]
    return [("\n".join(lines), "")] if lines else []





async def send_blog_post(post: dict) -> bool:
    """向配置的渠道推送一篇博客。

    无论何种模式，第一条均统一发送博客提醒头（作者/标题/时间/链接）；
    后续内容严格根据通道配置的 blog_card_mode 分发：
    1. card_only: 提醒头 -> 精美长图卡片（极简防刷屏，坚决不发单张原图，不发正文纯文本）；
    2. card_and_images: 提醒头 -> 精美长图卡片 -> 紧随推送全量高清原始写真（方便存图）；
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
    except (OSError, RuntimeError, ValueError) as e:
        log_all(f"💡 长图卡片预渲染跳过或失败，自动降级为标准图文: {type(e).__name__}", is_debug=True)

    # 辅助函数：向 QQ 官方 Bot 发送全量原始写真
    async def _send_qq_official_raw_images(bot, scope, target):
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

                if not img_bytes or not await bot.send_media_file(scope, target, "image", img_bytes):
                    return False
            except (OSError, httpx.HTTPError, ValueError) as ex:
                log_all(f"⚠️ 官方 Bot [{bot.name}] 博客图片推送失败: {type(ex).__name__}", is_debug=True)
                return False
            await asyncio.sleep(0.4)
        return True

    tasks = []

    # ----------------------------------------------------
    # Channel 1: QQ 官方 Bot 并发分发
    # ----------------------------------------------------
    if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False):
        from src.platforms.qq_official import get_configured_bots
        for bot in get_configured_bots():
            if not bot.target_openid and not bot.group_openid:
                continue
            if not getattr(bot, "push_blog", False):
                continue
            if bot.blog_filter and group_key not in bot.blog_filter:
                continue

            async def _send_official_blog(b=bot):
                try:
                    scope = "groups" if b.group_openid else "users"
                    target = b.group_openid if b.group_openid else b.target_openid
                    mode = getattr(b, "blog_card_mode", "") or getattr(cfg, "BLOG_CARD_MODE", "card_and_images")

                    if scope == "groups":
                        if not await b.send_group_text(target, header_text):
                            return False
                    else:
                        if not await b.send_private_text(target, header_text):
                            return False
                    await asyncio.sleep(0.3)

                    if mode == "card_only":
                        sent_card = False
                        if card_path and card_path.exists():
                            with open(card_path, "rb") as f:
                                card_bytes = f.read()
                            sent_card = await b.send_media_file(scope, target, "image", card_bytes)
                        if not sent_card and pairs:
                            await b.send_translation_qq(scope, target, pairs)

                    elif mode == "card_and_images":
                        sent_card = False
                        if card_path and card_path.exists():
                            with open(card_path, "rb") as f:
                                card_bytes = f.read()
                            sent_card = await b.send_media_file(scope, target, "image", card_bytes)
                            await asyncio.sleep(0.3)

                        if sent_card:
                            if media_urls:
                                if not await _send_qq_official_raw_images(b, scope, target):
                                    return False
                        else:
                            if media_urls:
                                if not await _send_qq_official_raw_images(b, scope, target):
                                    return False
                            if pairs:
                                await b.send_translation_qq(scope, target, pairs)
                    else:
                        if media_urls:
                            if not await _send_qq_official_raw_images(b, scope, target):
                                return False
                        if pairs:
                            await b.send_translation_qq(scope, target, pairs)
                    return True
                except (OSError, httpx.HTTPError, ValueError, RuntimeError) as e:
                    b_label = f"{b.remark} ({b.name})" if getattr(b, "remark", None) else b.name
                    log_all(f"⚠️ 官方 QQ Bot 博客推送失败 [{b_label}]: {type(e).__name__}", is_error=True)
                    return False

            tasks.append(_send_official_blog())

    # ----------------------------------------------------
    # Channel 2: NapCat 并发分发
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

            async def _send_napcat_blog(r=route, target_gid=gid):
                from src.platforms.napcat import send_qq_message
                try:
                    mode = r.get("blog_card_mode") or getattr(cfg, "BLOG_CARD_MODE", "card_and_images")
                    if not await send_qq_message(int(target_gid), [{"type": "text", "data": {"text": header_text}}]):
                        return False
                    await asyncio.sleep(0.3)

                    if mode == "card_only":
                        sent_card = False
                        if card_path and card_path.exists():
                            card_file_uri = f"file:///{card_path.resolve().as_posix()}"
                            sent_card = await send_qq_message(int(target_gid), [{"type": "image", "data": {"file": card_file_uri}}])
                        if not sent_card and pairs:
                            from src.platforms.qq_official import _escape_qq_md
                            blocks = [f"*{_escape_qq_md(ja)}*\n{_escape_qq_md(zh)}" if zh else f"*{_escape_qq_md(ja)}*" for ja, zh in pairs]
                            body_txt = "\n\n".join(blocks)
                            await send_qq_message(int(target_gid), [{"type": "text", "data": {"text": body_txt}}])

                    elif mode == "card_and_images":
                        sent_card = False
                        if card_path and card_path.exists():
                            card_file_uri = f"file:///{card_path.resolve().as_posix()}"
                            sent_card = await send_qq_message(int(target_gid), [{"type": "image", "data": {"file": card_file_uri}}])
                            await asyncio.sleep(0.3)

                        if sent_card:
                            if media_urls:
                                raw_chain = [{"type": "image", "data": {"file": u}} for u in media_urls]
                                await send_qq_message(int(target_gid), raw_chain)
                        else:
                            if media_urls:
                                raw_chain = [{"type": "image", "data": {"file": u}} for u in media_urls]
                                await send_qq_message(int(target_gid), raw_chain)
                                await asyncio.sleep(0.3)
                            if pairs:
                                from src.platforms.qq_official import _escape_qq_md
                                blocks = [f"*{_escape_qq_md(ja)}*\n{_escape_qq_md(zh)}" if zh else f"*{_escape_qq_md(ja)}*" for ja, zh in pairs]
                                body_txt = "\n\n".join(blocks)
                                await send_qq_message(int(target_gid), [{"type": "text", "data": {"text": body_txt}}])
                    else:
                        if media_urls:
                            raw_chain = [{"type": "image", "data": {"file": u}} for u in media_urls]
                            await send_qq_message(int(target_gid), raw_chain)
                            await asyncio.sleep(0.3)
                        if pairs:
                            from src.platforms.qq_official import _escape_qq_md
                            blocks = [f"*{_escape_qq_md(ja)}*\n{_escape_qq_md(zh)}" if zh else f"*{_escape_qq_md(ja)}*" for ja, zh in pairs]
                            body_txt = "\n\n".join(blocks)
                            await send_qq_message(int(target_gid), [{"type": "text", "data": {"text": body_txt}}])
                    return True
                except (OSError, ValueError, RuntimeError) as e:
                    r_label = f"{r.get('remark')} ({target_gid})" if r.get("remark") else f"群 {target_gid}"
                    log_all(f"⚠️ NapCat 博客推送失败 [{r_label}]: {type(e).__name__}", is_error=True)
                    return False

            tasks.append(_send_napcat_blog())

    # ----------------------------------------------------
    # Channel 3: Telegram 并发分发
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

            async def _send_tg_blog(b=bot):
                try:
                    mode = getattr(b, "blog_card_mode", "") or getattr(cfg, "BLOG_CARD_MODE", "card_and_images")
                    if not await b._post_message(b.target_chat, header_text):
                        return False
                    await asyncio.sleep(0.3)

                    if mode == "card_only":
                        sent_card = False
                        if card_path and card_path.exists():
                            sent_card = await b.send_photo_file(str(card_path))
                        if not sent_card and pairs:
                            await b.send_translation_tg(pairs)

                    elif mode == "card_and_images":
                        sent_card = False
                        if card_path and card_path.exists():
                            sent_card = await b.send_photo_file(str(card_path))
                            await asyncio.sleep(0.3)

                        if sent_card:
                            if media_urls:
                                await b.send_media_group_photos(media_urls)
                        else:
                            if media_urls:
                                await b.send_media_group_photos(media_urls)
                                await asyncio.sleep(0.3)
                            if pairs:
                                await b.send_translation_tg(pairs)
                    else:
                        if media_urls:
                            await b.send_media_group_photos(media_urls)
                            await asyncio.sleep(0.3)
                        if pairs:
                            await b.send_translation_tg(pairs)
                    return True
                except (OSError, ValueError, RuntimeError) as e:
                    b_label = f"{b.remark} ({b.name})" if getattr(b, "remark", None) else b.name
                    log_all(f"⚠️ TG 博客推送失败 [{b_label}]: {type(e).__name__}", is_error=True)
                    return False

            tasks.append(_send_tg_blog())

    if not tasks:
        return True

    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [type(r).__name__ for r in results if isinstance(r, Exception)]
    if failures:
        log_all(f"⚠️ 博客路由任务异常: {' · '.join(failures)}", is_error=True)
    return any(r is True for r in results)
