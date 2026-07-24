# ============================================================
# app.py — 程序入口：初始化所有模块、驱动主轮询循环
# ============================================================
import asyncio
import os
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

from src import fetcher
from src import translator
from src.platforms import bilibili
from src.platforms import napcat
from src.platforms import qq_official
from src.platforms import tgbot
from src import health
from src.platforms.qq_official import health_check as qq_official_health_check
import config.config as cfg
from config.credentials import (
    load_all_accounts, proactive_refresh_if_expiring,
    refresh_mobile_token, get_token_remaining_seconds,
)
from config.watcher import start_watcher
from src.logger import init_loggers, log_all
from src.utils import in_hour_range


# ──────────────────────────────────────────────
# 改进 3：启动健康检查
# ──────────────────────────────────────────────
async def _health_check(qq_client: httpx.AsyncClient) -> bool:
    """
    启动时检查：
      1. 已启用的 QQ 推送通道是否可用
      2. MONITOR_LIST 里每个 account_id 是否都已加载凭证

    任意一项失败都打印警告，但不阻止程序启动，
    让运维人员能第一时间看到问题所在。
    """
    all_ok = True

    if not cfg.ENABLE_NAPCAT_QQ and not cfg.ENABLE_QQ_OFFICIAL_BOT and not cfg.ENABLE_TG_BOT:
        log_all("🟡 所有推送通道均未启用：成员消息会被抓取并记录，但不会推送", is_error=True)

    # ── 检查 NapCat 连通性 ────────────────────────────────
    if cfg.ENABLE_NAPCAT_QQ:
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
        except Exception as e:
            log_all(f"🔴 NapCat QQ 无法连接 ({type(e).__name__})，请确认 napcat/lagrange 已启动", is_error=True)
            health.get_tracker().record_channel("napcat", False, "无法连接")
            all_ok = False
    else:
        log_all("⏸️ NapCat QQ 推送未启用")

    # ── 检查官方 QQ Bot 凭证 ──────────────────────────────
    if cfg.ENABLE_QQ_OFFICIAL_BOT:
        if not await qq_official_health_check():
            all_ok = False
    else:
        log_all("⏸️ 官方 QQ Bot 推送未启用")

    # ── 检查 TG Bot 连通性 ──────────────────────────────────
    if cfg.ENABLE_TG_BOT:
        if await tgbot.health_check():
            health.get_tracker().record_channel("tg", True)
        else:
            health.get_tracker().record_channel("tg", False, "无法连接")
            all_ok = False
    else:
        log_all("⏸️ TG Bot 推送未启用")

    # ── 检查所有账号凭证已加载且内容完整 ────────────────────
    from config.credentials import ACCOUNT_CREDS, validate_account_cred
    needed = {m["account_id"] for m in cfg.MONITOR_LIST}
    missing = needed - set(ACCOUNT_CREDS.keys())
    if missing:
        log_all(f"🔴 以下账号凭证缺失：{missing}", is_error=True)
        all_ok = False

    invalid = []
    for acc_id in sorted(needed - missing):
        ok, reason = validate_account_cred(acc_id)
        if not ok:
            invalid.append(f"{acc_id}（{reason}）")

    if invalid:
        log_all(f"🔴 以下账号凭证不完整：{'；'.join(invalid)}", is_error=True)
        all_ok = False
    elif not missing:
        log_all(f"🟢 账号凭证完整（{len(needed)} 个账号）")
        for acc_id in sorted(needed):
            remaining = get_token_remaining_seconds(acc_id)
            if remaining is not None:
                health.get_tracker().record_token(acc_id, max(0, remaining))

    return all_ok


def _get_jst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def _calc_sleep_seconds() -> int:
    """若当前在休眠时段内，返回距离休眠结束的秒数；否则返回 0。
    支持跨午夜休眠窗口（如 SLEEP_START=22, SLEEP_END=6）。"""
    jst = _get_jst_now()
    if in_hour_range(jst.hour, cfg.SLEEP_START_HOUR, cfg.SLEEP_END_HOUR):
        wake = jst.replace(hour=cfg.SLEEP_END_HOUR, minute=0, second=0, microsecond=0)
        if wake <= jst:
            wake += timedelta(days=1)
        return int((wake - jst).total_seconds())
    return 0


def _next_interval() -> tuple[int, str]:
    jst = _get_jst_now()
    if in_hour_range(jst.hour, cfg.NIGHT_START_HOUR, cfg.DAY_START_HOUR):
        base = random.randint(*cfg.NIGHT_INTERVAL)
        tag = "🌙 深夜低速"
    else:
        base = random.randint(*cfg.DAY_INTERVAL)
        tag = "☀️ 日间巡查"
    # ±10% 抖动，最低不低于 1s
    jitter = int(base * random.uniform(-0.1, 0.1))
    return max(1, base + jitter), tag


async def _run_loop(http_client: httpx.AsyncClient) -> None:
    while True:
        member_names = " · ".join(m["m_name"].replace(" ", "") for m in cfg.MONITOR_LIST)

        # 每个账号取第一个关联成员的 target_group 作为报警目标
        account_target_groups: dict[str, int] = {}
        for m in cfg.MONITOR_LIST:
            if m["account_id"] not in account_target_groups:
                account_target_groups[m["account_id"]] = m["target_groups"][0]

        # ── 改进 3：休眠时段暂停轮询 ──
        sleep_sec = _calc_sleep_seconds()
        if sleep_sec > 0:
            jst = _get_jst_now()
            log_all(
                f"😴 休眠时段（{cfg.SLEEP_START_HOUR}:00-{cfg.SLEEP_END_HOUR}:00 JST），"
                f"当前 {jst.hour:02d}:{jst.minute:02d}，暂停 {sleep_sec}s",
                is_debug=True,
            )
            await asyncio.sleep(sleep_sec)
            continue

        # ── 改进 1：每轮巡查前主动检查并刷新即将过期的 Token ──
        await asyncio.gather(*[
            proactive_refresh_if_expiring(acc_id, grp)
            for acc_id, grp in account_target_groups.items()
        ])

        # ── 改进 4：随机打乱成员轮询顺序 ──
        shuffled = list(cfg.MONITOR_LIST)
        random.shuffle(shuffled)

        # Phase 1: 并发抓取所有成员的消息
        fetch_results = await asyncio.gather(
            *[fetcher.fetch_member_messages(m) for m in shuffled],
            return_exceptions=True,
        )

        # Phase 2: 按 shuffled 顺序逐个成员串行推送
        error_members = []
        for i, result in enumerate(fetch_results):
            member = shuffled[i]
            name = member['m_name'].replace(" ", "")

            if isinstance(result, Exception):
                log_all(f"💥 抓取异常 [{name}]: {result}", is_error=True)
                error_members.append(name)
                continue

            if result is None:
                log_all(f"⚠️ 跳过 {name}：抓取返回空（详情见上方错误日志）", is_debug=True)
                error_members.append(name)
                continue

            new_msgs, id_list, id_set, l_time_ref, time_file, file_lock = result
            ok = await fetcher.push_member_messages(
                member, new_msgs, id_list, id_set, l_time_ref, time_file, file_lock
            )
            if not ok:
                error_members.append(name)

        if not error_members:
            log_all(f"🔍 巡查完毕 [{member_names}]")
        else:
            log_all(f"⚠️ 巡查完毕（异常成员：{' · '.join(error_members)}）", is_error=True)

        summary = health.get_tracker().cycle_complete()
        if summary:
            log_all(summary)

        wait_time, tag = _next_interval()
        log_all(f"{tag} | 下次巡查: {wait_time}s 后", is_debug=True)
        await asyncio.sleep(wait_time)


async def _init_mobile_accounts() -> None:
    """启动时为所有 mobile 账号做初始 Token 刷新（仿照 nogizaka-monitor 的 init_tokens）。
    若已有有效 Token 则跳过，避免第一轮抓取浪费在 401 上。"""
    for acc_id, acc_cfg in cfg.ACCOUNTS.items():
        if acc_cfg.get("auth_method") != "mobile":
            continue
        remaining = get_token_remaining_seconds(acc_id)
        if remaining is not None and remaining > 60:
            log_all(f"🔑 移动端账号 {acc_id} Token 有效（剩余 {int(remaining)}s），跳过初始化")
            continue
        # 找到该账号关联的第一个成员的目标群（用于报警）
        target_group = 0
        for m in cfg.MONITOR_LIST:
            if m["account_id"] == acc_id:
                target_group = m["target_groups"][0]
                break
        log_all(f"🔑 移动端账号 {acc_id} 执行初始 Token 刷新...")
        await refresh_mobile_token(acc_id, target_group)


async def main() -> None:
    print("=== 坂道联合监控系统 ===")
    os.makedirs(cfg.TIME_RECORD_DIR, exist_ok=True)
    os.makedirs(cfg.SENT_IDS_DIR, exist_ok=True)

    # 1. 基础设施
    init_loggers()
    load_all_accounts()
    await _init_mobile_accounts()

    # 2. 在事件循环内创建需要 asyncio 的锁（translator / bilibili / tgbot）
    translator.initialize()
    bilibili.initialize()
    tgbot.initialize()

    health.initialize(
        summary_interval=cfg.HEALTH_SUMMARY_INTERVAL,
        error_buffer=cfg.HEALTH_ERROR_BUFFER,
        token_warn_seconds=cfg.HEALTH_TOKEN_WARN_SECONDS,
    )

    # 3. 创建共享 HTTP 客户端
    http_client = httpx.AsyncClient(
        timeout=20,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    qq_client = httpx.AsyncClient(
        timeout=15,
        transport=httpx.AsyncHTTPTransport(retries=0, http2=False, trust_env=False),
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
    )
    semaphore = asyncio.Semaphore(cfg.HTTP_SEMAPHORE_LIMIT)

    # 4. 注入依赖
    napcat.initialize(qq_client)
    qq_official.initialize(qq_client)
    fetcher.initialize(http_client, semaphore)

    # 5. 启动健康检查（改进 3）
    await _health_check(qq_client)
    print()

    # 6. 可选启动 config.json 文件监控（watchdog 未安装时返回 None）
    config_path = Path(__file__).resolve().parent.parent / "config" / "config.json"
    observer = start_watcher(config_path)

    try:
        await _run_loop(http_client)
    except KeyboardInterrupt:
        print("\n🛑 安全退出中...")
    finally:
        if observer is not None:
            observer.stop()
            observer.join()
        await asyncio.gather(http_client.aclose(), qq_client.aclose())
        print("✅ 资源清理完毕")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
