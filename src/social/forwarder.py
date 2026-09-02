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
from dataclasses import dataclass
import logging
import os
import subprocess  # nosec B404

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


@dataclass(frozen=True)
class SocialDeliveryResult:
    """一条社交动态的路由投递结果。

    ``complete`` 与 ``any_success`` 刻意分开：部分路由成功时，成功路由可以
    保留在数据库中跳过，但失败路由仍需在下一轮补偿，因此不能把部分成功当作
    整条内容已经完成。
    """

    outcome: str
    matched_routes: int
    attempted_routes: int
    success_routes: int
    failed_routes: int
    skipped_routes: int = 0
    errors: tuple[str, ...] = ()

    @property
    def any_success(self) -> bool:
        return self.success_routes > 0

    @property
    def complete(self) -> bool:
        """是否可以让 fetcher 将整条内容标记为已同步。"""
        return self.outcome in {"success", "no_route", "already_delivered"}


class SocialForwarder:
    """社交平台多通道推送器。"""

    def __init__(self, config: dict, downloader=None, store=None):
        self._config = config
        self._dl = downloader
        self._store = store
        self._last_delivery_result: SocialDeliveryResult | None = None

    @property
    def last_delivery_result(self) -> SocialDeliveryResult | None:
        """最近一次 ``forward_post()`` 的结果。

        社媒管理器在持有共享转发锁时读取该属性，因此不会与另一个平台的
        转发结果交叉。外部调用方也可以用它解释 bool 返回值的具体原因。
        """
        return self._last_delivery_result

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
                    out = future.result(timeout=40)
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

    def forward_post(self, post: Post, target_channels: list[str] | None = None) -> bool:
        """推送一条社交动态至各通道，返回是否已安全完成。

        所有匹配路由成功，或没有匹配路由（配置决定的跳过）时返回 ``True``。
        部分成功仍返回 ``False``，这样 fetcher 不会过早标记整条内容已同步，
        下一轮可以只补发失败路由。具体结果可从 ``last_delivery_result`` 读取。
        """
        self._last_delivery_result = None

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
        delivered_routes = (
            self._store.delivered_routes(plat, post.post_id) if self._store else set()
        )

        async def _do_broadcast():
            errors = []
            tasks = []
            task_route_ids = []
            matched_routes = 0
            skipped_routes = 0

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
                    route_id = f"tg:{getattr(b, 'name', b.target_chat)}"
                    matched_routes += 1
                    if route_id in delivered_routes:
                        skipped_routes += 1
                        continue

                    async def _send_tg_post(target_bot=b):
                        try:
                            t_ok = await target_bot._post_message(target_bot.target_chat, full_text)
                            if not t_ok:
                                return False
                            for m in post.media:
                                fp = m.local_path
                                if fp and os.path.exists(fp):
                                    if m.type == "image":
                                        try:
                                            with open(fp, "rb") as photo_file:
                                                await target_bot._bot.send_photo(chat_id=target_bot.target_chat, photo=photo_file)
                                        except (OSError, ValueError) as ex:
                                            log_all(f"⚠️ TG Bot 发送图片失败: {type(ex).__name__}", is_error=True)
                                            return False
                                    elif m.type == "video":
                                        try:
                                            with open(fp, "rb") as video_file:
                                                await target_bot._bot.send_video(chat_id=target_bot.target_chat, video=video_file)
                                        except (OSError, ValueError) as ex:
                                            log_all(f"⚠️ TG Bot 发送视频失败: {type(ex).__name__}", is_error=True)
                                            return False
                            return t_ok
                        except Exception as e:
                            bot_label = f"{target_bot.remark} ({target_bot.name})" if getattr(target_bot, "remark", None) else target_bot.name
                            errors.append(f"Telegram 推送失败 [{bot_label}]: {type(e).__name__}")
                            return False

                    tasks.append(_send_tg_post())
                    task_route_ids.append(route_id)

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
                    route_id = f"napcat:{gid}"
                    matched_routes += 1
                    if route_id in delivered_routes:
                        skipped_routes += 1
                        continue

                    async def _send_napcat_post(route=r, target_gid=gid):
                        try:
                            return await napcat.send_qq_message(target_gid, chain)
                        except Exception as e:
                            r_label = f"{route.get('remark')} ({target_gid})" if route.get("remark") else f"群 {target_gid}"
                            errors.append(f"NapCat 推送失败 [{r_label}]: {type(e).__name__}")
                            return False

                    tasks.append(_send_napcat_post())
                    task_route_ids.append(route_id)

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
                    route_id = f"official:{bot.name}"
                    matched_routes += 1
                    if route_id in delivered_routes:
                        skipped_routes += 1
                        continue

                    async def _send_official_post(b=bot, sp=send_private, sg=send_group):
                        try:
                            if sp and b.target_openid:
                                if not await b.send_private_text(b.target_openid, full_text):
                                    return False
                            if sg and getattr(b, "group_openid", None):
                                if not await b.send_group_text(b.group_openid, full_text):
                                    return False

                            for m in post.media:
                                fp = m.local_path
                                if fp and os.path.exists(fp):
                                    try:
                                        with open(fp, "rb") as mf:
                                            m_bytes = mf.read()
                                        if m_bytes:
                                            m_type = "image" if m.type == "image" else "video" if m.type == "video" else "record" if m.type == "audio" else "image"
                                            # QQ 官方 Bot 直传若不带 file_name，客户端会显示“未命名”。
                                            # 使用已落地文件的 basename，上传层再负责安全清洗与扩展名补齐。
                                            media_filename = os.path.basename(fp)
                                            if sp and b.target_openid:
                                                await b.send_media_file(
                                                    "users", b.target_openid, m_type, m_bytes,
                                                    filename=media_filename,
                                                )
                                            if sg and getattr(b, "group_openid", None):
                                                await b.send_media_file(
                                                    "groups", b.group_openid, m_type, m_bytes,
                                                    filename=media_filename,
                                                )
                                    except (OSError, ValueError) as ex:
                                        log_all(f"⚠️ QQ 官方 Bot 发送媒体失败: {type(ex).__name__}", is_error=True)
                                        return False
                            return True
                        except Exception as e:
                            b_label = f"{b.remark} ({b.name})" if getattr(b, "remark", None) else b.name
                            errors.append(f"QQ 官方机器人推送失败 [{b_label}]: {type(e).__name__}")
                            return False

                    tasks.append(_send_official_post())
                    task_route_ids.append(route_id)

            if not tasks:
                return {
                    "results": (),
                    "errors": tuple(errors),
                    "matched_routes": matched_routes,
                    "skipped_routes": skipped_routes,
                }

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for route_id, result in zip(task_route_ids, results):
                if isinstance(result, Exception):
                    errors.append(f"路由任务异常: {type(result).__name__}")
                if self._store:
                    self._store.mark_route_result(
                        plat, post.post_id, route_id, result is True,
                        "" if result is True else type(result).__name__,
                    )
            return {
                "results": tuple(results),
                "errors": tuple(errors),
                "matched_routes": matched_routes,
                "skipped_routes": skipped_routes,
            }

        try:
            broadcast = self._dispatch_async(_do_broadcast())
            results = tuple(broadcast.get("results", ()))
            errors = tuple(broadcast.get("errors", ()))
            matched_routes = int(broadcast.get("matched_routes", 0))
            skipped_routes = int(broadcast.get("skipped_routes", 0))
            attempted_routes = len(results)
            success_routes = sum(result is True for result in results)
            failed_routes = attempted_routes - success_routes

            if matched_routes == 0:
                outcome = "no_route"
            elif attempted_routes == 0 and skipped_routes == matched_routes:
                outcome = "already_delivered"
            elif failed_routes == 0:
                outcome = "success"
            elif success_routes > 0:
                outcome = "partial"
            else:
                outcome = "failed"

            self._last_delivery_result = SocialDeliveryResult(
                outcome=outcome,
                matched_routes=matched_routes,
                attempted_routes=attempted_routes,
                success_routes=success_routes,
                failed_routes=failed_routes,
                skipped_routes=skipped_routes,
                errors=errors,
            )
            if errors:
                for err in errors:
                    log_all(f"⚠️ [社媒推送] {err}", is_error=True)

            post_ref = f"{post.author} - {post.post_id[:20]}"
            if outcome == "no_route":
                log_all(
                    f"⏭️ [社媒推送] {post.platform} 动态跳过 | 无匹配路由 | {post_ref}",
                )
            elif outcome == "already_delivered":
                log_all(
                    f"✅ [社媒推送] {post.platform} 动态已完成 | 路由已投递 "
                    f"{skipped_routes}/{matched_routes} | {post_ref}",
                    is_debug=True,
                )
            elif outcome == "success":
                log_all(
                    f"✅ [社媒推送] {post.platform} 动态已分发 | 路由成功 "
                    f"{success_routes}/{matched_routes} | {post_ref}",
                    is_debug=True,
                )
            elif outcome == "partial":
                log_all(
                    f"⚠️ [社媒推送] {post.platform} 动态部分成功 | 路由成功 "
                    f"{success_routes}/{matched_routes} | 失败 {failed_routes} | "
                    f"下轮仅重试失败路由 | {post_ref}",
                    is_error=True,
                )
            else:
                log_all(
                    f"⚠️ [社媒推送] {post.platform} 动态全部目标失败 | "
                    f"路由 0/{matched_routes} | 下轮重试 | {post_ref}",
                    is_error=True,
                )
        except Exception as e:
            self._last_delivery_result = SocialDeliveryResult(
                outcome="error",
                matched_routes=0,
                attempted_routes=0,
                success_routes=0,
                failed_routes=0,
                errors=(f"{type(e).__name__}: {e}",),
            )
            log_all(f"🔥 [社媒推送] 分发异常: {type(e).__name__}", is_error=True)
            return False

        # 4. 写入内容归档库
        try:
            from src.social import archive
            archive.get_archive().add_post(post)
        except (OSError, ValueError) as e:
            log_all(f"⚠️ [社媒归档] 写入失败: {type(e).__name__}", is_debug=True)
        return bool(self._last_delivery_result and self._last_delivery_result.complete)

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
