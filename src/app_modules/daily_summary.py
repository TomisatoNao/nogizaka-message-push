"""
src/app_modules/daily_summary.py — 每日运行摘要（反向心跳 / 死人开关）与存储统计
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import traceback

import config.config as cfg
from src import archive, health
from src.logger import log_all

DISK_WARN_BYTES = 10 * 1024 ** 3   # 磁盘剩余低于此值在摘要里标红
SUMMARY_MAX_ATTEMPTS = 3
SUMMARY_RETRY_SECONDS = 1800       # 失败后 30 分钟补发


def _get_jst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def _to_jst_date(utc_str: str) -> str:
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    except ValueError:
        return ""


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
        app_mod = sys.modules.get("src.app")
        warn_bytes = getattr(app_mod, "DISK_WARN_BYTES", DISK_WARN_BYTES) if app_mod else DISK_WARN_BYTES

        sb = get_storage_breakdown()
        cats = sb.get("categories", {})
        msg_h = cats.get("message_media", {}).get("human", "0 B")
        blog_h = cats.get("blog_images", {}).get("human", "0 B")
        social_h = cats.get("social_media", {}).get("human", "0 B")
        live_h = cats.get("live_recordings", {}).get("human", "0 B")
        app_total_h = sb.get("app_total", {}).get("human", "0 B")
        free_h = sb.get("disk", {}).get("free_human", "0 B")
        free_b = sb.get("disk", {}).get("free_bytes", 0)

        warn = " ⚠️ 磁盘空间不足" if free_b < warn_bytes else ""
        return (f"存储: 归档占用 {app_total_h} (消息 {msg_h} · 博客 {blog_h} · 社媒 {social_h} · 录像 {live_h}) · "
                f"磁盘剩余 {free_h}{warn}")
    except Exception:
        return ""


def _build_daily_summary() -> str:
    """生成全量每日运行摘要（整合 Message、三团博客、社交媒体、通道健康与存储监控）。"""
    from config.credentials import get_token_remaining_seconds

    app_mod = sys.modules.get("src.app")
    storage_line_fn = getattr(app_mod, "_storage_line", _storage_line) if app_mod else _storage_line

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
        member_map: dict[str, dict] = {}
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
            except (sqlite3.Error, OSError, ValueError) as ex:
                log_all(f"⚠️ 今日汇总 SQLite 查询跳过: {ex}", is_debug=True)

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
                group_posts: dict[str, list[str]] = {}
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
                plat_map: dict[str, dict[str, int]] = {}
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
    storage = storage_line_fn()
    if storage:
        lines.append(f"  • {storage}")

    lines.append("─" * 20)
    lines.append("（收到本摘要即代表系统在正常运行 · 反向心跳守候中）")
    return "\n".join(lines)


async def _send_summary_with_retry() -> None:
    """发送每日摘要，失败后重试 —— 摘要本身是死人开关，
    它自己静默失败的话，就等于监控失灵了。"""
    from src.notifier import send_report_message

    app_mod = sys.modules.get("src.app")
    build_fn = getattr(app_mod, "_build_daily_summary", _build_daily_summary) if app_mod else _build_daily_summary
    max_attempts = getattr(app_mod, "SUMMARY_MAX_ATTEMPTS", SUMMARY_MAX_ATTEMPTS) if app_mod else SUMMARY_MAX_ATTEMPTS
    retry_seconds = getattr(app_mod, "SUMMARY_RETRY_SECONDS", SUMMARY_RETRY_SECONDS) if app_mod else SUMMARY_RETRY_SECONDS

    for attempt in range(1, max_attempts + 1):
        try:
            if await send_report_message(build_fn()):
                log_all("📅 每日摘要已发送" if attempt == 1
                        else f"📅 每日摘要已发送（第 {attempt} 次尝试）")
                return
            reason = "所有通道均未成功"
        except asyncio.CancelledError:
            raise
        except Exception:
            reason = "异常"
            log_all(f"⚠️ 每日摘要异常:\n{traceback.format_exc()}", is_error=True)

        if attempt < max_attempts:
            log_all(f"⚠️ 每日摘要发送失败（{reason}），{retry_seconds // 60} 分钟后重试"
                    f"（{attempt}/{max_attempts}）", is_error=True)
            await asyncio.sleep(retry_seconds)
        else:
            log_all(f"🚨 每日摘要连续 {max_attempts} 次发送失败，本次放弃", is_error=True)
            health.get_tracker().record_error("每日摘要发送失败", health.ErrorTier.PERSISTENT)


async def _daily_summary_loop() -> None:
    app_mod = sys.modules.get("src.app")
    send_fn = getattr(app_mod, "_send_summary_with_retry", _send_summary_with_retry) if app_mod else _send_summary_with_retry
    while True:
        jst = _get_jst_now()
        target = jst.replace(hour=cfg.DAILY_SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        if target <= jst:
            target += timedelta(days=1)
        await asyncio.sleep((target - jst).total_seconds())
        await send_fn()


__all__ = [
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
]
