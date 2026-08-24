"""
social/forwarder.py — 社交平台多通道推送分发中心

集成通道：
  1. Telegram Bot (HTML / MediaGroup)
  2. NapCat / Lagrange (OneBot11 协议，支持群推送)
  3. QQ 官方机器人 (单聊 / 频道)
  4. 统一 AI 翻译 (Gemini / 智谱 GLM / 硅基流动 / DeepSeek 轮换)
  5. SQLite 内容归档 (data/archive.db)
"""

import asyncio
import logging
import os
import subprocess

import config.config as cfg
from src.logger import log_all
from src.platforms import napcat, qq_official, tgbot
from src.utils import match_member_filter
from src.social.formatter import (
    build_live_end_message,
    build_post_message,
    collect_alts,
)
from src.social.models import Post
from src.social.settings import social_settings

log = logging.getLogger("social.forwarder")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class SendFailed(RuntimeError):
    """发送失败 —— 向上抛出，让调度器不标记已同步（下轮自动重试）。"""


class SocialForwarder:
    """社交平台多通道推送器。"""

    def __init__(self, config: dict, downloader=None):
        self._config = config
        self._dl = downloader

    @property
    def _cfg(self) -> dict:
        return social_settings(self._config)

    # ── 翻译适配 ─────────────────────────────────────────

    def _translate(self, text: str) -> str | None:
        """调用全局 AI 翻译引擎，支持自动降级与轮番模型重试。"""
        if not text or not text.strip():
            return None
        if not self._cfg.get("translate", True):
            return None
        try:
            from src import translator
            # 在后台线程中同步调用翻译逻辑
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # 若已在运行中的 loop，走线程池执行
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, translator.translate_text(text, "社媒", "偶像"))
                    out = future.result(timeout=15)
            else:
                out = asyncio.run(translator.translate_text(text, "社媒", "偶像"))

            if out and out.strip() and out.strip() != text.strip():
                log_all(f"✅ [社媒翻译] 翻译完成（{len(text)} 字 → {len(out.strip())} 字）", is_debug=True)
                return out.strip()
        except Exception as e:
            log_all(f"⚠️ [社媒翻译] AI 翻译失败，仅发送原文: {e}", is_debug=True)
        return None

    # ── 多通道统一广播 ───────────────────────────────────

    def _dispatch_async(self, coro):
        """在独立事件循环或当前环境中执行异步推送任务。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=60)
        else:
            return asyncio.run(coro)

    def forward_post(self, post: Post, target_channels: list[str] | None = None) -> None:
        """推送一条社交动态至各通道（支持定向通道列表）。"""
        # 1. AI 翻译（如明确跳过或已有翻译则不再调用）
        skip_translate = post.extra.get("_skip_translate", False)
        if skip_translate:
            translated = None
        else:
            translated = post.extra.get("_translated")
            if translated is None and post.text:
                translated = self._translate(post.text)
                if translated:
                    post.extra["_translated"] = translated

        # 图片 alt 描述翻译
        alt_zh: dict = {}
        if not skip_translate:
            alts = collect_alts(post)
            for idx, text in alts:
                zh = self._translate(text)
                if zh:
                    alt_zh[idx] = zh
            if alts:
                post.extra["_alt_texts"] = {str(i): t for i, t in alts}
                if alt_zh:
                    post.extra["_alt_translated"] = {str(i): v for i, v in alt_zh.items()}

        # 2. 生成消息文本
        is_raw = bool(post.extra.get("raw_message"))
        if is_raw:
            full_text = post.text
        else:
            full_text = build_post_message(post, translated, alt_zh)

        # 3. 异步分发至各通道（支持多维 Pub/Sub 订阅：平台开关 + social_filter + member_filter）
        m_name = post.extra.get("member_name")
        acc_name = post.extra.get("account") or post.author
        plat = post.platform.lower()

        async def _do_broadcast():
            any_success = False
            errors = []
            tasks = []

            # ── A. Telegram Bot 并发推送 ──────────────────────────────
            if getattr(cfg, "ENABLE_TG_BOT", False) and (not target_channels or any(c == "tg" or c.startswith("tg:") for c in target_channels)):
                for b in tgbot.get_configured_bots():
                    if not b.token or not b.target_chat:
                        continue
                    if target_channels:
                        tg_matches = [c for c in target_channels if c == "tg" or c == f"tg:{b.target_chat}" or c == f"tg:{getattr(b, 'name', '')}"]
                        if not tg_matches:
                            continue
                    else:
                        if not getattr(b, f"push_{plat}", True):
                            continue
                        if m_name and b.member_filter and not match_member_filter(m_name, b.member_filter):
                            continue
                        if b.social_filter and acc_name not in b.social_filter and (not m_name or m_name not in b.social_filter):
                            continue

                    async def _send_tg_post(target_bot=b):
                        try:
                            t_ok = await target_bot._post_message(target_bot.target_chat, full_text)
                            for m in post.media:
                                fp = m.local_path
                                if fp and os.path.exists(fp):
                                    if m.type == "image":
                                        try:
                                            with open(fp, "rb") as photo_file:
                                                await target_bot._bot.send_photo(chat_id=target_bot.target_chat, photo=photo_file)
                                        except Exception as ex:
                                            log_all(f"⚠️ TG Bot 发送图片异常: {ex}", is_error=True)
                                    elif m.type == "video":
                                        try:
                                            with open(fp, "rb") as video_file:
                                                await target_bot._bot.send_video(chat_id=target_bot.target_chat, video=video_file)
                                        except Exception as ex:
                                            log_all(f"⚠️ TG Bot 发送视频异常: {ex}", is_error=True)
                            return t_ok
                        except Exception as e:
                            errors.append(f"Telegram 推送失败: {e}")
                            return False

                    tasks.append(_send_tg_post())

            # ── B. NapCat QQ 群并发推送 ──────────────────────────────
            if getattr(cfg, "ENABLE_NAPCAT_QQ", False) and (not target_channels or any(c == "napcat" or c.startswith("napcat:") for c in target_channels)):
                chain = [{"type": "text", "data": {"text": full_text}}]
                for m in post.media:
                    fp = m.local_path
                    if fp and os.path.exists(fp):
                        abs_uri = "file:///" + os.path.abspath(fp).replace("\\", "/")
                        if m.type == "image":
                            chain.append({"type": "image", "data": {"file": abs_uri}})
                        elif m.type == "video":
                            chain.append({"type": "video", "data": {"file": abs_uri}})
                        elif m.type == "audio":
                            chain.append({"type": "record", "data": {"file": abs_uri}})

                for r in getattr(cfg, "NAPCAT_ROUTES", []):
                    gid = r.get("group_id")
                    if not gid:
                        continue
                    if target_channels:
                        nap_matches = [c for c in target_channels if c == "napcat" or c == f"napcat:{gid}"]
                        if not nap_matches:
                            continue
                    else:
                        if not r.get(f"push_{plat}", True):
                            continue
                        m_filters = r.get("member_filter") or []
                        if m_name and m_filters and not match_member_filter(m_name, m_filters):
                            continue
                        s_filters = r.get("social_filter") or []
                        if s_filters and acc_name not in s_filters and (not m_name or m_name not in s_filters):
                            continue

                    async def _send_napcat_post(target_gid=gid):
                        try:
                            return await napcat.send_qq_message(target_gid, chain)
                        except Exception as e:
                            errors.append(f"NapCat 推送失败 (群 {target_gid}): {e}")
                            return False

                    tasks.append(_send_napcat_post())

            # ── C. QQ 官方机器人并发推送 ─────────────────────────────
            if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False) and (not target_channels or any(c == "qq_official" or c.startswith("official:") for c in target_channels)):
                for bot in qq_official.get_configured_bots():
                    send_private = bool(bot.target_openid)
                    send_group = bool(getattr(bot, "group_openid", None))

                    if target_channels:
                        priv_keys = {
                            "qq_official",
                            f"official:{bot.name}",
                            f"official:{bot.name}:private",
                            f"official:{bot.app_id}",
                            f"official:{bot.app_id}:private",
                            f"official:{bot.target_openid}",
                        }
                        grp_keys = {
                            "qq_official",
                            f"official:{bot.name}",
                            f"official:{bot.name}:group",
                            f"official:{bot.app_id}",
                            f"official:{bot.app_id}:group",
                            f"official:{getattr(bot, 'group_openid', '')}",
                        }
                        send_private = send_private and any(k in target_channels for k in priv_keys if k)
                        send_group = send_group and any(k in target_channels for k in grp_keys if k)
                        if not send_private and not send_group:
                            continue
                    else:
                        if not getattr(bot, f"push_{plat}", True):
                            continue
                        if m_name and bot.member_filter and not match_member_filter(m_name, bot.member_filter):
                            continue
                        if bot.social_filter and acc_name not in bot.social_filter and (not m_name or m_name not in bot.social_filter):
                            continue

                    async def _send_official_post(b=bot, sp=send_private, sg=send_group):
                        try:
                            if sp and b.target_openid:
                                await b.send_private_text(b.target_openid, full_text)
                            if sg and getattr(b, "group_openid", None):
                                await b.send_group_text(b.group_openid, full_text)

                            for m in post.media:
                                fp = m.local_path
                                if fp and os.path.exists(fp):
                                    try:
                                        with open(fp, "rb") as mf:
                                            m_bytes = mf.read()
                                        if m_bytes:
                                            m_type = "image" if m.type == "image" else "video" if m.type == "video" else "record" if m.type == "audio" else "image"
                                            if sp and b.target_openid:
                                                await b.send_media_file("users", b.target_openid, m_type, m_bytes)
                                            if sg and getattr(b, "group_openid", None):
                                                await b.send_media_file("groups", b.group_openid, m_type, m_bytes)
                                    except Exception as ex:
                                        log_all(f"⚠️ QQ 官方 Bot 发送媒体异常: {ex}", is_error=True)
                            return True
                        except Exception as e:
                            errors.append(f"QQ 官方机器人推送失败 [{b.name}]: {e}")
                            return False

                    tasks.append(_send_official_post())

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                any_success = any(isinstance(r, bool) and r for r in results)

            return any_success, errors

        try:
            any_success, errors = self._dispatch_async(_do_broadcast())
            if errors:
                for err in errors:
                    log_all(f"⚠️ [社媒推送] {err}", is_error=True)
            log_all(f"✅ [社媒推送] {post.platform} 动态已分发: {post.author} - {post.post_id[:20]}", is_debug=True)
        except Exception as e:
            log_all(f"🔥 [社媒推送] 分发异常: {e}", is_error=True)

        # 4. 写入内容归档库
        try:
            from src.social import archive
            archive.get_archive().add_post(post)
        except Exception as e:
            log_all(f"⚠️ [社媒归档] 写入失败: {e}", is_debug=True)

    def send_recording(self, result) -> None:
        """发送「直播录制完成」通知并归档。"""
        result.delivery_succeeded = False
        msg = build_live_end_message(
            author=result.display_name,
            start_time=getattr(result, "start_str", ""),
            end_time=getattr(result, "end_str", ""),
            duration=getattr(result, "duration_str", ""),
            size=getattr(result, "size_str", ""),
            save_path=result.output_dir,
            part_count=len(result.parts),
            note=result.note,
        )

        acc_name = getattr(result, "account", "") or result.display_name

        async def _do_send():
            # 广播通知文本（支持 push_live & social_filter）
            if getattr(cfg, "ENABLE_TG_BOT", False):
                for b in tgbot.get_configured_bots():
                    if not b.target_chat or not getattr(b, "push_live", True):
                        continue
                    if b.social_filter and acc_name not in b.social_filter and result.display_name not in b.social_filter:
                        continue
                    await b._post_message(b.target_chat, msg)

            if getattr(cfg, "ENABLE_NAPCAT_QQ", False):
                for r in getattr(cfg, "NAPCAT_ROUTES", []):
                    gid = r.get("group_id")
                    if not gid or not r.get("push_live", True):
                        continue
                    s_filters = r.get("social_filter") or []
                    if s_filters and acc_name not in s_filters and result.display_name not in s_filters:
                        continue
                    await napcat.send_qq_message(gid, [{"type": "text", "data": {"text": msg}}])

            if getattr(cfg, "ENABLE_QQ_OFFICIAL_BOT", False):
                for bot in qq_official.get_configured_bots():
                    if not getattr(bot, "push_live", True):
                        continue
                    if bot.social_filter and acc_name not in bot.social_filter and result.display_name not in bot.social_filter:
                        continue
                    if bot.target_openid:
                        await bot.send_private_text(bot.target_openid, msg)
                    if getattr(bot, "group_openid", None):
                        await bot.send_group_text(bot.group_openid, msg)

        try:
            self._dispatch_async(_do_send())
            result.delivery_succeeded = True
            log_all(f"✅ [直播录制] 录制完成通知已分发: {result.display_name}", is_debug=True)
        except Exception as e:
            log_all(f"🔥 [直播录制] 录制通知分发失败: {e}", is_error=True)
