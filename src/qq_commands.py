# ============================================================
# qq_commands.py — 官方 QQ Bot 指令响应与社媒自动解析 (私聊 / 群聊 @机器人)
# ============================================================
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone

import config.config as cfg

JST = timezone(timedelta(hours=9))
MAX_REPLY_CHARS = 1400       # 官方 Bot 单条消息上限保守值
MAX_LIST_ITEMS = 5          # 列表类回复最多列几项

_SOCIAL_URL_RE = re.compile(
    r"https?://(?:[a-zA-Z0-9_-]+\.)*(?:twitter\.com|x\.com|instagram\.com|tiktok\.com|douyin\.com)/[^\s]+"
)


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}天{seconds % 86400 // 3600}小时"
    if seconds >= 3600:
        return f"{seconds // 3600}小时{seconds % 3600 // 60}分"
    if seconds >= 60:
        return f"{seconds // 60}分{seconds % 60}秒"
    return f"{seconds}秒"


def _jst_str(utc_str: str) -> str:
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return utc_str[:16]


def _clean_body(text: str, limit: int = 100) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    cleaned = " ".join(lines)
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


# ──────────────────────────────────────────────
# 各指令实现与功能菜单优化
# ──────────────────────────────────────────────

def _cmd_help(_args: str) -> str:
    return (
        "📖 坂道消息推送 Bot 指令菜单\n\n"
        "【📊 系统与监控】\n"
        "• /status — 查看程序运行状态、Token寿命与轮询周期\n"
        "• /members — 查看当前各平台已订阅监控的偶像名单\n"
        "• /ping — 快速测试机器人连接状态与网络延迟\n\n"
        "【🔍 消息与归档】\n"
        "• /latest [成员名] [条数] — 获取指定成员最新动态（如 /latest 冨里奈央 3）\n"
        "• /search <关键词> — 全文检索中日文归档（如 /search 富士急）\n"
        "• /stats — 查看归档统计概况与今日更新量\n\n"
        "【🌐 社媒自动解析与 AI 双语翻译】\n"
        "• 直接发送 X(Twitter) / Instagram / TikTok 动态链接，Bot 将自动提取高清原图/视频并附带 AI 双语翻译回复！\n\n"
        "💡 提示：支持中英文指令别名（如「状态」「最新」「搜索」），群聊中请 @机器人 使用。"
    )


def _cmd_ping(_args: str) -> str:
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    return f"🏓 Pong! 系统与 WebSocket 通信链路正常\n⏱️ 当前时间: {now_jst} (JST)"


def _cmd_status(_args: str) -> str:
    from config.credentials import get_token_remaining_seconds
    from src import health

    snap = health.get_tracker().snapshot()
    lines = [
        "📊 坂道推送助手 · 系统运行状态\n",
        f"⏱️ 运行时长: {_fmt_duration(snap['uptime_seconds'])} (第 {snap['cycle_count']} 轮巡查)"
    ]

    nxt = snap.get("next_cycle")
    if nxt:
        left = nxt["at_epoch"] - time.time()
        lines.append(f"⏰ 下次巡查: {_fmt_duration(left)} 后 {nxt.get('tag', '')}".strip())

    tokens = []
    for acc_id in cfg.ACCOUNTS:
        remaining = get_token_remaining_seconds(acc_id)
        if remaining is None:
            tokens.append(f"• {acc_id}: 未知")
        elif remaining <= 0:
            tokens.append(f"• {acc_id}: 已失效🔴")
        else:
            tokens.append(f"• {acc_id}: {_fmt_duration(remaining)}")
    if tokens:
        lines.append("\n🔑 账号状态:\n  " + "\n  ".join(tokens))

    channels = snap.get("channels") or {}
    if channels:
        ch_lines = [f"• {n}: 成功率 {c['success']}/{c['total']}" for n, c in channels.items()]
        lines.append("\n📢 推送通道:\n  " + "\n  ".join(ch_lines))

    bad = [m["name"] for m in snap.get("members", []) if not m["fetch_ok"] or not m["push_ok"]]
    if bad:
        lines.append(f"\n👥 成员监控: ⚠️ 存在异常 ({'、'.join(bad)})")
    else:
        lines.append(f"\n👥 成员监控: ✅ 全部 {len(cfg.MONITOR_LIST)} 位成员正常")

    persistent = [e for e in snap.get("errors", []) if e["tier"] == "PERSISTENT"]
    if persistent:
        lines.append(f"\n⚠️ 待处理告警: {len(persistent)} 条 ({_clip(persistent[-1]['msg'], 40)})")
    return "\n".join(lines).strip()


def _cmd_members(_args: str) -> str:
    if not cfg.MONITOR_LIST:
        return "👥 当前尚未配置任何监控成员。"
    
    group_map = {"nogizaka46": "乃木坂46", "sakurazaka46": "櫻坂46", "hinatazaka46": "日向坂46"}
    grouped: dict[str, list[str]] = {}
    for m in cfg.MONITOR_LIST:
        g_key = m.get("group") or m.get("group_type") or ""
        g_name = group_map.get(g_key, "其他成员")
        m_id = m.get("m_id") or m.get("id") or ""
        id_str = f" (id={m_id})" if m_id else ""
        grouped.setdefault(g_name, []).append(f"• {m.get('m_name') or m.get('name')}{id_str}")
    
    lines = [f"👥 监控偶像名单（共 {len(cfg.MONITOR_LIST)} 位）"]
    for g_name, mems in grouped.items():
        lines.append(f"\n【{g_name}】\n" + "\n".join(mems))
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
    hint = name_hint.replace(" ", "").lower()
    for d in dirs:
        if hint in d.replace("_", "").lower():
            return d
    return None


def _cmd_latest(args: str) -> str:
    from src import archive

    parts = args.split()
    count = 3
    if parts and parts[-1].isdigit():
        count = max(1, min(int(parts[-1]), MAX_LIST_ITEMS))
        parts = parts[:-1]
    name_hint = " ".join(parts)
    member = _resolve_member(name_hint)
    if not member:
        return f"❓ 没找到该成员「{name_hint}」的归档。\n💡 发送 /members 可查看当前已监控成员名单。"

    months = archive.list_months(member)
    if not months:
        return f"💬 【{member.replace('_', ' ')}】暂无历史归档记录。"

    msgs: list[dict] = []
    for m in months:
        msgs = archive.load_month(member, m["year"], m["month"]) + msgs
        if len(msgs) >= count:
            break
    picked = sorted(msgs, key=lambda x: x.get("updated_at", ""))[-count:]
    if not picked:
        return f"💬 【{member.replace('_', ' ')}】暂无历史归档记录。"

    lines = [f"💬 【{member.replace('_', ' ')}】最新 {len(picked)} 条动态：\n"]
    for i, msg in enumerate(reversed(picked), 1):
        time_str = _jst_str(msg.get("published_at") or msg.get("updated_at", ""))
        kind = {"picture": "🖼 [图片]", "image": "🖼 [图片]", "video": "🎬 [视频]", "voice": "🎤 [语音]"}.get(msg.get("type"), "💬 [消息]")
        body = _clean_body(msg.get("_translation") or msg.get("text") or "", 90) or "（多媒体附件）"
        lines.append(f"{i}. 📅 {time_str} {kind}\n   {body}\n")
    return "\n".join(lines).strip()


def _cmd_search(args: str) -> str:
    from src import archive

    query = args.strip()
    if not query:
        return "🔍 用法：/search <关键词>（例如：/search 富士急，中日文均可检索）"
    member = _resolve_member("")
    if not member:
        return "🔍 数据库中暂无归档内容。"

    hits = archive.search(member, query)
    if not hits:
        return f"🔍 关键词「{query}」没有命中相关内容。"
    lines = [f"🔍 检索关键词「{query}」· 命中 {len(hits)} 条（展示最新 {min(len(hits), MAX_LIST_ITEMS)} 条）：\n"]
    for i, msg in enumerate(hits[:MAX_LIST_ITEMS], 1):
        time_str = _jst_str(msg.get("published_at") or msg.get("updated_at", ""))
        kind = {"picture": "🖼", "image": "🖼", "video": "🎬", "voice": "🎤"}.get(msg.get("type"), "💬")
        body = _clean_body(msg.get("_translation") or msg.get("text") or "", 90) or "（多媒体附件）"
        lines.append(f"{i}. 📅 {time_str} {kind}\n   {body}\n")
    return "\n".join(lines).strip()


def _cmd_stats(_args: str) -> str:
    from src import archive

    members = archive.list_members()
    if not members:
        return "📚 数据库中暂无归档数据。"
    today = datetime.now(JST).strftime("%Y-%m-%d")
    lines = ["📚 系统归档统计概览\n"]
    total_all = 0
    for name in members[:MAX_LIST_ITEMS]:
        months = archive.list_months(name)
        total = sum(m["count"] for m in months)
        total_all += total
        today_n = archive.day_counts(name).get(today, 0)
        lines.append(f"• {name.replace('_', ' ')}: 共 {total} 条 / {len(months)} 个月" + (f" (今日 +{today_n})" if today_n else ""))
    if len(members) > MAX_LIST_ITEMS:
        lines.append(f"（另有 {len(members) - MAX_LIST_ITEMS} 位成员未列出）")
    lines.append(f"\n📈 全局累计已归档: {total_all} 条记录")
    return "\n".join(lines).strip()


_COMMANDS = {
    "help":     _cmd_help,
    "帮助":     _cmd_help,
    "菜单":     _cmd_help,
    "status":   _cmd_status,
    "状态":     _cmd_status,
    "运行状态": _cmd_status,
    "members":  _cmd_members,
    "成员":     _cmd_members,
    "监控":     _cmd_members,
    "latest":   _cmd_latest,
    "最新":     _cmd_latest,
    "最新消息": _cmd_latest,
    "search":   _cmd_search,
    "搜索":     _cmd_search,
    "查询":     _cmd_search,
    "stats":    _cmd_stats,
    "统计":     _cmd_stats,
    "ping":     _cmd_ping,
    "测试":     _cmd_ping,
}


# ──────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────

def allowed_senders() -> set[str]:
    """允许使用指令的 openid 白名单（统一转大写以便大小写无关匹配）。"""
    explicit = getattr(cfg, "QQ_COMMANDS_ALLOW", None) or []
    if explicit:
        return {str(x).strip().upper() for x in explicit if str(x).strip()}
    res = set()
    for b in getattr(cfg, "QQ_OFFICIAL_BOTS", []):
        if b.get("target_openid"):
            res.add(b["target_openid"].strip().upper())
        if b.get("group_openid"):
            res.add(b["group_openid"].strip().upper())
    return res


async def _async_parse_and_reply_social(url: str, target_id: str, scope: str = "users", app_id: str = ""):
    """后台任务：解析社媒链接、下载多媒体、AI 翻译并回复（支持单聊与群聊）。"""
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

        # 2. 并发执行：媒体多线程下载 与 异步 AI 智能翻译（带 10s 快速超时）
        downloader = MediaDownloader(raw_cfg)
        forwarder = SocialForwarder(raw_cfg, downloader)

        async def _do_translate_async():
            t_res = None
            if post.text:
                try:
                    from src import translator
                    t_res = await asyncio.wait_for(translator.translate_text(post.text, "社媒", "偶像"), timeout=10.0)
                    if t_res and t_res.strip() != post.text.strip():
                        post.extra["_translated"] = t_res.strip()
                except Exception as ex:
                    log_all(f"⚠️ [社媒翻译] AI 翻译跳过/超时: {ex}", is_debug=True)
            return t_res

        download_task = asyncio.to_thread(downloader.download, post)
        translate_task = _do_translate_async()
        _, translated = await asyncio.gather(download_task, translate_task)

        alt_zh: dict = {}
        alts = collect_alts(post)
        if alts:
            post.extra["_alt_texts"] = {str(i): t for i, t in alts}

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
            log_all("⚠️ [社媒解析] 未找到可用的 QQ 官方 Bot 实例", is_error=True)
            return

        # 5. 回复正文（包含标题、原帖正文与双语翻译）
        log_all(f"📤 [社媒解析] 开始向 {scope}:{target_id[:8]}… 发送动态正文与 {len(post.media)} 个媒体附件...")
        if scope == "groups":
            await target_bot.send_group_text(target_id, full_text)
        else:
            await target_bot.send_private_text(target_id, full_text)

        # 6. 回复所有高清图片 / 视频媒体附件
        media_ok = 0
        for m in post.media:
            fp = m.local_path
            if fp and os.path.exists(fp):
                try:
                    with open(fp, "rb") as mf:
                        m_bytes = mf.read()
                    if m_bytes:
                        m_type = "image" if m.type == "image" else "video" if m.type == "video" else "record" if m.type == "audio" else "image"
                        fname = os.path.basename(fp)
                        sent = await target_bot.send_media_file(scope, target_id, m_type, m_bytes, filename=fname)
                        if sent:
                            media_ok += 1
                        else:
                            log_all(f"⚠️ [社媒解析] 媒体附件 {fname} 发送未成功 (API 返回 None)", is_error=True)
                except Exception as ex:
                    log_all(f"⚠️ [社媒解析] 发送媒体附件异常: {ex}", is_error=True)

        # 7. 归档至数据库
        try:
            from src.social.archive import get_archive
            get_archive().add_post(post)
        except Exception as ex:
            log_all(f"⚠️ [社媒归档] 保存失败: {ex}", is_error=True)

        log_all(f"✅ [社媒解析] 成功向 {scope}:{target_id[:8]}… 回复 {post.platform} 动态: {post.author} (发送 {media_ok}/{len(post.media)} 个媒体)")

    except Exception as e:
        log_all(f"⚠️ [社媒解析] 失败: {e}", is_error=True)
        try:
            from src.platforms import qq_official
            bots = qq_official.get_configured_bots()
            if bots:
                err_msg = f"❌ 社媒链接解析失败: {e}"
                if scope == "groups":
                    await bots[0].send_group_text(target_id, err_msg)
                else:
                    await bots[0].send_private_text(target_id, err_msg)
        except Exception:
            pass


def _trigger_social_reply_task(url: str, target_id: str, scope: str = "users", app_id: str = "") -> None:
    """在当前 loop 或后台线程中触发解析与回复任务。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        loop.create_task(_async_parse_and_reply_social(url, target_id, scope=scope, app_id=app_id))
    else:
        import threading
        t = threading.Thread(
            target=lambda: asyncio.run(_async_parse_and_reply_social(url, target_id, scope=scope, app_id=app_id)),
            daemon=True,
        )
        t.start()


def handle(text: str, sender_openid: str, app_id: str = "", group_openid: str = "") -> str | None:
    """解析并执行指令或社媒链接解析。返回回复文本；不是指令/链接或无权限时返回 None。"""
    from src.logger import log_all

    content = (text or "").strip()
    if not content:
        return None

    # 1. 权限检查（统一大写比对，白名单为空或不在白名单内均静默不响应）
    allow = allowed_senders()
    if not allow:
        return None

    sender_norm = (sender_openid or "").strip().upper()
    group_norm = (group_openid or "").strip().upper()

    if sender_norm not in allow and (not group_norm or group_norm not in allow):
        return None

    target_id = group_openid if group_openid else sender_openid
    scope = "groups" if group_openid else "users"

    # 2. 识别是否包含社媒链接
    social_match = _SOCIAL_URL_RE.search(content)
    if social_match:
        url = social_match.group(0)
        _trigger_social_reply_task(url, target_id, scope=scope, app_id=app_id)
        log_all(f"🤖 [社媒解析] 收到来自 {scope}:{target_id[:8]}… 的社媒链接: {url[:50]}")
        return "🔍 已识别社媒链接，正在解析、提取原图/视频与 AI 双语翻译并回复给您…"

    # 3. 指令解析（支持 / 开头 或 预定义中文简写指令）
    cmd_name = ""
    cmd_args = ""
    if content.startswith("/"):
        cmd_name, _, cmd_args = content[1:].partition(" ")
    else:
        # 支持不带斜杠的中文指令（如"菜单"、"状态"、"最新 冨里奈央"）
        first_word, _, rest = content.partition(" ")
        if first_word.lower() in _COMMANDS:
            cmd_name = first_word
            cmd_args = rest

    if not cmd_name:
        return None

    cmd_key = cmd_name.lower().strip()
    handler = _COMMANDS.get(cmd_key)
    if handler is not None:
        try:
            reply = handler(cmd_args.strip())
        except Exception as e:
            log_all(f"⚠️ Bot 指令 /{cmd_key} 执行失败: {type(e).__name__}: {e}", is_error=True)
            return f"⚠️ 指令执行出错: {type(e).__name__}"

        log_all(f"🤖 Bot 指令 /{cmd_key} 已成功响应", is_debug=True)
        return _clip_reply(reply)
    else:
        # 收到未知指令
        return (
            f"❓ 未知指令「/{_clip(cmd_name, 20)}」\n\n"
            f"💡 您可以发送 /help 或「菜单」查看完整可用功能列表。"
        )


def _clip_reply(text: str) -> str:
    if len(text) <= MAX_REPLY_CHARS:
        return text
    return text[:MAX_REPLY_CHARS - 30] + "\n\n…（内容过长已截断）"


