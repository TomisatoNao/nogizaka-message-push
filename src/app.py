# ============================================================
# main.py — 程序入口：初始化所有模块、驱动主轮询循环
# ============================================================
import asyncio
import os
import random
from datetime import datetime, timezone, timedelta

import httpx

from src import fetcher
from src import translator
from src.platforms import bilibili
from src.platforms import napcat
from src.platforms import qq_official
from src.platforms.qq_official import health_check as qq_official_health_check
from config.config import (
    ACCOUNTS,
    DAY_INTERVAL,
    DAY_START_HOUR,
    ENABLE_NAPCAT_QQ,
    ENABLE_QQ_OFFICIAL_BOT,
    HTTP_SEMAPHORE_LIMIT,
    MONITOR_LIST,
    NIGHT_INTERVAL,
    NIGHT_START_HOUR,
    QQ_BOT_API,
    SENT_IDS_DIR,
    TIME_RECORD_DIR,
)
from config.credentials import load_all_accounts, proactive_refresh_if_expiring
from src.logger import init_loggers, log_all


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

    if not ENABLE_NAPCAT_QQ and not ENABLE_QQ_OFFICIAL_BOT:
        log_all("🟡 QQ 推送通道均未启用：成员消息会被抓取并记录，但不会推送到 QQ", is_error=True)

    # ── 检查 NapCat 连通性 ────────────────────────────────
    if ENABLE_NAPCAT_QQ:
        status_url = QQ_BOT_API.rsplit("/", 1)[0] + "/get_status"
        try:
            resp = await qq_client.get(status_url)
            if resp.status_code == 200:
                log_all("🟢 NapCat QQ 连通正常")
            else:
                log_all(f"🟡 NapCat QQ 返回 HTTP {resp.status_code}，可能运行异常", is_error=True)
                all_ok = False
        except Exception as e:
            log_all(f"🔴 NapCat QQ 无法连接 ({type(e).__name__})，请确认 napcat/lagrange 已启动", is_error=True)
            all_ok = False
    else:
        log_all("⏸️ NapCat QQ 推送未启用")

    # ── 检查官方 QQ Bot 凭证 ──────────────────────────────
    if ENABLE_QQ_OFFICIAL_BOT:
        if not await qq_official_health_check():
            all_ok = False
    else:
        log_all("⏸️ 官方 QQ Bot 推送未启用")

    # ── 检查所有账号凭证已加载 ────────────────────────────
    from config.credentials import ACCOUNT_CREDS
    needed = {m["account_id"] for m in MONITOR_LIST}
    missing = needed - set(ACCOUNT_CREDS.keys())
    if missing:
        log_all(f"🔴 以下账号凭证缺失：{missing}", is_error=True)
        all_ok = False
    else:
        log_all(f"🟢 账号凭证完整（{len(needed)} 个账号）")

    return all_ok


def _get_jst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def _next_interval() -> tuple[int, str]:
    jst = _get_jst_now()
    if NIGHT_START_HOUR <= jst.hour < DAY_START_HOUR:
        return random.randint(*NIGHT_INTERVAL), "🌙 深夜低速"
    return random.randint(*DAY_INTERVAL), "☀️ 日间巡查"


async def _run_loop(http_client: httpx.AsyncClient) -> None:
    member_names = " · ".join(m["m_name"].replace(" ", "") for m in MONITOR_LIST)

    # 每个账号取第一个关联成员的 target_group 作为报警目标
    account_target_groups: dict[str, int] = {}
    for m in MONITOR_LIST:
        if m["account_id"] not in account_target_groups:
            account_target_groups[m["account_id"]] = m["target_group"]

    while True:
        # ── 改进 1：每轮巡查前主动检查并刷新即将过期的 Token ──
        await asyncio.gather(*[
            proactive_refresh_if_expiring(acc_id, grp)
            for acc_id, grp in account_target_groups.items()
        ])

        results = await asyncio.gather(
            *[fetcher.fetch_member(m) for m in MONITOR_LIST],
            return_exceptions=True,
        )

        all_ok = True
        error_members = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                name = MONITOR_LIST[i]['m_name']
                log_all(f"💥 任务异常 [{name}]: {res}", is_error=True)
                error_members.append(name.replace(" ", ""))
                all_ok = False

        if all_ok:
            log_all(f"🔍 巡查完毕 [{member_names}]")
        else:
            log_all(f"⚠️ 巡查完毕（异常成员：{' · '.join(error_members)}）", is_error=True)

        wait_time, tag = _next_interval()
        log_all(f"{tag} | 下次巡查: {wait_time}s 后", is_debug=True)
        await asyncio.sleep(wait_time)


async def main() -> None:
    print("=== 坂道联合监控系统 ===")
    os.makedirs(TIME_RECORD_DIR, exist_ok=True)
    os.makedirs(SENT_IDS_DIR, exist_ok=True)

    # 1. 基础设施
    init_loggers()
    load_all_accounts()

    # 2. 在事件循环内创建需要 asyncio 的锁（translator / bilibili）
    translator.initialize()
    bilibili.initialize()

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
    semaphore = asyncio.Semaphore(HTTP_SEMAPHORE_LIMIT)

    # 4. 注入依赖
    napcat.initialize(qq_client)
    qq_official.initialize(qq_client)
    fetcher.initialize(http_client, semaphore)

    # 5. 启动健康检查（改进 3）
    await _health_check(qq_client)
    print()

    try:
        await _run_loop(http_client)
    except KeyboardInterrupt:
        print("\n🛑 安全退出中...")
    finally:
        await asyncio.gather(http_client.aclose(), qq_client.aclose())
        print("✅ 资源清理完毕")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
