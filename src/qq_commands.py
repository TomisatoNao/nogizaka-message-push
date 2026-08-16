# ============================================================
# qq_commands.py — 官方 QQ Bot 指令：私聊 Bot 查询系统状态与归档
# ============================================================
# 官方 Bot 是公开的，任何人都能给它发消息，因此：
#   - 只响应白名单 openid（默认取各 Bot 的 target_openid，即只有你自己）
#   - 只提供只读查询，不提供重启 / 改配置 / 看凭证这类高危操作
#     （那些在网页管理端做，有登录和权限体系兜着）
# ============================================================
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import config.config as cfg

JST = timezone(timedelta(hours=9))
MAX_REPLY_CHARS = 900        # 官方 Bot 单条消息上限保守值
MAX_LIST_ITEMS = 5           # 列表类回复最多列几项

_SOCIAL_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com|instagram\.com|tiktok\.com|v\.douyin\.com)/[^\s]+"
)


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds}s"


def _jst_str(utc_str: str) -> str:
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(JST).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return utc_str[:16]


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


# ──────────────────────────────────────────────
# 各指令实现
# ──────────────────────────────────────────────

def _cmd_help(_args: str) -> str:
    return (
        "📖 可用指令\n"
        "/status — 运行状态\n"
        "/members — 监控成员列表\n"
        "/latest [成员] [条数] — 最近消息（默认 3 条）\n"
        "/search 关键词 — 搜索归档（原文与译文）\n"
        "/stats — 归档统计\n"
        "/help — 本帮助\n"
        "💡 提示：私聊直接发送 X / Instagram / TikTok 链接，Bot 将自动解析并回复原图/视频与 AI 双语翻译。"
    )



def _cmd_status(_args: str) -> str:
    from config.credentials import get_token_remaining_seconds
    from src import health

    snap = health.get_tracker().snapshot()
    lines = [f"📊 第 {snap['cycle_count']} 轮 · 运行 {_fmt_duration(snap['uptime_seconds'])}"]

    nxt = snap.get("next_cycle")
    if nxt:
        left = nxt["at_epoch"] - time.time()
        lines.append(f"下次巡查: {_fmt_duration(left)} 后 {nxt.get('tag', '')}".strip())

    tokens = []
    for acc_id in cfg.ACCOUNTS:
        remaining = get_token_remaining_seconds(acc_id)
        if remaining is None:
            tokens.append(f"{acc_id} 未知")
        elif remaining <= 0:
            tokens.append(f"{acc_id} 失效🔴")
        else:
            tokens.append(f"{acc_id} {_fmt_duration(remaining)}")
    if tokens:
        lines.append("Token: " + " · ".join(tokens))

    channels = snap.get("channels") or {}
    if channels:
        lines.append("通道: " + " | ".join(
            f"{n} {c['success']}/{c['total']}" for n, c in channels.items()))

    bad = [m["name"] for m in snap.get("members", []) if not m["fetch_ok"] or not m["push_ok"]]
    lines.append(f"⚠️ 异常成员: {' · '.join(bad)}" if bad else "成员状态正常")

    persistent = [e for e in snap.get("errors", []) if e["tier"] == "PERSISTENT"]
    if persistent:
        lines.append(f"⚠️ 待处理错误 {len(persistent)} 条: {_clip(persistent[-1]['msg'], 60)}")
    return "\n".join(lines)


def _cmd_members(_args: str) -> str:
    if not cfg.MONITOR_LIST:
        return "还没有配置监控成员。"
    lines = ["👥 监控成员"]
    for m in cfg.MONITOR_LIST:
        targets = []
        if m.get("target_groups"):
            targets.append(f"QQ群×{len(m['target_groups'])}")
        if (m.get("tg_chat_id") or "").strip():
            targets.append("TG")
        lines.append(f"· {m['m_name']}（id={m['m_id']}）{' '.join(targets)}")
    return "\n".join(lines)


def _resolve_member(name_hint: str) -> str | None:
    """把用户输入的成员名映射到归档目录名；未指定时取第一个监控成员。"""
    from src import archive

    dirs = archive.list_members()
    if not dirs:
        return None
    if not name_hint:
        if cfg.MONITOR_LIST:
            want = archive.member_dir_name(cfg.MONITOR_LIST[0]["m_name"])
            if want in dirs:
                return want
        return dirs[0]
    hint = name_hint.replace(" ", "")
    for d in dirs:
        if hint in d.replace("_", ""):
            return d
    return None


def _cmd_latest(args: str) -> str:
    from src import archive

    parts = args.split()
    count = 3
    if parts and parts[-1].isdigit():
        count = max(1, min(int(parts[-1]), MAX_LIST_ITEMS))
        parts = parts[:-1]
    member = _resolve_member(" ".join(parts))
    if not member:
        return "没找到该成员的归档。用 /members 看可用成员。"

    months = archive.list_months(member)
    if not months:
        return f"{member} 还没有归档内容。"

    msgs: list[dict] = []
    for m in months:                      # list_months 已是新月份在前
        msgs = archive.load_month(member, m["year"], m["month"]) + msgs
        if len(msgs) >= count:
            break
    picked = sorted(msgs, key=lambda x: x.get("updated_at", ""))[-count:]
    if not picked:
        return f"{member} 还没有归档内容。"

    lines = [f"💬 {member.replace('_', ' ')} 最近 {len(picked)} 条"]
    for msg in reversed(picked):
        head = f"[{_jst_str(msg.get('published_at') or msg.get('updated_at', ''))}]"
        kind = {"picture": "🖼", "image": "🖼", "video": "🎬", "voice": "🎤"}.get(msg.get("type"), "")
        body = _clip(msg.get("_translation") or msg.get("text") or "", 80) or "（无文字）"
        lines.append(f"{head}{kind} {body}")
    return "\n".join(lines)


def _cmd_search(args: str) -> str:
    from src import archive

    query = args.strip()
    if not query:
        return "用法：/search 关键词（原文和译文都会搜）"
    member = _resolve_member("")
    if not member:
        return "还没有归档内容。"

    hits = archive.search(member, query)
    if not hits:
        return f"🔍「{query}」没有命中。"
    lines = [f"🔍「{query}」命中 {len(hits)} 条，最近 {min(len(hits), MAX_LIST_ITEMS)} 条："]
    for msg in hits[:MAX_LIST_ITEMS]:
        head = f"[{_jst_str(msg.get('published_at') or msg.get('updated_at', ''))}]"
        body = _clip(msg.get("_translation") or msg.get("text") or "", 70)
        lines.append(f"{head} {body}")
    return "\n".join(lines)


def _cmd_stats(_args: str) -> str:
    from src import archive

    members = archive.list_members()
    if not members:
        return "还没有归档内容。"
    today = datetime.now(JST).strftime("%Y-%m-%d")
    lines = ["📚 归档统计"]
    total_all = 0
    for name in members[:MAX_LIST_ITEMS]:
        months = archive.list_months(name)
        total = sum(m["count"] for m in months)
        total_all += total
        today_n = archive.day_counts(name).get(today, 0)
        lines.append(f"· {name.replace('_', ' ')}: {total} 条 / {len(months)} 个月"
                     + (f" · 今日 {today_n}" if today_n else ""))
    if len(members) > MAX_LIST_ITEMS:
        lines.append(f"（另有 {len(members) - MAX_LIST_ITEMS} 位成员未列出）")
    lines.append(f"合计 {total_all} 条")
    return "\n".join(lines)


_COMMANDS = {
    "help":    _cmd_help,
    "status":  _cmd_status,
    "members": _cmd_members,
    "latest":  _cmd_latest,
    "search":  _cmd_search,
    "stats":   _cmd_stats,
}


# ──────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────

def allowed_senders() -> set[str]:
    """允许使用指令的 openid 白名单。

    默认取各 Bot 的 target_openid —— 也就是"只有你自己能用"；
    config.json 的 qq_commands.allow_openids 可显式指定。
    """
    explicit = getattr(cfg, "QQ_COMMANDS_ALLOW", None) or []
    if explicit:
        return {str(x).strip() for x in explicit if str(x).strip()}
    return {b.get("target_openid", "").strip()
            for b in cfg.QQ_OFFICIAL_BOTS if b.get("target_openid", "").strip()}


async def _async_parse_and_reply_social(url: str, sender_openid: str, app_id: str = ""):
    """后台任务：解析社媒链接、下载多媒体、AI 翻译并直接私聊回复给发送者。"""
    import os
    from src.logger import log_all
    try:
        from src.social.single_fetcher import SocialUrlParser
        from src.social.downloader import MediaDownloader
        from src.social.forwarder import SocialForwarder, build_post_message, collect_alts
        from src.platforms import qq_official

        raw_cfg = cfg._load_config() if hasattr(cfg, "_load_config") else {}
        parser = SocialUrlParser(raw_cfg)

        # 1. 解析动态
        post = parser.parse(url)

        # 2. 并发执行：媒体多线程下载 与 AI 智能翻译（大幅缩短等待时间）
        downloader = MediaDownloader(raw_cfg)
        forwarder = SocialForwarder(raw_cfg, downloader)

        def _do_translate():
            translated = forwarder._translate(post.text) if post.text else None
            if translated:
                post.extra["_translated"] = translated
            alts = collect_alts(post)
            alt_zh: dict = {}
            for idx, text in alts:
                zh = forwarder._translate(text)
                if zh:
                    alt_zh[idx] = zh
            if alts:
                post.extra["_alt_texts"] = {str(i): t for i, t in alts}
                if alt_zh:
                    post.extra["_alt_translated"] = {str(i): v for i, v in alt_zh.items()}
            return translated, alt_zh

        download_task = asyncio.to_thread(downloader.download, post)
        translate_task = asyncio.to_thread(_do_translate)
        _, (translated, alt_zh) = await asyncio.gather(download_task, translate_task)

        full_text = build_post_message(post, translated, alt_zh)

        # 4. 获取目标 Bot
        bots = qq_official.get_configured_bots()
        target_bot = None
        if app_id:
            for b in bots:
                if b.app_id == app_id:
                    target_bot = b
                    break
        if not target_bot and bots:
            target_bot = bots[0]

        # 若未提前注册，从配置动态构造
        if not target_bot:
            for b_cfg in getattr(cfg, "QQ_OFFICIAL_BOTS", []):
                if not app_id or b_cfg.get("app_id") == app_id:
                    target_bot = qq_official.QQOfficialBot(
                        name=b_cfg.get("name", "official_bot"),
                        app_id=b_cfg.get("app_id", ""),
                        client_secret=b_cfg.get("client_secret", ""),
                        target_openid=b_cfg.get("target_openid", ""),
                        group_openid=b_cfg.get("group_openid", ""),
                    )
                    break

        if not target_bot:
            log_all("⚠️ [社媒私聊解析] 未找到可用的 QQ 官方 Bot 实例", is_error=True)
            return

        # 5. 回复正文（包含标题、原帖正文与双语翻译）
        await target_bot.send_private_text(sender_openid, full_text)

        # 6. 回复所有高清图片 / 视频媒体附件
        for m in post.media:
            fp = m.local_path
            if fp and os.path.exists(fp):
                try:
                    with open(fp, "rb") as mf:
                        m_bytes = mf.read()
                    if m_bytes:
                        m_type = "image" if m.type == "image" else "video" if m.type == "video" else "record" if m.type == "audio" else "image"
                        await target_bot.send_media_file("users", sender_openid, m_type, m_bytes)
                except Exception as ex:
                    log_all(f"⚠️ [社媒私聊解析] 发送媒体附件异常: {ex}", is_error=True)

        # 7. 归档至数据库
        try:
            from src.social.archive import get_archive
            get_archive().add_post(post)
        except Exception as ex:
            log_all(f"⚠️ [社媒归档] 保存失败: {ex}", is_error=True)

        log_all(f"✅ [社媒私聊解析] 成功向用户 {sender_openid[:8]}… 回复 {post.platform} 动态: {post.author}")

    except Exception as e:
        log_all(f"⚠️ [社媒私聊解析] 失败: {e}", is_error=True)
        try:
            from src.platforms import qq_official
            bots = qq_official.get_configured_bots()
            if bots:
                await bots[0].send_private_text(sender_openid, f"❌ 社媒链接解析失败: {e}")
        except Exception:
            pass


def _trigger_social_reply_task(url: str, sender_openid: str, app_id: str = "") -> None:
    """在当前 loop 或后台线程中触发解析与回复任务。"""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        loop.create_task(_async_parse_and_reply_social(url, sender_openid, app_id))
    else:
        import threading
        t = threading.Thread(
            target=lambda: asyncio.run(_async_parse_and_reply_social(url, sender_openid, app_id)),
            daemon=True,
        )
        t.start()


def handle(text: str, sender_openid: str, app_id: str = "") -> str | None:
    """解析并执行指令或社媒链接解析。返回回复文本；不是指令/链接或无权限时返回 None。"""
    from src.logger import log_all

    content = (text or "").strip()
    if not content:
        return None

    # 1. 识别是否包含社媒链接
    social_match = _SOCIAL_URL_RE.search(content)
    if social_match:
        allow = allowed_senders()
        if not allow or sender_openid not in allow:
            log_all(f"🔒 拒绝未授权的社媒解析请求: {_clip(content, 40)}（来自 {sender_openid[:12]}…）",
                    is_error=True)
            return None

        url = social_match.group(0)
        _trigger_social_reply_task(url, sender_openid, app_id)
        log_all(f"🤖 [社媒私聊解析] 收到来自 {sender_openid[:8]}… 的社媒链接: {url[:50]}")
        return "🔍 已识别社媒链接，正在解析、提取原图/视频与 AI 翻译并回复给您…"

    # 2. 识别是否为 / 指令
    if not content.startswith("/"):
        return None

    allow = allowed_senders()
    if not allow or sender_openid not in allow:
        log_all(f"🔒 拒绝未授权的 Bot 指令: {_clip(content, 40)}（来自 {sender_openid[:12]}…）",
                is_error=True)
        return None

    name, _, args = content[1:].partition(" ")
    handler = _COMMANDS.get(name.lower().strip())
    if handler is None:
        return f"未知指令：/{_clip(name, 20)}\n发送 /help 查看可用指令。"

    try:
        reply = handler(args.strip())
    except Exception as e:
        log_all(f"⚠️ Bot 指令 /{name} 执行失败: {type(e).__name__}: {e}", is_error=True)
        return f"指令执行出错：{type(e).__name__}"

    log_all(f"🤖 Bot 指令 /{name} 已响应", is_debug=True)
    return _clip_reply(reply)


def _clip_reply(text: str) -> str:
    if len(text) <= MAX_REPLY_CHARS:
        return text
    return text[:MAX_REPLY_CHARS - 20] + "\n…（内容过长已截断）"

