# ============================================================
# notifier.py — QQ 多通道推送调度
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
    if cfg.ENABLE_NAPCAT_QQ:
        channels.append("napcat")
    if cfg.ENABLE_QQ_OFFICIAL_BOT and has_bots():
        channels.append("official")
    if cfg.ENABLE_TG_BOT:
        channels.append("tg")
    return channels


async def send_member_message(member: dict, message_chain: list[dict]) -> bool:
    """
    向所有启用的 QQ 通道推送成员消息。
    NapCat 保持原有可靠性语义：失败会阻断时间戳记录。
    官方 Bot 与 NapCat 同开时作为旁路，失败只记日志，避免官方频控造成群消息重复。
    """
    channels = enabled_channels()
    if not channels:
        log_all("⏸️ QQ 推送通道均未启用，本条消息仅记录状态", is_error=True)
        return True

    napcat_ok = True

    if cfg.ENABLE_NAPCAT_QQ:
        for gid in member.get("target_groups") or []:
            ok = await send_qq_message(gid, message_chain)
            health.get_tracker().record_channel("napcat", ok, f"群 {gid} 发送失败")
            if not ok:
                napcat_ok = False
                log_all(f"⚠️ NapCat QQ 推送失败 (群 {gid})", is_error=True)

    if cfg.ENABLE_QQ_OFFICIAL_BOT:
        bots = get_configured_bots()
        media_payloads = None
        if bots:
            media_payloads = await qq_official.download_media_payloads(member, message_chain)

        def _resolve_bot(name: str):
            if name:
                for b in bots:
                    if b.name == name:
                        return b
                return None
            return bots[0] if bots else None

        # 单聊目标（target_openid）—— 受 member_filter 过滤
        for bot in bots:
            if not bot.target_openid:
                continue
            if bot.member_filter and member.get("m_name", "") not in bot.member_filter:
                continue
            ok = await bot.send_message_chain(member, message_chain, media_payloads=media_payloads)
            health.get_tracker().record_channel(f"official:{bot.name}", ok)
            if not ok:
                log_all(f"⚠️ 官方 QQ Bot [{bot.name}] 单聊推送失败", is_error=True)

        # 群聊目标（group_openid）—— 受 member_filter 过滤
        for bot in bots:
            if not bot.group_openid:
                continue
            if bot.member_filter and member.get("m_name", "") not in bot.member_filter:
                continue
            ok = await bot.send_message_chain_to_group(
                bot.group_openid, member, message_chain, media_payloads=media_payloads)
            health.get_tracker().record_channel(f"official:{bot.name}:group", ok)
            if not ok:
                log_all(f"⚠️ 官方 QQ Bot [{bot.name}] 群推送失败 ({bot.group_openid[:16]}…)", is_error=True)

    # TG Bot 作为旁路推送，失败不影响 QQ 状态
    if cfg.ENABLE_TG_BOT:
        tg_ok = await tgbot.send_member_message(member, message_chain)
        health.get_tracker().record_channel("tg", tg_ok)
        if not tg_ok:
            log_all(f"⚠️ TG Bot 推送失败 [{member.get('m_name', '?')}]", is_error=True)

    if cfg.ENABLE_NAPCAT_QQ:
        return napcat_ok
    return True


async def send_report_message(text: str) -> bool:
    """向所有已启用通道发送运行报告（每日摘要等，非警报语义、无前缀）。
    目标沿用告警路由：NapCat 取 monitor 里第一个 QQ 群，TG 取第一个频道。"""
    any_ok = False

    if cfg.ENABLE_NAPCAT_QQ:
        gid = next((g for m in cfg.MONITOR_LIST for g in (m.get("target_groups") or [])), 0)
        if gid:
            ok = await send_qq_message(gid, [{"type": "text", "data": {"text": text}}])
            any_ok = any_ok or ok

    if cfg.ENABLE_QQ_OFFICIAL_BOT:
        for bot in get_configured_bots():
            if not bot.target_openid:
                continue      # 群专用 Bot，跳过单聊报告
            if await bot.send_text(text):
                any_ok = True

    if cfg.ENABLE_TG_BOT:
        cid = next(((m.get("tg_chat_id") or "").strip() for m in cfg.MONITOR_LIST
                    if (m.get("tg_chat_id") or "").strip()), "")
        if cid and await tgbot.send_text(cid, text):
            any_ok = True

    return any_ok


async def send_alert_message(target_group: int, text: str) -> bool:
    """向所有已启用的推送通道发送系统警报。"""
    channels = enabled_channels()
    if not channels:
        log_all(f"⏸️ 没有可用的推送通道，警报未发送: {text}", is_error=True)
        return False

    any_ok = False

    # NapCat 群告警（target_group 为 0 表示该账号无关联 QQ 群，跳过）
    if cfg.ENABLE_NAPCAT_QQ and target_group:
        alert_chain = [{"type": "text", "data": {"text": f"📢 系统警报\n{text}"}}]
        ok = await send_qq_message(target_group, alert_chain)
        if ok:
            any_ok = True
        else:
            health.get_tracker().record_error(
                f"NapCat 告警发送失败 (群 {target_group})", ErrorTier.TRANSIENT
            )
            if error_logger:
                error_logger.error(f"NapCat 告警发送失败: {text}")

    # QQ 官方 Bot 告警 — 发给所有已配置的 Bot
    if cfg.ENABLE_QQ_OFFICIAL_BOT:
        bots = get_configured_bots()
        if not bots:
            log_all(f"⏸️ 没有已配置的 QQ 官方 Bot，警报未发送: {text}", is_error=True)
        else:
            for bot in bots:
                if not bot.target_openid:
                    continue    # 群专用 Bot，跳过单聊告警
                ok = await bot.send_text(f"📢 系统警报\n{text}")
                if ok:
                    any_ok = True
                else:
                    health.get_tracker().record_error(
                        f"官方Bot [{bot.name}] 告警发送失败", ErrorTier.TRANSIENT
                    )

    # TG Bot 告警
    if cfg.ENABLE_TG_BOT:
        tg_chat_id = ""
        for m in cfg.MONITOR_LIST:
            cid = (m.get("tg_chat_id") or "").strip()
            if not cid:
                continue
            # target_group 为 0（无关联 QQ 群）时取第一个可用频道
            if not target_group or target_group in (m.get("target_groups") or []):
                tg_chat_id = cid
                break
        if tg_chat_id:
            ok = await tgbot.send_alert(tg_chat_id, text)
            if ok:
                any_ok = True
            else:
                health.get_tracker().record_error(
                    "TG Bot 告警发送失败", ErrorTier.TRANSIENT
                )
        else:
            log_all(f"⏸️ 群 {target_group} 无关联 TG 频道，TG 警报跳过", is_debug=True)

    return any_ok
