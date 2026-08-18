# ============================================================
# app.py — 程序入口：初始化所有模块、驱动主轮询循环
# ============================================================
import asyncio
import os
import random
import signal
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

from src import archive
from src import fetcher
from src import tagger
from src import translator
from src.platforms import napcat
from src.platforms import qq_official
from src.platforms import tgbot
from src import health
from src import blog_fetcher
from src.social.manager import start_social_service, stop_social_service
from src.platforms.qq_official import health_check as qq_official_health_check
import config.config as cfg
from config.credentials import (
    initialize as init_credentials,
    load_all_accounts, proactive_refresh_if_expiring,
    refresh_token, refresh_mobile_token, get_token_remaining_seconds,
)
from config.watcher import start_watcher
from src.logger import init_loggers, log_all
from src.webui import start_webui
from src.utils import in_hour_range

# ---- 博客状态 ----
_blog_db: object = None
_blog_client: httpx.AsyncClient | None = None


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

    # ── 检查每个成员至少有一个可用推送目标 ──────────────────
    # 官方 Bot 推送的是全局 TARGET_OPENID、不区分成员，所以只要有可用的官方 Bot，
    # 所有成员都有推送目标，无需再看 groups / tg。
    official_covers_all = cfg.ENABLE_QQ_OFFICIAL_BOT and qq_official.has_bots()
    orphans = [] if official_covers_all else [
        m["m_name"] for m in cfg.MONITOR_LIST
        if not (cfg.ENABLE_NAPCAT_QQ and m.get("target_groups"))
        and not (cfg.ENABLE_TG_BOT and (m.get("tg_chat_id") or "").strip())
    ]
    if orphans:
        log_all(
            f"🔴 以下成员没有任何可用推送目标（无生效的 QQ 群 / TG 频道 / 官方 Bot）："
            f"{' · '.join(orphans)}",
            is_error=True,
        )
        all_ok = False

    # ── 检查所有账号凭证已加载且内容完整 ────────────────────
    from config.credentials import ACCOUNT_CREDS, validate_account_cred
    needed = {m["account_id"] for m in cfg.MONITOR_LIST if m.get("account_id")}
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


def _alert_group_for_account(acc_id: str) -> int:
    """该账号的告警目标群号。
    账号下所有成员都没配 QQ 群（纯 TG 推送）时返回 0，告警将只走 TG / 官方 Bot。"""
    for m in cfg.MONITOR_LIST:
        if m["account_id"] == acc_id and m.get("target_groups"):
            return m["target_groups"][0]
    return 0


def _get_jst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


# ──────────────────────────────────────────────
# 每日运行摘要（同时是"死人开关"：哪天没收到摘要 = 系统挂了）
# ──────────────────────────────────────────────
# 停止信号文件：外部（部署脚本）创建它即可让主程序优雅退出。
# 计划任务 / systemd 启动的进程往往需要管理员权限才能杀，用文件信号
# 就绕开了权限问题，也保证走完整的清理流程。
STOP_FILE = Path(__file__).resolve().parent.parent / "logs" / "service.stop"

DISK_WARN_BYTES = 10 * 1024 ** 3   # 磁盘剩余低于此值在摘要里标红
SUMMARY_MAX_ATTEMPTS = 3
SUMMARY_RETRY_SECONDS = 1800       # 失败后 30 分钟补发

def _build_daily_summary() -> str:
    """生成全量每日运行摘要（整合 Message、三团博客、社交媒体、通道健康与存储监控）。"""
    from config.credentials import get_token_remaining_seconds

    jst = _get_jst_now()
    today_str = jst.strftime("%Y-%m-%d")
    lines = [
        f"📅 每日运行摘要 · {today_str}（JST）",
        "─" * 20,
    ]

    # ── 1. Message 消息模块 ──
    lines.append("💌 【Message 消息】")
    if cfg.ARCHIVE_ENABLED:
        msg_db = Path(cfg.ARCHIVE_DIR) / "archive.db"
        member_map = {}
        month_total = 0
        if msg_db.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(msg_db)
                c = conn.cursor()
                c.execute("""
                    SELECT member_name, type, COUNT(*)
                    FROM messages
                    WHERE substr(datetime(published_at, '+9 hours'), 1, 10) = ?
                       OR substr(datetime(updated_at, '+9 hours'), 1, 10) = ?
                    GROUP BY member_name, type
                """, (today_str, today_str))
                for m_name, m_type, cnt in c.fetchall():
                    clean_name = m_name.replace(" ", "")
                    if clean_name not in member_map:
                        member_map[clean_name] = {"total": 0, "types": {}}
                    member_map[clean_name]["total"] += cnt
                    member_map[clean_name]["types"][m_type] = cnt

                c.execute("""
                    SELECT COUNT(*) FROM messages
                    WHERE substr(datetime(published_at, '+9 hours'), 1, 7) = ?
                       OR substr(datetime(updated_at, '+9 hours'), 1, 7) = ?
                """, (today_str[:7], today_str[:7]))
                month_total = (c.fetchone() or [0])[0]
                conn.close()
            except Exception:
                pass

        # 回退从 load_month 统计（兼容无 archive.db 的情况）
        if not member_map:
            for m in cfg.MONITOR_LIST:
                clean_name = m["m_name"].replace(" ", "")
                msgs = archive.load_month(m["m_name"], jst.year, jst.month)
                today_msgs = [
                    msg for msg in msgs
                    if (_to_jst_date(msg.get("published_at") or msg.get("updated_at", ""))) == today_str
                ]
                if today_msgs:
                    member_map[clean_name] = {"total": len(today_msgs), "types": {}}
                    for msg in today_msgs:
                        t = msg.get("type", "text")
                        member_map[clean_name]["types"][t] = member_map[clean_name]["types"].get(t, 0) + 1

        if member_map:
            type_icons = {"text": "📝", "picture": "📸", "image": "📸", "voice": "🎙️", "video": "🎬"}
            for m_name, data in member_map.items():
                type_str_list = []
                for t_name, t_cnt in data["types"].items():
                    icon = type_icons.get(t_name, "📄")
                    type_str_list.append(f"{icon}{t_cnt}")
                types_formatted = f" ({' '.join(type_str_list)})" if type_str_list else ""
                lines.append(f"  • {m_name} {data['total']} 条{types_formatted}")
            if month_total:
                lines.append(f"  • 当月累计接收: {month_total} 条")
        else:
            lines.append("  • 今日无新消息")
    else:
        lines.append("  • 消息归档未启用")

    # ── 2. 官方博客模块 ──
    lines.append("\n📝 【官方博客】")
    blog_db = Path("data/archive/blogs.db")
    if blog_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(blog_db)
            c = conn.cursor()
            c.execute("""
                SELECT group_key, author, title
                FROM blog_posts
                WHERE substr(date, 1, 10) = ?
                   OR substr(datetime(created_at, '+9 hours'), 1, 10) = ?
                ORDER BY id ASC
            """, (today_str, today_str))
            b_rows = c.fetchall()
            conn.close()

            if b_rows:
                group_labels = {"nogizaka": "乃木坂46", "sakurazaka": "樱坂46", "hinatazaka": "日向坂46"}
                group_posts = {}
                for gkey, author, _ in b_rows:
                    group_posts.setdefault(gkey, []).append(author)

                lines.append(f"  • 今日更新 {len(b_rows)} 篇:")
                for gkey, authors in group_posts.items():
                    g_label = group_labels.get(gkey, gkey)
                    seen = []
                    for a in authors:
                        if a and a not in seen:
                            seen.append(a)
                    author_preview = "、".join(seen[:3])
                    if len(seen) > 3:
                        author_preview += f" 等 {len(seen)} 人"
                    author_suffix = f" ({author_preview})" if author_preview else ""
                    lines.append(f"    - {g_label}: {len(authors)} 篇{author_suffix}")
            else:
                lines.append("  • 今日三团官网暂无新博客")
        except Exception as e:
            lines.append(f"  • 博客统计异常: {e}")
    else:
        lines.append("  • 博客模块运行中")

    # ── 3. 社交媒体与直播 ──
    lines.append("\n🌐 【社交媒体 & 直播】")
    social_db = Path("data/archive.db")
    if social_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(social_db)
            c = conn.cursor()
            c.execute("""
                SELECT platform, kind, COUNT(*)
                FROM posts
                WHERE substr(datetime(ts, 'unixepoch', '+9 hours'), 1, 10) = ?
                   OR substr(datetime(archived_at, 'unixepoch', '+9 hours'), 1, 10) = ?
                GROUP BY platform, kind
            """, (today_str, today_str))
            s_rows = c.fetchall()
            conn.close()

            if s_rows:
                plat_map = {}
                for plat, kind, cnt in s_rows:
                    plat_map.setdefault(plat, {})[kind or "post"] = cnt

                plat_labels = {"x": "X", "instagram": "Instagram", "tiktok": "TikTok", "tiktok_live": "TikTok直播"}
                summary_parts = []
                for p_key, kinds in plat_map.items():
                    p_name = plat_labels.get(p_key, p_key)
                    total_cnt = sum(kinds.values())
                    if p_key == "instagram" and "story" in kinds:
                        stories = kinds["story"]
                        posts = total_cnt - stories
                        summary_parts.append(f"{p_name} {total_cnt} 条 ({posts} 贴文 / {stories} Story)")
                    else:
                        summary_parts.append(f"{p_name} {total_cnt} 条")
                lines.append("  • 今日动态: " + " · ".join(summary_parts))
            else:
                lines.append("  • 今日暂无新增社媒动态")
        except Exception as e:
            lines.append(f"  • 社媒统计异常: {e}")
    else:
        lines.append("  • 社媒模块运行中")

    # ── 4. 通道与系统健康 ──
    lines.append("\n🤖 【通道与系统健康】")
    snap = health.get_tracker().snapshot()
    uptime_h = int(snap["uptime_seconds"] // 3600)
    uptime_m = int((snap["uptime_seconds"] % 3600) // 60)
    lines.append(f"  • 巡查状态: 连续运行 {uptime_h}h {uptime_m}m · 第 {snap['cycle_count']} 轮")

    token_parts = []
    for acc_id in cfg.ACCOUNTS:
        remaining = get_token_remaining_seconds(acc_id)
        if remaining is None:
            token_parts.append(f"{acc_id} 未知")
        elif remaining <= 0:
            token_parts.append(f"{acc_id} 失效🔴")
        else:
            token_parts.append(f"{acc_id} 正常")
    if token_parts:
        lines.append("  • 凭证Token: " + " · ".join(token_parts))

    t_models = []
    if getattr(cfg, "GEMINI_API_KEY", ""):
        t_models.append(f"Gemini ({getattr(cfg, 'GEMINI_MODEL', 'gemini-3.7-flash')})")
    if getattr(cfg, "ZHIPU_API_KEY", ""):
        t_models.append(f"智谱 ({getattr(cfg, 'ZHIPU_MODEL', 'glm-4-flash')})")
    if t_models:
        lines.append("  • 翻译引擎: " + " · ".join(t_models))

    persistent = [e for e in snap["errors"] if e["tier"] == "PERSISTENT"]
    if persistent:
        lines.append(f"  • 待处理错误: ⚠️ {len(persistent)} 条（最新: {persistent[-1]['msg'][:40]}）")
    else:
        lines.append("  • 异常状态: 正常（无待处理错误）")

    # ── 5. 存储空间概况 ──
    lines.append("\n💾 【存储与磁盘空间】")
    storage = _storage_line()
    if storage:
        lines.append(f"  • {storage}")

    lines.append("─" * 20)
    lines.append("（收到本摘要即代表系统在正常运行 · 反向心跳守候中）")
    return "\n".join(lines)


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _storage_line() -> str:
    """存储占用细分 + 磁盘剩余。"""
    from src.utils import get_storage_breakdown
    try:
        sb = get_storage_breakdown()
        cats = sb.get("categories", {})
        msg_h = cats.get("message_media", {}).get("human", "0 B")
        blog_h = cats.get("blog_images", {}).get("human", "0 B")
        social_h = cats.get("social_media", {}).get("human", "0 B")
        live_h = cats.get("live_recordings", {}).get("human", "0 B")
        app_total_h = sb.get("app_total", {}).get("human", "0 B")
        free_h = sb.get("disk", {}).get("free_human", "0 B")
        free_b = sb.get("disk", {}).get("free_bytes", 0)

        warn = " ⚠️ 磁盘空间不足" if free_b < DISK_WARN_BYTES else ""
        return (f"存储: 归档占用 {app_total_h} (消息 {msg_h} · 博客 {blog_h} · 社媒 {social_h} · 录像 {live_h}) · "
                f"磁盘剩余 {free_h}{warn}")
    except Exception:
        return ""


def _to_jst_date(utc_str: str) -> str:
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    except ValueError:
        return ""


async def _send_summary_with_retry() -> None:
    """发送每日摘要，失败后重试 —— 摘要本身是死人开关，
    它自己静默失败的话，就等于监控失灵了。"""
    from src.notifier import send_report_message

    for attempt in range(1, SUMMARY_MAX_ATTEMPTS + 1):
        try:
            if await send_report_message(_build_daily_summary()):
                log_all("📅 每日摘要已发送" if attempt == 1
                        else f"📅 每日摘要已发送（第 {attempt} 次尝试）")
                return
            reason = "所有通道均未成功"
        except asyncio.CancelledError:
            raise
        except Exception:
            reason = "异常"
            log_all(f"⚠️ 每日摘要异常:\n{traceback.format_exc()}", is_error=True)

        if attempt < SUMMARY_MAX_ATTEMPTS:
            log_all(f"⚠️ 每日摘要发送失败（{reason}），{SUMMARY_RETRY_SECONDS // 60} 分钟后重试"
                    f"（{attempt}/{SUMMARY_MAX_ATTEMPTS}）", is_error=True)
            await asyncio.sleep(SUMMARY_RETRY_SECONDS)
        else:
            log_all(f"🚨 每日摘要连续 {SUMMARY_MAX_ATTEMPTS} 次发送失败，本次放弃", is_error=True)
            health.get_tracker().record_error("每日摘要发送失败", health.ErrorTier.PERSISTENT)


async def _daily_summary_loop() -> None:
    while True:
        jst = _get_jst_now()
        target = jst.replace(hour=cfg.DAILY_SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        if target <= jst:
            target += timedelta(days=1)
        await asyncio.sleep((target - jst).total_seconds())
        await _send_summary_with_retry()


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


async def _run_cycle() -> None:
    """单轮巡查：主动续期 → 并发抓取 → 串行推送。"""
    # ── Phase 1: Message 消息巡查 ──
    if getattr(cfg, "MESSAGE_MONITOR_ENABLED", True):
        valid_monitors = [m for m in cfg.MONITOR_LIST if m.get("account_id") and m.get("m_id")]
        member_names = " · ".join(m["m_name"].replace(" ", "") for m in valid_monitors)

        # 每个账号取一个 target_group 作为报警目标
        account_target_groups: dict[str, int] = {
            m["account_id"]: _alert_group_for_account(m["account_id"])
            for m in valid_monitors
            if m.get("account_id")
        }

        # ── 改进 1：每轮巡查前主动检查并刷新即将过期的 Token ──
        if account_target_groups:
            await asyncio.gather(*[
                proactive_refresh_if_expiring(acc_id, grp)
                for acc_id, grp in account_target_groups.items()
            ])

        # ── 改进 4：随机打乱成员轮询顺序 ──
        shuffled = list(valid_monitors)
        random.shuffle(shuffled)

        if shuffled:
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
                # 单个成员的推送异常不应波及其他成员（record_member_push 内部已记 TRANSIENT 错误）
                try:
                    ok = await fetcher.push_member_messages(
                        member, new_msgs, id_list, id_set, l_time_ref, time_file, file_lock
                    )
                except Exception:
                    log_all(f"💥 推送异常 [{name}]:\n{traceback.format_exc()}", is_error=True)
                    health.get_tracker().record_member_push(name, False)
                    error_members.append(name)
                    continue

                if not ok:
                    error_members.append(name)

            if not error_members:
                log_all(f"🔍 巡查完毕 [{member_names}]")
            else:
                log_all(f"⚠️ 巡查完毕（异常成员：{' · '.join(error_members)}）", is_error=True)
    else:
        log_all("⏸️ Message 监控已暂停（配置已关闭）", is_debug=True)

    # ── 博客巡查 ──
    blog_cfg = cfg._config.get("blog_monitor") or {}
    if blog_cfg.get("enabled", True) and cfg._config.get("blog_records") is not None:
        try:
            new_posts = await blog_fetcher.run_blog_cycle(
                _blog_client, _blog_db, cfg._config)
            health.get_tracker().record_member_fetch("博客 (全局)", True)
            if new_posts:
                from src.notifier import send_blog_post
                log_all(f"📝 博客更新：{len(new_posts)} 篇")
                for post in new_posts:
                    try:
                        ok = await send_blog_post(post)
                        if ok:
                            log_all(f"✅ 博客 [{post.get('title', '无题')}] 推送完成")
                        else:
                            log_all(f"⚠️ 博客 [{post.get('title', '无题')}] 推送失败（无可用通道）", is_error=True)
                    except Exception as e:
                        log_all(f"💥 博客推送异常: {e}", is_error=True)
                        health.get_tracker().record_member_push("博客 (全局)", False)
                    await asyncio.sleep(0.5)
            health.get_tracker().record_member_push("博客 (全局)", True)
            
        except Exception as e:
            health.get_tracker().record_member_fetch("博客 (全局)", False, health.ErrorTier.TRANSIENT, str(e))
            log_all(f"⚠️ 博客巡查异常: {e}", is_error=True)


def _stop_requested() -> bool:
    """外部是否请求停止（存在停止信号文件）。"""
    try:
        return STOP_FILE.exists()
    except OSError:
        return False


async def _wait_or_trigger(event: asyncio.Event, timeout: float) -> bool:
    """等待 timeout 秒；期间事件被置位（网页「立即巡查」）则提前返回 True。"""
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        event.clear()
        return True
    except asyncio.TimeoutError:
        return False


async def _run_loop(http_client: httpx.AsyncClient, poll_event: asyncio.Event,
                    stop_event: asyncio.Event | None = None) -> None:
    while True:
        if _stop_requested():
            log_all("🛑 检测到停止信号文件，优雅退出")
            if stop_event is not None:
                stop_event.set()
            return
        try:
            # ── 改进 3：休眠时段暂停轮询（手动触发可唤醒）──
            sleep_sec = _calc_sleep_seconds()
            if sleep_sec > 0:
                jst = _get_jst_now()
                log_all(
                    f"😴 休眠时段（{cfg.SLEEP_START_HOUR}:00-{cfg.SLEEP_END_HOUR}:00 JST），"
                    f"当前 {jst.hour:02d}:{jst.minute:02d}，暂停 {sleep_sec}s",
                    is_debug=True,
                )
                health.get_tracker().record_next_cycle(time.time() + sleep_sec, "😴 休眠")
                if not await _wait_or_trigger(poll_event, sleep_sec):
                    continue
                log_all("⏩ 休眠时段手动触发巡查", is_debug=True)

            await _run_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 任何未预料的异常都不该终止长驻循环：记录后照常等待下一轮
            log_all(f"💥 巡查轮次异常，跳过本轮:\n{traceback.format_exc()}", is_error=True)
            health.get_tracker().record_error("巡查轮次异常", health.ErrorTier.TRANSIENT)

        summary = health.get_tracker().cycle_complete()
        if summary:
            log_all(summary)

        wait_time, tag = _next_interval()
        health.get_tracker().record_next_cycle(time.time() + wait_time, tag)
        log_all(f"{tag} | 下次巡查: {wait_time}s 后", is_debug=True)
        # 长等待期间也要能及时响应停止信号，切成小段轮询
        waited = 0.0
        while waited < wait_time:
            slice_s = min(10.0, wait_time - waited)
            if await _wait_or_trigger(poll_event, slice_s):
                log_all("⏩ 手动触发巡查", is_debug=True)
                break
            waited += slice_s
            if _stop_requested():
                break


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
    """启动时为所有账号做初始 Token 刷新与握手。
    - mobile 账号：使用 refresh_token 换取初始 access_token
    - web 账号：若 Token 临期或需要握手，主动调用 refresh_token 获取并持久化 Set-Cookie
    若已有充足有效 Token 则跳过，避免第一轮抓取浪费在 401 上。"""
    if not getattr(cfg, "MESSAGE_MONITOR_ENABLED", True):
        log_all("⏸️ Message 监控已关闭，跳过账号初始握手", is_debug=True)
        return
    for acc_id, acc_cfg in cfg.ACCOUNTS.items():
        is_mobile = acc_cfg.get("auth_method") == "mobile"
        remaining = get_token_remaining_seconds(acc_id)
        if remaining is not None and remaining > 60:
            log_all(f"🔑 账号 {acc_id} Token 有效（剩余 {int(remaining)}s），跳过初始化")
            continue
        target_group = _alert_group_for_account(acc_id)
        if is_mobile:
            log_all(f"🔑 移动端账号 {acc_id} 执行初始 Token 刷新...")
            await refresh_mobile_token(acc_id, target_group)
        else:
            log_all(f"🔑 Web 账号 {acc_id} 执行初始 Token 刷新与握手...")
            await refresh_token(acc_id, target_group)


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
    print("=== 坂道联合监控系统 ===")
    os.makedirs(cfg.TIME_RECORD_DIR, exist_ok=True)
    os.makedirs(cfg.SENT_IDS_DIR, exist_ok=True)
    # 上次的停止信号不该影响本次启动
    try:
        STOP_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    # 1. 基础设施
    init_loggers()
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
        banner = (
            "\n" + "=" * 70 + "\n"
            "🔑 系统首次运行：已为您自动创建初始管理员账号！\n"
            f"   • 用户名:   {admin_user}\n"
            f"   • 初始密码: {admin_pw}\n"
            "   • Web 管理端: http://127.0.0.1:8787/\n\n"
            "⚠️ 请妥善保存初始密码！若遗忘，可在终端执行：\n"
            f"   python tools/manage_users.py passwd {admin_user}\n"
            "=" * 70 + "\n"
        )
        print(banner, flush=True)
        log_all("🔑 系统首次运行：已初始化创建 admin 账号（初始密码已输出至控制台）")

    proxy_url = getattr(cfg, "PROXY", "") or None
    if proxy_url:
        log_all(f"🌐 已配置网络代理: {proxy_url}")

    # 2. 创建共享 HTTP 客户端
    http_client = httpx.AsyncClient(
        timeout=20,
        proxy=proxy_url,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    qq_client = httpx.AsyncClient(
        timeout=15,
        transport=httpx.AsyncHTTPTransport(retries=0, http2=False, trust_env=False),
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
    )
    semaphore = asyncio.Semaphore(cfg.HTTP_SEMAPHORE_LIMIT)

    # 3. 注入依赖 & 在事件循环内创建各模块的锁
    #    translator / credentials 也复用共享连接池，避免每次翻译/续期新建 TLS 连接
    init_credentials(http_client)
    translator.initialize(http_client)
    archive.initialize(http_client)
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

    # 4. 账号初始 Token 刷新与自动握手
    #    必须放在通道注入（步骤 3）之后：刷新失败时 refresh_token 会走
    #    send_alert_message，此时 napcat._client / tgbot._bot 必须已就绪，否则告警静默丢失。
    await _init_accounts()

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
            elif channel == "official":
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
                    on_openid=_openid_action)
        if cfg.WEB_ADMIN_ENABLED else None
    )

    summary_task = (
        asyncio.create_task(_daily_summary_loop()) if cfg.DAILY_SUMMARY_ENABLED else None
    )

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
            print("\n🛑 收到停止信号，安全退出中...")
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
        else:
            stop_task.cancel()
            loop_task.result()   # _run_loop 不会正常返回；到这里说明它抛了异常，向上传播
    except KeyboardInterrupt:
        print("\n🛑 安全退出中...")
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
        except Exception:
            pass
        try:
            await asyncio.gather(http_client.aclose(), qq_client.aclose(), return_exceptions=True)
        except Exception:
            pass
        if _blog_client is not None:
            try:
                await _blog_client.aclose()
            except Exception:
                pass
        print("✅ 资源清理完毕")

    if restart_requested:
        # 进程自替换：同 PID 拉起全新进程，.env / 模块状态全部重新加载
        print("🔁 正在重启主程序...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
