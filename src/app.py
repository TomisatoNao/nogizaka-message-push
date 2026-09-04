# ============================================================
# app.py — 程序入口：初始化所有模块、驱动主轮询循环
# ============================================================
import asyncio
import os
import sys
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
    _alert_group_for_account,
    _build_daily_summary,
    _calc_sleep_seconds,
    _command_listeners,
    _daily_summary_loop,
    _dir_size,
    _get_jst_now,
    _has_configured_workload,
    _health_check,
    _init_accounts,
    _initial_admin_banner,
    _install_stop_handlers,
    _is_pid_running,
    _is_python_process,
    _kill_pid,
    _last_command_status,
    _message_cycle_summary,
    _message_monitor_enabled,
    _next_interval,
    _on_config_reload,
    _required_account_ids,
    _run_cycle,
    _run_loop,
    _send_summary_with_retry,
    _stop_requested,
    _storage_line,
    _sync_command_listeners,
    _to_jst_date,
    _valid_monitors,
    _wait_or_trigger,
    get_command_listeners,
    get_main_loop,
    handle_openid_action,
    handle_test_push,
    set_main_loop,
)
from src.logger import init_loggers, log_all
from src.platforms import napcat, qq_official, tgbot
from src.platforms.qq_official import health_check as qq_official_health_check
from src.social.manager import start_social_service, stop_social_service
from src.webui import start_webui

# ---- 博客状态 ----
_blog_db: object = None
_blog_client: httpx.AsyncClient | None = None


# 指令监听主事件循环引用
_main_loop: asyncio.AbstractEventLoop | None = None


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
        """网页「测试推送」回调（HTTP 线程调用）：把发送协程调度到主事件循环执行。"""
        return handle_test_push(channel, target, text, loop)

    def _openid_action(action: str, app_id: str, secret: str, mode: str = "user") -> tuple[bool, str]:
        """网页端的 openid 监听控制（HTTP 线程调用，调度到主事件循环）。"""
        return handle_openid_action(action, app_id, secret, mode, loop)

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
    set_main_loop(loop)
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
        set_main_loop(None)
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
    "_command_listeners",
    "_last_command_status",
    "set_main_loop",
    "get_main_loop",
    "get_command_listeners",
    "handle_test_push",
    "handle_openid_action",
    "get_token_remaining_seconds",
    "refresh_mobile_token",
    "refresh_token",
    "qq_official_health_check",
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
