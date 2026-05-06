# ============================================================
# notifier.py — QQ 多通道推送调度
# ============================================================
from config.config import ENABLE_NAPCAT_QQ, ENABLE_QQ_OFFICIAL_BOT
from src.logger import log_all
from src.platforms.napcat import send_qq_message
from src.platforms.qq_official import get_bots, has_bots


def enabled_channels() -> list[str]:
    channels: list[str] = []
    if ENABLE_NAPCAT_QQ:
        channels.append("napcat")
    if ENABLE_QQ_OFFICIAL_BOT and has_bots():
        channels.append("official")
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

    if ENABLE_NAPCAT_QQ:
        napcat_ok = await send_qq_message(member["target_group"], message_chain)
        if not napcat_ok:
            log_all("⚠️ NapCat QQ 推送失败", is_error=True)

    if ENABLE_QQ_OFFICIAL_BOT:
        bots = get_bots()
        for bot in bots:
            ok = await bot.send_message_chain(member, message_chain)
            if not ok:
                log_all(f"⚠️ 官方 QQ Bot [{bot.name}] 推送失败", is_error=True)

    if ENABLE_NAPCAT_QQ:
        return napcat_ok
    return True


async def send_alert_message(target_group: int, text: str) -> bool:
    """发送系统警报。NapCat 发到原群，官方 Bot 发到配置的个人 openid。"""
    channels = enabled_channels()
    if not channels:
        log_all(f"⏸️ QQ 推送通道均未启用，警报未发送: {text}", is_error=True)
        return False

    ok = True
    if ENABLE_NAPCAT_QQ:
        ok = await send_qq_message(target_group, [{"type": "text", "data": {"text": text}}]) and ok
    if ENABLE_QQ_OFFICIAL_BOT:
        bots = get_bots()
        for bot in bots:
            if not await bot.send_text(text):
                ok = False
    return ok
