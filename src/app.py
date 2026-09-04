# ============================================================
# app.py — 程序入口：初始化所有模块、驱动主轮询循环
# ============================================================
import asyncio
import os
import signal
import sys
import traceback
from pathlib import Path

import httpx

import config.config as cfg
from config.credentials import (
    get_token_remaining_seconds,
    initialize as init_credentials,
    load_all_accounts,
    refresh_mobile_token,
    refresh_token,
)
from config.watcher import start_watcher
from src import archive, blog_fetcher, fetcher, health, tagger, translator
from src.app_modules import (
    DISK_WARN_BYTES,
    PID_FILE,
    STOP_FILE,
    SUMMARY_MAX_ATTEMPTS,
    SUMMARY_RETRY_SECONDS,
    _MemberCycleResult,
    _acquire_instance_lock,
    _build_daily_summary,
    _calc_sleep_seconds,
    _daily_summary_loop,
    _dir_size,
    _get_jst_now,
    _is_pid_running,
    _is_python_process,
    _kill_pid,
    _message_cycle_summary,
    _next_interval,
    _run_cycle,
    _run_loop,
    _send_summary_with_retry,
    _stop_requested,
    _storage_line,
    _to_jst_date,
    _wait_or_trigger,
)
from src.logger import init_loggers, log_all
from src.platforms import napcat, qq_official, tgbot
from src.platforms.qq_official import health_check as qq_official_health_check
from src.social.manager import start_social_service, stop_social_service
from src.webui import start_webui

# ---- 博客状态 ----
_blog_db: object = None
_blog_client: httpx.AsyncClient | None = None


# ──────────────────────────────────────────────
# 改进 3：启动健康检查
# ──────────────────────────────────────────────
def _message_monitor_enabled() -> bool:
    """读取 Message 监控开关；缺失时采用首次运行的安全默认值 False。"""
    return bool(getattr(cfg, "MESSAGE_MONITOR_ENABLED", False))


def _valid_monitors() -> list[dict]:
    """返回具备账号与成员 ID 的有效 Message 监控项。"""
    return [
        member for member in getattr(cfg, "MONITOR_LIST", [])
        if isinstance(member, dict) and member.get("account_id") and member.get("m_id")
    ]


def _required_account_ids() -> list[str]:
    """返回当前有效监控项真正需要的账号，不把账号池预设当成已启用任务。"""
    return sorted({str(member["account_id"]).strip() for member in _valid_monitors()})


def _has_configured_workload() -> bool:
    """判断是否至少配置了一项会实际工作的监控任务。"""
    if _message_monitor_enabled() and _valid_monitors():
        return True
    blog_cfg = getattr(cfg, "BLOG_MONITOR", None) or {}
    if isinstance(blog_cfg, dict) and blog_cfg.get("enabled", False):
        return True
    platforms = getattr(cfg, "PLATFORMS", None) or {}
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


async def _health_check(qq_client: httpx.AsyncClient) -> bool:
    """
    启动时检查：
      1. 已启用的 QQ 推送通道是否可用
      2. MONITOR_LIST 里每个 account_id 是否都已加载凭证

    未配置项使用 INFO/WARN 表示等待配置；已启用服务的真实连通性故障
    使用 ERROR 记录，但都不阻止程序启动，让运维人员能区分“未配置”和“故障”。
    """
    all_ok = True
    setup_reasons: list[str] = []
    degraded_reasons: list[str] = []
    napcat_enabled = bool(getattr(cfg, "ENABLE_NAPCAT_QQ", False))
    official_enabled = bool(getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False))
    tg_enabled = bool(getattr(cfg, "ENABLE_TG_BOT", False))

    if not napcat_enabled and not official_enabled and not tg_enabled:
        log_all("🟡 推送通道尚未启用，等待配置（当前不会发送成员消息）")
        setup_reasons.append("尚未启用推送通道")

    # ── 检查 NapCat 连通性 ────────────────────────────────
    if napcat_enabled:
        status_url = cfg.QQ_BOT_API.rsplit("/", 1)[0] + "/get_status"
        try:
            resp = await qq_client.get(status_url)
            if resp.status_code == 200:
                log_all("🟢 NapCat QQ 连通正常")
                health.get_tracker().record_channel("napcat", True)
            else:
                log_all(f"🟡 NapCat QQ 返回 HTTP {resp.status_code}，可能运行异常", is_error=True)
                health.get_tracker().record_channel("napcat", False, f"HTTP {resp.status_code}")
                all_ok = False
                degraded_reasons.append(f"NapCat 返回 HTTP {resp.status_code}")
        except Exception as e:
            log_all(f"🔴 NapCat QQ 无法连接 ({type(e).__name__})，请确认 napcat/lagrange 已启动", is_error=True)
            health.get_tracker().record_channel("napcat", False, "无法连接")
            all_ok = False
            degraded_reasons.append("NapCat 无法连接")
    else:
        log_all("⏸️ NapCat QQ 推送未启用", is_debug=True)

    # ── 检查官方 QQ Bot 凭证 ──────────────────────────────
    if official_enabled:
        if not qq_official.has_bots():
            log_all("⚠️ QQ 官方 Bot 已启用，但尚未配置有效 Bot", is_warning=True)
            all_ok = False
            setup_reasons.append("QQ 官方 Bot 尚未配置")
        elif not await qq_official_health_check():
            all_ok = False
            degraded_reasons.append("QQ 官方 Bot 凭证检查失败")
    else:
        log_all("⏸️ 官方 QQ Bot 推送未启用", is_debug=True)

    # ── 检查 TG Bot 连通性 ──────────────────────────────────
    if tg_enabled:
        if not tgbot.get_configured_bots():
            log_all(
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
        log_all("⏸️ TG Bot 推送未启用", is_debug=True)

    # ── 检查每个成员至少有一个可用推送目标 ──────────────────
    # 官方 Bot 推送的是全局 TARGET_OPENID、不区分成员，所以只要有可用的官方 Bot，
    # 所有成员都有推送目标，无需再看 groups / tg。
    if _message_monitor_enabled():
        monitors = _valid_monitors()
        if not monitors:
            log_all("ℹ️ Message 监控已启用但尚未配置有效成员，等待配置")
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
                log_all(
                    "⚠️ 以下成员尚未配置有效推送目标（可稍后在管理端补充）："
                    f"{' · '.join(orphans)}",
                    is_warning=True,
                )
                all_ok = False
                setup_reasons.append(f"{len(orphans)} 个成员缺少推送目标")

            # ── 只检查当前有效监控项需要的账号凭证 ─────────────
            from config.credentials import ACCOUNT_CREDS, validate_account_cred
            needed = set(_required_account_ids())
            missing = needed - set(ACCOUNT_CREDS.keys())
            if missing:
                log_all(f"⚠️ 以下监控账号尚未配置凭证：{'、'.join(sorted(missing))}", is_warning=True)
                all_ok = False
                setup_reasons.append(f"{len(missing)} 个监控账号缺少凭证")

            invalid = []
            for acc_id in sorted(needed - missing):
                ok, reason = validate_account_cred(acc_id)
                if not ok:
                    invalid.append(f"{acc_id}（{reason}）")

            if invalid:
                log_all(f"⚠️ 以下监控账号凭证尚未就绪：{'；'.join(invalid)}", is_warning=True)
                all_ok = False
                setup_reasons.append(f"{len(invalid)} 个监控账号凭证待完善")
            elif not missing:
                log_all(f"🟢 监控账号凭证完整（{len(needed)} 个账号）")
                for acc_id in sorted(needed):
                    remaining = get_token_remaining_seconds(acc_id)
                    if remaining is not None:
                        health.get_tracker().record_token(acc_id, max(0, remaining))
    else:
        log_all("ℹ️ Message 监控尚未启用，跳过账号握手、成员目标和凭证检查")
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
    log_all(
        f"🚦 启动状态：{startup_state}"
        + (f"（{'；'.join(startup_reasons)}）" if startup_reasons else ""),
        is_warning=startup_state == "DEGRADED",
    )

    return all_ok


def _alert_group_for_account(acc_id: str) -> int:
    """该账号的告警目标群号。
    账号下所有成员都没配 QQ 群（纯 TG 推送）时返回 0，告警将只走 TG / 官方 Bot。"""
    for m in cfg.MONITOR_LIST:
        if m["account_id"] == acc_id and m.get("target_groups"):
            return m["target_groups"][0]
    return 0


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    """注册 SIGTERM / SIGINT → 优雅停止。

    docker stop / systemd 发送的是 SIGTERM，此前只处理 KeyboardInterrupt，
    容器停止时 finally 里的 observer / client 清理完全不会执行。
    Windows 的事件循环不支持 add_signal_handler，退回 signal.signal
    （其回调运行在主线程信号上下文，需 call_soon_threadsafe 交回事件循环）。"""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop_event.set))
            except (ValueError, OSError):
                pass  # 非主线程等场景注册失败：保底仍有 KeyboardInterrupt


async def _init_accounts() -> None:
    """启动时为当前 Message 监控引用的账号并发执行初始 Token 刷新与握手。
    - mobile 账号：使用 refresh_token 换取初始 access_token
    - web 账号：若 Token 临期或需要握手，主动调用 refresh_token 获取并持久化 Set-Cookie
    若已有充足有效 Token 则跳过，避免第一轮抓取浪费在 401 上。"""
    if not _message_monitor_enabled():
        log_all("ℹ️ Message 监控尚未启用，跳过账号初始握手", is_debug=True)
        return

    needed = _required_account_ids()
    if not needed:
        log_all("ℹ️ Message 监控已启用但尚未配置有效成员，跳过账号初始握手")
        return

    async def _init_one(acc_id: str, acc_cfg: dict) -> None:
        is_mobile = acc_cfg.get("auth_method") == "mobile"
        remaining = get_token_remaining_seconds(acc_id)
        if remaining is not None and remaining > 60:
            log_all(f"🔑 账号 {acc_id} Token 有效（剩余 {int(remaining)}s），跳过初始化")
            return
        target_group = _alert_group_for_account(acc_id)
        if is_mobile:
            log_all(f"🔑 移动端账号 {acc_id} 执行初始 Token 刷新...")
            await refresh_mobile_token(acc_id, target_group)
        else:
            log_all(f"🔑 Web 账号 {acc_id} 执行初始 Token 刷新与握手...")
            await refresh_token(acc_id, target_group)

    tasks = []
    for acc_id in needed:
        acc_cfg = getattr(cfg, "ACCOUNTS", {}).get(acc_id)
        if not isinstance(acc_cfg, dict):
            log_all(f"⚠️ 监控账号 {acc_id} 未定义，跳过初始握手", is_warning=True)
            continue
        tasks.append(_init_one(acc_id, acc_cfg))
    if tasks:
        await asyncio.gather(*tasks)


# 官方 Bot 指令监听：app_id → (client_secret, 任务)。
# 由 _sync_command_listeners() 按当前配置增删，启动时和每次热重载后都会调一次，
# 所以在管理端加 Bot / 删 Bot / 开关指令开关都不用重启进程。
_command_listeners: dict[str, tuple[str, asyncio.Task]] = {}
_main_loop: asyncio.AbstractEventLoop | None = None
_last_command_status: str = ""


def _sync_command_listeners() -> None:
    """按当前配置增删官方 Bot 指令监听任务（必须在事件循环里调用）。

    不依赖 qq_official 推送通道：只想用 Bot 查信息、不想让它推消息也是合理配置，
    监听只需要 Bot 自己的 app_id + client_secret。
    """
    global _last_command_status
    from src import qq_commands
    from src.qq_openid import listen_forever

    enabled = getattr(cfg, "QQ_COMMANDS_ENABLED", False)
    desired: dict[str, str] = {}
    bot_names: dict[str, str] = {}
    if enabled:
        for b in cfg.QQ_OFFICIAL_BOTS:
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
        log_all(status, is_error=is_error)
    _last_command_status = status


def _on_config_reload(success: bool) -> None:
    """config.json 热重载后的补偿动作（由 watchdog 线程调用）。

    新增账号的凭证需要重新加载 —— load_all_accounts() 幂等，已加载的账号自动跳过。
    注意：`.env` 只在进程启动时读取一次，新增或修改 `.env` 里的凭证仍需重启。
    """
    if not success:
        return
    try:
        load_all_accounts()
    except Exception:
        log_all(f"🚨 热重载后加载账号凭证失败:\n{traceback.format_exc()}", is_error=True)
        
    try:
        from src.platforms import qq_official, tgbot
        qq_official.reload()
        tgbot.initialize()
    except Exception as e:
        log_all(f"⚠️ 热重载 Bot 失败: {e}", is_error=True)

    try:
        from src.social.manager import reload_social_service
        reload_social_service()
    except Exception as e:
        log_all(f"⚠️ 热重载社媒监控配置失败: {e}", is_error=True)

    # 指令监听要跟着新配置走，否则在管理端新加的 Bot 得等到下次重启才会上线
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(_sync_command_listeners)


async def main() -> None:
    # 1. 基础设施
    init_loggers()
    _acquire_instance_lock()
    log_all("🌸 坂道联合监控系统已启动")
    # 上次的停止信号不该影响本次启动
    try:
        STOP_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    load_all_accounts()
    health.initialize(
        summary_interval=cfg.HEALTH_SUMMARY_INTERVAL,
        error_buffer=cfg.HEALTH_ERROR_BUFFER,
        token_warn_seconds=cfg.HEALTH_TOKEN_WARN_SECONDS,
    )

    # 首次运行自动生成 admin 管理员并输出初始密码
    from src import auth
    created, admin_user, admin_pw = auth.ensure_initial_admin()
    if created:
        banner = _initial_admin_banner(
            admin_user,
            admin_pw,
            int(getattr(cfg, "WEB_ADMIN_PORT", 46046)),
        )
        print(banner, flush=True)
        log_all("🔑 系统首次运行：已初始化创建 admin 账号（初始密码已输出至控制台）")

    proxy_url = getattr(cfg, "PROXY", "") or None
    if proxy_url:
        log_all(f"🌐 已配置网络代理: {proxy_url}")

    # 2. 创建普通请求与认证专用 HTTP 客户端。
    #    认证续期使用独立连接池，避免媒体/翻译慢请求占满普通池；
    #    credentials 内部还会用 TOKEN_REFRESH_CONCURRENCY 限制续期并发。
    http_client = httpx.AsyncClient(
        timeout=20,
        proxy=proxy_url,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    try:
        refresh_concurrency = max(1, int(getattr(cfg, "TOKEN_REFRESH_CONCURRENCY", 2)))
    except (TypeError, ValueError):
        refresh_concurrency = 2
    auth_http_client = httpx.AsyncClient(
        timeout=15,
        proxy=proxy_url,
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=max(2, refresh_concurrency * 2),
            max_keepalive_connections=refresh_concurrency,
        ),
    )
    qq_client = httpx.AsyncClient(
        timeout=15,
        transport=httpx.AsyncHTTPTransport(retries=0, http2=False, trust_env=False),
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
    )
    semaphore = asyncio.Semaphore(cfg.HTTP_SEMAPHORE_LIMIT)

    # 3. 注入依赖 & 在事件循环内创建各模块的锁
    #    translator / archive / tagger 复用普通池；credentials 使用认证专用池。
    init_credentials(http_client, auth_client=auth_http_client)
    translator.initialize(http_client)
    archive.initialize(http_client)
    # 自动检查并纠正历史归档中因旧版 UTC 导致的跨月错位数据（全自动无损自愈）
    try:
        archive.realign_archive_timezones()
    except Exception as e:
        log_all(f"⚠️ 历史归档时区自动自愈异常: {e}", is_debug=True)
    tagger.initialize(http_client)
    tgbot.initialize()
    napcat.initialize(qq_client)
    qq_official.initialize(qq_client)
    fetcher.initialize(http_client, semaphore)

    # 博客数据库初始化
    global _blog_db
    _blog_db = blog_fetcher.init_blog_db()

    # 博客 http 客户端
    global _blog_client
    _blog_client = httpx.AsyncClient(timeout=30, proxy=proxy_url, follow_redirects=True)

    # 博客长图渲染引擎检测
    try:
        from src.blog_card_renderer import is_playwright_available
        if is_playwright_available():
            log_all("🎨 博客长图卡片渲染引擎已就绪 (Playwright Chromium)")
        else:
            log_all("💡 博客长图卡片渲染引擎未就绪（Playwright 未安装，博客将以标准图文模式推送）")
    except Exception as e:
        log_all(f"⚠️ 博客长图卡片环境检测异常: {e}", is_debug=True)

    # 4. 账号初始 Token 刷新与自动握手
    #    必须放在通道注入（步骤 3）之后：刷新失败时 refresh_token 会走
    #    send_alert_message，此时 napcat._client / tgbot._bot 必须已就绪，否则告警静默丢失。
    await _init_accounts()

    # 4.5 启动成员订阅状态同步（后台异步更新 SQLite 订阅状态缓存）。
    # 仅在 Message 监控确实有有效成员时执行；首次运行的账号预设没有凭证，
    # 不应被当成已启用任务逐个请求并刷出“凭证不可用”错误。
    if _message_monitor_enabled() and _valid_monitors():
        try:
            from src.member_directory import sync_all_accounts_subscriptions
            asyncio.create_task(
                sync_all_accounts_subscriptions(
                    http_client, account_ids=_required_account_ids(),
                )
            )
        except Exception as e:
            log_all(f"⚠️ 初始同步账号订阅状态异常: {e}", is_warning=True)
    else:
        log_all("ℹ️ 未满足成员订阅同步条件，跳过初始订阅同步", is_debug=True)

    # 5. 启动健康检查（改进 3）
    await _health_check(qq_client)

    # 6. 可选启动 config.json 文件监控（watchdog 未安装时返回 None）
    config_path = Path(__file__).resolve().parent.parent / "config" / "config.json"
    observer = start_watcher(config_path, on_reload=_on_config_reload)

    # 6.5 启动社交媒体监控守护（X / Instagram / TikTok / TikTok Live）
    try:
        import json5
        with open(config_path, "r", encoding="utf-8") as f:
            raw_cfg = json5.load(f)
        start_social_service(raw_cfg)
    except Exception as e:
        log_all(f"⚠️ 启动社交媒体监控失败: {e}", is_error=True)

    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)

    # 7. 可选启动网页管理端（config.json 的 web_admin.enabled 控制）
    #    重启回调运行在 HTTP 处理线程：走优雅停机流程，清理完毕后 execv 自替换
    loop = asyncio.get_running_loop()

    # Windows 平台 IOCP 异步操作中止容错（防止 WinError 995 异常中断事件循环）
    def _loop_exception_handler(current_loop, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, OSError)) and getattr(exc, "winerror", None) == 995:
            return
        if isinstance(exc, asyncio.InvalidStateError) and sys.platform == "win32":
            return
        current_loop.default_exception_handler(context)

    loop.set_exception_handler(_loop_exception_handler)

    restart_requested = False
    poll_event = asyncio.Event()

    def _request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        loop.call_soon_threadsafe(stop_event.set)

    def _request_poll() -> None:
        loop.call_soon_threadsafe(poll_event.set)

    def _request_test_push(channel: str, target: str, text: str) -> tuple[bool, str]:
        """网页「测试推送」回调（HTTP 线程调用）：把发送协程调度到主事件循环执行。
        target 参数现在是:
        - TG: bot_name
        - NapCat: group_id
        - Official: "bot_name|mode" (mode = group / private)
        """
        try:
            if channel == "tg":
                if not cfg.ENABLE_TG_BOT:
                    return False, "TG 通道未启用"
                bots = tgbot.get_configured_bots()
                bot = next((b for b in bots if b.name == target), None)
                if not bot:
                    return False, f"找不到指定的 TG Bot: {target}"
                coro = bot.send_text(text)
            elif channel == "napcat":
                if not cfg.ENABLE_NAPCAT_QQ:
                    return False, "NapCat 通道未启用"
                chain = [{"type": "text", "data": {"text": text}}]
                coro = napcat.send_qq_message(int(target), chain)
            elif channel in ("qq_official", "official"):
                if not cfg.ENABLE_QQ_OFFICIAL_BOT:
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
        # 测试推送的成败也计入通道统计，状态页才不会与事实矛盾
        health.get_tracker().record_channel(channel, ok, err or None)
        return ok, err

    def _openid_action(action: str, app_id: str, secret: str, mode: str = "user") -> tuple[bool, str]:
        """网页端的 openid 监听控制（HTTP 线程调用，调度到主事件循环）。
        mode: 'user'（单聊）| 'group'（群聊）。"""
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

    webui_server = (
        start_webui(on_reload=_on_config_reload, on_restart=_request_restart,
                    on_poll=_request_poll, on_test_push=_request_test_push,
                    on_openid=_openid_action, prewarm_home=True)
        if cfg.WEB_ADMIN_ENABLED else None
    )

    summary_task = (
        asyncio.create_task(_daily_summary_loop()) if cfg.DAILY_SUMMARY_ENABLED else None
    )

    # 异步非阻塞预热成员与博客作者头像
    async def _bg_avatar_warmup():
        try:
            from src.avatar_manager import sync_all_avatars
            await sync_all_avatars(force=False)
        except Exception:  # nosec B110
            pass

    asyncio.create_task(_bg_avatar_warmup())

    # 官方 Bot 指令监听（私聊 Bot 查状态 / 归档）
    global _main_loop
    _main_loop = loop
    _sync_command_listeners()

    try:
        loop_task = asyncio.create_task(_run_loop(http_client, poll_event, stop_event))
        stop_task = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            {loop_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            log_all("🛑 收到停止信号，安全退出中...")
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
        else:
            stop_task.cancel()
            loop_task.result()   # _run_loop 不会正常返回；到这里说明它抛了异常，向上传播
    except KeyboardInterrupt:
        log_all("🛑 安全退出中...")
    finally:
        if summary_task is not None:
            summary_task.cancel()
            await asyncio.gather(summary_task, return_exceptions=True)
        _main_loop = None
        listener_tasks = [task for _, task in _command_listeners.values()]
        _command_listeners.clear()
        for task in listener_tasks:
            task.cancel()
        if listener_tasks:
            await asyncio.gather(*listener_tasks, return_exceptions=True)
        if webui_server is not None:
            webui_server.shutdown()
            webui_server.server_close()
        if observer is not None:
            observer.stop()
            observer.join(timeout=2.0)
        stop_social_service()
        try:
            await tagger.wait_pending(timeout=30)
            await archive.wait_pending(timeout=30)   # 归档后台任务收尾（媒体下载中途别掐）
        except Exception:  # nosec B110
            pass
        try:
            await asyncio.gather(
                http_client.aclose(), auth_http_client.aclose(), qq_client.aclose(),
                return_exceptions=True,
            )
        except Exception:  # nosec B110
            pass
        if _blog_client is not None:
            try:
                await _blog_client.aclose()
            except Exception:  # nosec B110
                pass
        log_all("✅ 资源清理完毕")

    if restart_requested:
        # 进程自替换：拉起全新进程，.env / 模块状态全部重新加载
        log_all("🔁 正在重启主程序...")
        if sys.platform == "win32":
            import subprocess  # nosec B404
            subprocess.Popen([sys.executable] + sys.argv, close_fds=True)  # nosec B603
            os._exit(0)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)  # nosec B606


__all__ = [
    # 核心主入口与生命周期
    "main",
    "_blog_db",
    "_blog_client",
    "_message_monitor_enabled",
    "_valid_monitors",
    "_required_account_ids",
    "_has_configured_workload",
    "_initial_admin_banner",
    "_health_check",
    "_alert_group_for_account",
    "_install_stop_handlers",
    "_init_accounts",
    "_sync_command_listeners",
    "_on_config_reload",
    # 子模块 re-export（向下兼容外部及单测测试钩子）
    # process_lock
    "PID_FILE",
    "STOP_FILE",
    "_is_pid_running",
    "_is_python_process",
    "_kill_pid",
    "_acquire_instance_lock",
    "_stop_requested",
    # daily_summary
    "DISK_WARN_BYTES",
    "SUMMARY_MAX_ATTEMPTS",
    "SUMMARY_RETRY_SECONDS",
    "_get_jst_now",
    "_to_jst_date",
    "_dir_size",
    "_storage_line",
    "_build_daily_summary",
    "_send_summary_with_retry",
    "_daily_summary_loop",
    # message_worker
    "_MemberCycleResult",
    "_message_cycle_summary",
    "_calc_sleep_seconds",
    "_next_interval",
    "_wait_or_trigger",
    "_run_cycle",
    "_run_loop",
]


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
