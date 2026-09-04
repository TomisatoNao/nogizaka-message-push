"""
src/app_modules/bootstrap.py — 系统启动自检、账号握手、指令监听与生命周期初始化
"""

from __future__ import annotations

import asyncio
import signal
import sys
import traceback
from typing import TYPE_CHECKING

import httpx

import config.config as cfg
from config.credentials import (
    ACCOUNT_CREDS,
    get_token_remaining_seconds,
    load_all_accounts,
    refresh_mobile_token,
    refresh_token,
    validate_account_cred,
)
from src import health
from src.logger import log_all
from src.platforms import napcat, qq_official, tgbot

if TYPE_CHECKING:
    pass


def _app_attr(name: str, default=None):
    """动态获取 src.app 模块上的属性（优先尊重外部单测 monkeypatch）。"""
    app_mod = sys.modules.get("src.app")
    if app_mod and hasattr(app_mod, name):
        return getattr(app_mod, name)
    return default


def _log(content: str, **kwargs) -> None:
    fn = _app_attr("log_all", log_all)
    fn(content, **kwargs)


def _get_cfg():
    return _app_attr("cfg", cfg)


def _get_token_remaining(acc_id: str) -> float | None:
    fn = _app_attr("get_token_remaining_seconds", get_token_remaining_seconds)
    return fn(acc_id)


# ──────────────────────────────────────────────
# 启动监控项检查与工作负载判定
# ──────────────────────────────────────────────
def _message_monitor_enabled() -> bool:
    """读取 Message 监控开关；缺失时采用首次运行的安全默认值 False。"""
    c = _get_cfg()
    return bool(getattr(c, "MESSAGE_MONITOR_ENABLED", False))


def _valid_monitors() -> list[dict]:
    """返回具备账号与成员 ID 的有效 Message 监控项。"""
    c = _get_cfg()
    return [
        member for member in getattr(c, "MONITOR_LIST", [])
        if isinstance(member, dict) and member.get("account_id") and member.get("m_id")
    ]


def _required_account_ids() -> list[str]:
    """返回当前有效监控项真正需要的账号，不把账号池预设当成已启用任务。"""
    return sorted({str(member["account_id"]).strip() for member in _valid_monitors()})


def _has_configured_workload() -> bool:
    """判断是否至少配置了一项会实际工作的监控任务。"""
    if _message_monitor_enabled() and _valid_monitors():
        return True
    c = _get_cfg()
    blog_cfg = getattr(c, "BLOG_MONITOR", None) or {}
    if isinstance(blog_cfg, dict) and blog_cfg.get("enabled", False):
        return True
    platforms = getattr(c, "PLATFORMS", None) or {}
    return isinstance(platforms, dict) and any(
        isinstance(item, dict) and item.get("enabled", False)
        for item in platforms.values()
    )


def _initial_admin_banner(admin_user: str, admin_pw: str, web_port: int) -> str:
    """生成一次性的初始管理员提示，避免隐式字符串拼接造成重复输出。"""
    return "\n".join([
        "",
        "=" * 70,
        "🔑 系统首次运行：已为您自动创建初始管理员账号！",
        f"   • 用户名:   {admin_user}",
        f"   • 初始密码: {admin_pw}",
        f"   • Web 管理端: http://127.0.0.1:{web_port}/",
        "",
        "⚠️ 请妥善保存初始密码！若遗忘，可在终端执行：",
        f"   python tools/manage_users.py passwd {admin_user}",
        "=" * 70,
        "",
    ])


def _alert_group_for_account(acc_id: str) -> int:
    """该账号的告警目标群号。
    账号下所有成员都没配 QQ 群（纯 TG 推送）时返回 0，告警将只走 TG / 官方 Bot。"""
    c = _get_cfg()
    for m in getattr(c, "MONITOR_LIST", []):
        if m.get("account_id") == acc_id and m.get("target_groups"):
            return m["target_groups"][0]
    return 0


# ──────────────────────────────────────────────
# 启动健康检查
# ──────────────────────────────────────────────
async def _health_check(qq_client: httpx.AsyncClient) -> bool:
    """
    启动时检查：
      1. 已启用的 QQ 推送通道是否可用
      2. MONITOR_LIST 里每个 account_id 是否都已加载凭证

    未配置项使用 INFO/WARN 表示等待配置；已启用服务的真实连通性故障
    使用 ERROR 记录，但都不阻止程序启动，让运维人员能区分“未配置”和“故障”。
    """
    c = _get_cfg()
    all_ok = True
    setup_reasons: list[str] = []
    degraded_reasons: list[str] = []
    napcat_enabled = bool(getattr(c, "ENABLE_NAPCAT_QQ", False))
    official_enabled = bool(getattr(c, "ENABLE_QQ_OFFICIAL_BOT", False))
    tg_enabled = bool(getattr(c, "ENABLE_TG_BOT", False))

    if not napcat_enabled and not official_enabled and not tg_enabled:
        _log("🟡 推送通道尚未启用，等待配置（当前不会发送成员消息）")
        setup_reasons.append("尚未启用推送通道")

    # ── 检查 NapCat 连通性 ────────────────────────────────
    if napcat_enabled:
        bot_api = getattr(c, "QQ_BOT_API", "http://127.0.0.1:3000/send_group_msg")
        status_url = bot_api.rsplit("/", 1)[0] + "/get_status"
        try:
            resp = await qq_client.get(status_url)
            if resp.status_code == 200:
                _log("🟢 NapCat QQ 连通正常")
                health.get_tracker().record_channel("napcat", True)
            else:
                _log(f"🟡 NapCat QQ 返回 HTTP {resp.status_code}，可能运行异常", is_error=True)
                health.get_tracker().record_channel("napcat", False, f"HTTP {resp.status_code}")
                all_ok = False
                degraded_reasons.append(f"NapCat 返回 HTTP {resp.status_code}")
        except Exception as e:
            _log(f"🔴 NapCat QQ 无法连接 ({type(e).__name__})，请确认 napcat/lagrange 已启动", is_error=True)
            health.get_tracker().record_channel("napcat", False, "无法连接")
            all_ok = False
            degraded_reasons.append("NapCat 无法连接")
    else:
        _log("⏸️ NapCat QQ 推送未启用", is_debug=True)

    # ── 检查官方 QQ Bot 凭证 ──────────────────────────────
    if official_enabled:
        if not qq_official.has_bots():
            _log("⚠️ QQ 官方 Bot 已启用，但尚未配置有效 Bot", is_warning=True)
            all_ok = False
            setup_reasons.append("QQ 官方 Bot 尚未配置")
        elif not await qq_official.health_check():
            all_ok = False
            degraded_reasons.append("QQ 官方 Bot 凭证检查失败")
    else:
        _log("⏸️ 官方 QQ Bot 推送未启用", is_debug=True)

    # ── 检查 TG Bot 连通性 ──────────────────────────────────
    if tg_enabled:
        if not tgbot.get_configured_bots():
            _log(
                "⚠️ Telegram Bot 已启用，但没有配置可用的专属 Token，"
                "请在每个 Bot 的环境变量中填写 <Bot名称大写>_TOKEN",
                is_warning=True,
            )
            all_ok = False
            setup_reasons.append("Telegram Bot 专属凭证未配置")
        elif await tgbot.health_check():
            health.get_tracker().record_channel("tg", True)
        else:
            health.get_tracker().record_channel("tg", False, "无法连接")
            all_ok = False
            degraded_reasons.append("Telegram Bot 无法连接")
    else:
        _log("⏸️ TG Bot 推送未启用", is_debug=True)

    # ── 检查每个成员至少有一个可用推送目标 ──────────────────
    if _message_monitor_enabled():
        monitors = _valid_monitors()
        if not monitors:
            _log("ℹ️ Message 监控已启用但尚未配置有效成员，等待配置")
            setup_reasons.append("尚未配置监控成员")
        else:
            official_covers_all = official_enabled and qq_official.has_bots()
            tg_ready = tg_enabled and bool(tgbot.get_configured_bots())
            orphans = [] if official_covers_all else [
                str(member.get("m_name") or member.get("m_id")) for member in monitors
                if not (napcat_enabled and member.get("target_groups"))
                and not (tg_ready and (member.get("tg_chat_id") or "").strip())
            ]
            if orphans:
                _log(
                    "⚠️ 以下成员尚未配置有效推送目标（可稍后在管理端补充）："
                    f"{' · '.join(orphans)}",
                    is_warning=True,
                )
                all_ok = False
                setup_reasons.append(f"{len(orphans)} 个成员缺少推送目标")

            # ── 只检查当前有效监控项需要的账号凭证 ─────────────
            needed = set(_required_account_ids())
            missing = needed - set(ACCOUNT_CREDS.keys())
            if missing:
                _log(f"⚠️ 以下监控账号尚未配置凭证：{'、'.join(sorted(missing))}", is_warning=True)
                all_ok = False
                setup_reasons.append(f"{len(missing)} 个监控账号缺少凭证")

            invalid = []
            for acc_id in sorted(needed - missing):
                ok, reason = validate_account_cred(acc_id)
                if not ok:
                    invalid.append(f"{acc_id}（{reason}）")

            if invalid:
                _log(f"⚠️ 以下监控账号凭证尚未就绪：{'；'.join(invalid)}", is_warning=True)
                all_ok = False
                setup_reasons.append(f"{len(invalid)} 个监控账号凭证待完善")
            elif not missing:
                _log(f"🟢 监控账号凭证完整（{len(needed)} 个账号）")
                for acc_id in sorted(needed):
                    remaining = _get_token_remaining(acc_id)
                    if remaining is not None:
                        health.get_tracker().record_token(acc_id, max(0, remaining))
    else:
        _log("ℹ️ Message 监控尚未启用，跳过账号握手、成员目标和凭证检查")
        if not _has_configured_workload():
            setup_reasons.append("尚未启用监控任务")

    if degraded_reasons:
        startup_state = "DEGRADED"
        startup_reasons = degraded_reasons + setup_reasons
    elif setup_reasons:
        startup_state = "SETUP_REQUIRED"
        startup_reasons = setup_reasons
    else:
        startup_state = "READY"
        startup_reasons = []
    health.get_tracker().set_startup_state(startup_state, startup_reasons)
    _log(
        f"🚦 启动状态：{startup_state}"
        + (f"（{'；'.join(startup_reasons)}）" if startup_reasons else ""),
        is_warning=startup_state == "DEGRADED",
    )

    return all_ok


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    """注册 SIGTERM / SIGINT → 优雅停止。"""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop_event.set))
            except (ValueError, OSError):
                pass


async def _init_accounts() -> None:
    """启动时为当前 Message 监控引用的账号并发执行初始 Token 刷新与握手。"""
    if not _message_monitor_enabled():
        _log("ℹ️ Message 监控尚未启用，跳过账号初始握手", is_debug=True)
        return

    needed = _required_account_ids()
    if not needed:
        _log("ℹ️ Message 监控已启用但尚未配置有效成员，跳过账号初始握手")
        return

    c = _get_cfg()

    async def _init_one(acc_id: str, acc_cfg: dict) -> None:
        is_mobile = acc_cfg.get("auth_method") == "mobile"
        remaining = _get_token_remaining(acc_id)
        if remaining is not None and remaining > 60:
            _log(f"🔑 账号 {acc_id} Token 有效（剩余 {int(remaining)}s），跳过初始化")
            return
        target_group = _alert_group_for_account(acc_id)
        if is_mobile:
            _log(f"🔑 移动端账号 {acc_id} 执行初始 Token 刷新...")
            await refresh_mobile_token(acc_id, target_group)
        else:
            _log(f"🔑 Web 账号 {acc_id} 执行初始 Token 刷新与握手...")
            await refresh_token(acc_id, target_group)

    tasks = []
    for acc_id in needed:
        acc_cfg = getattr(c, "ACCOUNTS", {}).get(acc_id)
        if not isinstance(acc_cfg, dict):
            _log(f"⚠️ 监控账号 {acc_id} 未定义，跳过初始握手", is_warning=True)
            continue
        tasks.append(_init_one(acc_id, acc_cfg))
    if tasks:
        await asyncio.gather(*tasks)


# ──────────────────────────────────────────────
# 官方 Bot 指令监听与配置热重载
# ──────────────────────────────────────────────
_command_listeners: dict[str, tuple[str, asyncio.Task]] = {}
_main_loop: asyncio.AbstractEventLoop | None = None
_last_command_status: str = ""


def set_main_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _main_loop
    _main_loop = loop


def get_main_loop() -> asyncio.AbstractEventLoop | None:
    return _main_loop


def get_command_listeners() -> dict[str, tuple[str, asyncio.Task]]:
    return _command_listeners


def _sync_command_listeners() -> None:
    """按当前配置增删官方 Bot 指令监听任务（必须在事件循环里调用）。"""
    global _last_command_status
    from src import qq_commands
    from src.qq_openid import listen_forever

    c = _get_cfg()
    enabled = getattr(c, "QQ_COMMANDS_ENABLED", False)
    desired: dict[str, str] = {}
    bot_names: dict[str, str] = {}
    if enabled:
        for b in getattr(c, "QQ_OFFICIAL_BOTS", []):
            if b.get("app_id") and b.get("client_secret"):
                desired[b["app_id"]] = b["client_secret"]
                bot_names[b["app_id"]] = b.get("name") or b["app_id"]

    # 撤掉：已删除的 Bot、换了 secret 的 Bot、以及已经自行退出的任务
    for app_id, (secret, task) in list(_command_listeners.items()):
        if desired.get(app_id) != secret or task.done():
            task.cancel()
            del _command_listeners[app_id]

    started = [aid for aid in desired if aid not in _command_listeners]
    for app_id in started:
        _command_listeners[app_id] = (
            desired[app_id],
            asyncio.create_task(listen_forever(
                app_id, desired[app_id], qq_commands.handle, bot_name=bot_names.get(app_id, ""))),
        )

    if not enabled:
        status, is_error = "", False
    elif not desired:
        status, is_error = ("⚠️ Bot 指令已启用，但没有任何填好 App ID + Client Secret 的"
                            "官方 Bot，指令监听未启动"), True
    elif not qq_commands.allowed_senders():
        status, is_error = ("⚠️ Bot 指令已启用，但白名单为空（既无 target_openid 也无 "
                            "qq_commands.allow_openids），将不响应任何人的指令"), True
    else:
        status, is_error = f"🤖 官方 Bot 指令监听运行中（{len(desired)} 个 Bot）", False

    # 只在状态变化时写日志，避免每次热重载都刷一条重复的
    if status and status != _last_command_status:
        _log(status, is_error=is_error)
    _last_command_status = status


def _on_config_reload(success: bool) -> None:
    """config.json 热重载后的补偿动作（由 watchdog 线程调用）。"""
    if not success:
        return
    try:
        load_all_accounts()
    except Exception:
        _log(f"🚨 热重载后加载账号凭证失败:\n{traceback.format_exc()}", is_error=True)

    try:
        from src.platforms import qq_official, tgbot
        qq_official.reload()
        tgbot.initialize()
    except Exception as e:
        _log(f"⚠️ 热重载 Bot 失败: {e}", is_error=True)

    try:
        from src.social.manager import reload_social_service
        reload_social_service()
    except Exception as e:
        _log(f"⚠️ 热重载社媒监控配置失败: {e}", is_error=True)

    # 指令监听要跟着新配置走，否则在管理端新加的 Bot 得等到下次重启才会上线
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(_sync_command_listeners)


# ──────────────────────────────────────────────
# WebUI 回调处理（线程安全调度至主事件循环）
# ──────────────────────────────────────────────
def handle_test_push(channel: str, target: str, text: str, loop: asyncio.AbstractEventLoop) -> tuple[bool, str]:
    """网页「测试推送」回调（HTTP 线程调用）：把发送协程调度到主事件循环执行。"""
    c = _get_cfg()
    try:
        if channel == "tg":
            if not getattr(c, "ENABLE_TG_BOT", False):
                return False, "TG 通道未启用"
            bots = tgbot.get_configured_bots()
            bot = next((b for b in bots if b.name == target), None)
            if not bot:
                return False, f"找不到指定的 TG Bot: {target}"
            coro = bot.send_text(text)
        elif channel == "napcat":
            if not getattr(c, "ENABLE_NAPCAT_QQ", False):
                return False, "NapCat 通道未启用"
            chain = [{"type": "text", "data": {"text": text}}]
            coro = napcat.send_qq_message(int(target), chain)
        elif channel in ("qq_official", "official"):
            if not getattr(c, "ENABLE_QQ_OFFICIAL_BOT", False):
                return False, "QQ 官方 Bot 通道未启用"
            parts = target.split("|")
            if len(parts) != 2:
                return False, "无效的目标格式"
            bot_name, mode = parts[0], parts[1]
            bots = qq_official.get_configured_bots()
            bot = next((b for b in bots if b.name == bot_name), None)
            if not bot:
                return False, f"找不到指定的官方 Bot: {bot_name}"
            if mode == "group":
                if not bot.group_openid:
                    return False, "未配置群 openid"
                coro = bot.send_group_text(bot.group_openid, text)
            else:
                if not bot.target_openid:
                    return False, "未配置单聊 openid"
                coro = bot.send_private_text(bot.target_openid, text)
        else:
            return False, f"不支持的通道: {channel}"
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        ok = fut.result(timeout=45)
        err = "" if ok else "发送失败（详见日志）"
    except Exception as e:
        ok, err = False, f"{type(e).__name__}: {e}"
    health.get_tracker().record_channel(channel, ok, err or None)
    return ok, err


def handle_openid_action(action: str, app_id: str, secret: str, mode: str, loop: asyncio.AbstractEventLoop) -> tuple[bool, str]:
    """网页端的 openid 监听控制（HTTP 线程调用，调度到主事件循环）。"""
    from src import qq_openid

    async def _do():
        if action == "stop":
            qq_openid.stop_session()
            return True, "已停止监听"
        return qq_openid.start_session(app_id, secret, mode)

    try:
        return asyncio.run_coroutine_threadsafe(_do(), loop).result(timeout=20)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


__all__ = [
    "_message_monitor_enabled",
    "_valid_monitors",
    "_required_account_ids",
    "_has_configured_workload",
    "_initial_admin_banner",
    "_alert_group_for_account",
    "_health_check",
    "_install_stop_handlers",
    "_init_accounts",
    "_command_listeners",
    "_last_command_status",
    "set_main_loop",
    "get_main_loop",
    "get_command_listeners",
    "_sync_command_listeners",
    "_on_config_reload",
    "handle_test_push",
    "handle_openid_action",
]
