# ============================================================
# fetcher.py — 核心抓取逻辑：拉取成员消息并分发推送
# ============================================================
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

import config.config as cfg
from src import archive
from src.logger import format_httpx_error, log_all, log_response
from config.credentials import (
    ACCOUNT_CREDS, get_file_lock, get_mobile_api_base, get_mobile_headers,
    get_web_headers, refresh_mobile_token, refresh_token, write_time_record,
    get_refresh_state, is_account_fetch_available,
)
from src.dedup import load_sent_ids, save_sent_id
from src.translator import translate_text_with_model
from src.notifier import matching_member_route_ids, send_member_message_detailed
from src.delivery_state import (
    mark_pending_routes,
    mark_successful_routes,
    pending_routes_for_member,
    reconcile_pending_routes,
    successful_routes,
)
from src.health import ErrorTier, get_tracker as _health_tracker
from src.platforms.napcat import build_message_chain

# ---- 模块级状态（由 initialize() 在 main() 中注入） ----
_http_client: httpx.AsyncClient  = None   # type: ignore
_semaphore:   asyncio.Semaphore  = None   # type: ignore

MAX_FETCH_ATTEMPTS = 2
RETRY_BASE_DELAY = 2.0   # 基础退避秒数，实际延迟 = base * 2^(attempt-1) + jitter


@dataclass
class _DeliveryLaneContext:
    """单成员本轮的路由 lane 协调状态。

    ``blocked_routes`` 只在当前成员循环内阻止同一路由越过失败消息；
    每条待处理记录同时写入 SQLite，下一轮/重启后可继续补偿。
    """

    blocked_routes: set[str] = field(default_factory=set)
    unresolved: dict[str, str] = field(default_factory=dict)
    handled_count: int = 0


_delivery_lane_context: ContextVar[_DeliveryLaneContext | None] = ContextVar(
    "delivery_lane_context", default=None
)


async def _maybe_alert_napcat_lane_failure(
    member: dict,
    route_ids: set[str],
    errors: dict[str, str],
) -> None:
    """熔断后通过仍可用的告警路由通知 NapCat lane 积压。"""

    napcat_routes = sorted(route for route in route_ids if route.startswith("napcat:"))
    if not napcat_routes:
        return
    try:
        from src.platforms.napcat import get_send_gate_snapshot

        gate = get_send_gate_snapshot()
        threshold = int(gate.get("failure_threshold") or 3)
        streak = int(gate.get("failure_streak") or 0)
        blocked = bool(gate.get("blocked_until"))
        if streak < threshold and not blocked:
            return
        tracker = _health_tracker()
        cooldown = getattr(cfg, "ALERT_COOLDOWN_SECONDS", 3600)
        if not tracker.alert_due("napcat_delivery", cooldown):
            return
        from src.notifier import send_alert_message

        member_name = str(member.get("m_name") or member.get("name") or "未知成员")
        error_codes = sorted({errors.get(route) for route in napcat_routes if errors.get(route)})
        error_text = ", ".join(error_codes[:3]) or str(gate.get("last_error") or "delivery_failed")
        text = (
            f"NapCat 路由已连续失败（成员：{member_name}，路由：{', '.join(napcat_routes[:3])}）。"
            f"错误：{error_text}；当前待补偿消息将按顺序保留。请检查 NapCat/QQNT 会话。"
        )
        try:
            delivered = await asyncio.wait_for(send_alert_message(0, text), timeout=10.0)
        except Exception as exc:
            log_all(
                f"⚠️ NapCat 投递告警发送异常 | error={type(exc).__name__}",
                is_warning=True,
            )
        else:
            log_all(
                f"{'✅' if delivered else '⚠️'} NapCat 投递告警{'已发送' if delivered else '未送达'} | "
                f"member={member_name}",
                is_debug=bool(delivered),
                is_warning=not delivered,
            )
    except Exception as exc:
        # 告警属于旁路能力，任何配置/状态读取异常不能影响 lane 推进。
        log_all(
            f"⚠️ NapCat 投递告警准备失败 | error={type(exc).__name__}",
            is_warning=True,
        )


def initialize(client: httpx.AsyncClient, semaphore: asyncio.Semaphore) -> None:
    """注入共享的 HTTP 客户端和并发信号量。"""
    global _http_client, _semaphore
    _http_client = client
    _semaphore   = semaphore


async def fetch_member_messages(
    member: dict,
    account_cfg: dict | None = None,
    backtrack_hours: int | None = None,
    skip_publish_types: tuple | list | set | None = None,
):
    """公开接口：抓取单个成员消息。"""
    return await _fetch_member_messages(
        member,
        account_cfg=account_cfg,
        backtrack_hours=backtrack_hours,
        skip_publish_types=skip_publish_types,
    )


async def push_member_messages(member: dict, new_msgs: list,
                               id_list: list, id_set: set,
                               l_time_ref: list,
                               time_file: str, file_lock) -> bool:
    """公开接口：推送单个成员消息并更新状态。"""
    return await _push_member_messages(
        member, new_msgs, id_list, id_set, l_time_ref, time_file, file_lock
    )


# ──────────────────────────────────────────────
# 单条消息处理
# ──────────────────────────────────────────────
async def _handle_message(member: dict, msg: dict,
                           id_list: list, id_set: set, l_time_ref: list) -> bool:
    """
    翻译 → 推送各通道 → 记录状态。
    返回 True 表示本条所有匹配路由均完成；在路由 lane 模式下，False
    只表示本条仍有待补偿路由，不会阻止其它健康 lane 处理后续消息。
    l_time_ref 是单元素列表，用于在此函数内修改外层的 l_time 变量。
    """
    m_name     = member["m_name"]
    group_type = member["group_type"]
    m_id       = member["m_id"]

    updated       = msg.get("updated_at", "")
    msg_id        = str(msg.get("id") or updated)
    original_text = msg.get("text", "")

    lane_context = _delivery_lane_context.get()

    if msg_id in id_set:
        l_time_ref[0] = updated
        if lane_context is not None:
            lane_context.handled_count += 1
            lane_context.unresolved.pop(msg_id, None)
        return True

    # 翻译
    translated = ""
    trans_model = ""
    if cfg.ENABLE_TRANSLATION and original_text.strip():
        raw, model_name = await translate_text_with_model(original_text, m_name, group_type)
        if raw.startswith("[翻译失败") or raw.startswith("[消息过长"):
            log_all(f"⚠️ [成员ID: {m_id} | 名字: {m_name}] 翻译失败，仅推送原文 ({raw})", is_error=True)
        elif raw.strip() == original_text.strip():
            log_all(f"ℹ️ [成员ID: {m_id} | 名字: {m_name}] 翻译结果与原文一致，跳过翻译推送", is_debug=True)
        else:
            translated = raw
            trans_model = model_name or ""
            # 日志只保留可定位的元数据，不输出翻译正文：正文可能包含隐私，
            # 且多行内容会把一条事件拆成大量难以检索的日志行。
            log_all(
                f"🌐 [成员ID: {m_id} | 名字: {m_name}] 翻译完成 | "
                f"模型: {trans_model or '未知'} | 原文: {len(original_text)} 字 | "
                f"译文: {len(translated)} 字",
                is_debug=True,
            )

    # 归档（先行下载媒体并持久化，保证本地素材就绪供各推送通道复用）
    await archive.archive_message(member, msg, translated)

    # 推送各通道（若含有媒体，各通道直接复用本地素材，免去重复网络请求）
    chain = build_message_chain(m_name, updated, msg, translated, model_name=trans_model)
    delivered_routes = successful_routes(group_type, m_id, msg_id)
    skip_routes = set(delivered_routes)
    if lane_context is not None:
        # 同一成员本轮内，某路由一旦在较早消息失败，就不能越过它发送
        # 后续消息；其它路由不受影响。
        skip_routes.update(lane_context.blocked_routes)
    report = await send_member_message_detailed(
        member, chain, skip_route_ids=skip_routes
    )
    successful = {attempt.route_id for attempt in report.attempts if attempt.ok}
    attempted = {attempt.route_id for attempt in report.attempts}
    failed_attempts = {
        attempt.route_id: str(attempt.error_code or "delivery_failed")
        for attempt in report.attempts
        if not attempt.ok
    }

    if lane_context is None:
        # 兼容直接调用 _handle_message 的旧入口；真正的成员循环会在
        # 下方使用 route lane 语义，不改变旧测试/插件的布尔契约。
        mark_successful_routes(group_type, m_id, msg_id, successful)
        if report.failure_count:
            log_all(
                f"⚠️ [成员ID: {m_id} | 名字: {m_name}] 消息推送失败，保留时间戳等待下次重试",
                is_error=True,
            )
            return False
        log_all(
            f"📤 [成员ID: {m_id} | 名字: {m_name}] 成功分发 1 条消息 (ID: {msg_id})",
            is_debug=True,
        )
        save_sent_id(group_type, m_id, msg_id, id_list, id_set)
        l_time_ref[0] = updated
        delay = max(0, cfg.QQ_SEND_INTERVAL + random.uniform(-0.3, 0.5))  # nosec B311
        await asyncio.sleep(delay)
        return True

    matched = set(report.matched_route_ids)
    if not matched:
        # 兼容自定义/旧版 notifier 返回只有 attempts 的报告。
        matched.update(attempted)
    completed = set(delivered_routes) | successful
    pending = matched - completed
    if successful:
        mark_successful_routes(
            group_type, m_id, msg_id, successful, message_time=updated
        )
    if pending:
        mark_pending_routes(
            group_type,
            m_id,
            msg_id,
            updated,
            pending,
            errors=failed_attempts,
            attempted_route_ids=set(failed_attempts),
        )
        # 失败路由及其后续消息都留在该 lane 的队列中，不能乱序越过。
        lane_context.blocked_routes.update(failed_attempts)
        lane_context.unresolved[msg_id] = str(updated or "")
        log_all(
            f"⚠️ [成员ID: {m_id} | 名字: {m_name}] 消息部分投递，"
            f"健康路由继续，待补偿 {len(pending)} 个路由 (ID: {msg_id})",
            is_error=True,
        )
        await _maybe_alert_napcat_lane_failure(member, pending, failed_attempts)
    else:
        lane_context.unresolved.pop(msg_id, None)
        log_all(
            f"📤 [成员ID: {m_id} | 名字: {m_name}] 成功分发 1 条消息 (ID: {msg_id})",
            is_debug=True,
        )
        save_sent_id(group_type, m_id, msg_id, id_list, id_set)

    lane_context.handled_count += 1
    l_time_ref[0] = updated
    delay = max(0, cfg.QQ_SEND_INTERVAL + random.uniform(-0.3, 0.5))  # nosec B311 -- 基于配置值随机微调
    if report.attempts:
        await asyncio.sleep(delay)
    return not pending


# ──────────────────────────────────────────────
# 单成员轮询
# ──────────────────────────────────────────────
async def _fetch_member_messages(
    member: dict,
    account_cfg: dict | None = None,
    backtrack_hours: int | None = None,
    skip_publish_types: tuple | list | set | None = None,
):
    """
    Phase 1（并发抓取）：读取时间戳 → API 请求（含 401 续期/重试）→ 排序过滤。
    返回 (new_msgs, id_list, id_set, l_time_ref, time_file, file_lock) 或 None。
    """
    global _http_client
    account_id   = member.get("account_id") or ""
    group_type   = member.get("group_type") or ""
    m_id         = member.get("m_id") or ""
    m_name       = member.get("m_name") or ""

    if not account_id or not m_id:
        # 该成员未绑定 Message 账号（例如纯社媒/博客监控成员），静默跳过 Message 抓取
        return None

    # 只推 TG 的成员可以没有 QQ 群；0 表示告警不走 NapCat
    target_groups = member.get("target_groups") or []
    target_group  = target_groups[0] if target_groups else 0

    cred = ACCOUNT_CREDS.get(account_id)
    if not cred:
        log_all(f"🚨 [成员ID: {m_id} | 名字: {m_name}] 账号 {account_id} 无可用凭据", is_error=True)
        _health_tracker().record_member_fetch(m_name, False, ErrorTier.PERSISTENT, f"账号 {account_id} 无可用凭据")
        return None

    # 主动续期失败后禁止继续拿旧 Token 扫描所有成员；网络型失败会在冷却
    # 到期后自动恢复，认证型失败则等待用户更新凭据并由管理端清除状态。
    fetch_available, refresh_reason = is_account_fetch_available(account_id)
    if not fetch_available:
        refresh_state = get_refresh_state(account_id)
        failure_kind = refresh_state.get("kind", "unknown")
        tier = ErrorTier.PERSISTENT if failure_kind == "credential_invalid" else ErrorTier.TRANSIENT
        message = f"账号 {account_id} {refresh_reason}，跳过成员抓取"
        log_all(f"⏸️ [成员ID: {m_id} | 名字: {m_name}] {message}", is_debug=True)
        _health_tracker().record_member_fetch(m_name, False, tier, message)
        return None

    # 优先从 SQLite 数据库获取时间戳水位线，旧磁盘文本文件作为平滑过渡
    l_time = archive.get_timeline_watermark(group_type, m_id)
    time_dir = getattr(cfg, "TIME_RECORD_DIR", "")
    time_file = os.path.join(time_dir, f"time_{group_type}_{m_id}.txt") if time_dir else ""
    file_lock = get_file_lock(time_file or f"{group_type}_{m_id}")

    if not l_time and time_file and os.path.exists(time_file):
        try:
            with open(time_file, "r", encoding="utf-8") as f:
                l_time = f.read().strip()
            if l_time:
                archive.set_timeline_watermark(group_type, m_id, l_time)
        except (OSError, UnicodeError) as exc:
            log_all(f"⚠️ [成员ID: {m_id} | 名字: {m_name}] 读取旧时间水位线失败: {type(exc).__name__}: {exc}", is_error=True)

    # 路由 lane 的失败消息不能依赖上游 timeline 长期保留；优先把最早
    # 待补偿时间拉回查询窗口，并在成功响应后从本地归档索引恢复消息。
    # 路由配置可能在 NapCat 故障期间被关闭、删除或改过滤条件。先同步
    # 活跃 route_id：停用路由的历史记录保留在 SQLite，但进入 suspended
    # 状态，不再把成员时间水位永久拉回；恢复同一 route_id 时自动重试。
    active_route_ids = set(matching_member_route_ids(member))
    reconcile_pending_routes(group_type, m_id, active_route_ids)
    pending_rows = pending_routes_for_member(group_type, m_id)
    pending_message_ids = [str(row.get("message_id") or "") for row in pending_rows]
    pending_times = [str(row.get("message_time") or "") for row in pending_rows if row.get("message_time")]
    if pending_times and (not l_time or min(pending_times) < str(l_time)):
        l_time = min(pending_times)

    is_first_fetch = False
    if not l_time:
        is_first_fetch = True
        effective_backtrack = backtrack_hours if backtrack_hours is not None else cfg.BACKTRACK_HOURS
        l_time = (
            datetime.now(timezone.utc) - timedelta(hours=effective_backtrack)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── 订阅感知优化：未订阅/已离线成员跳过日常实时抓取 ──
    from src.member_directory import is_member_active_subscription, get_member_subscription
    sub_is_active = is_member_active_subscription(account_id, m_id)
    if sub_is_active is False:
        if is_first_fetch:
            log_all(f"ℹ️ [成员ID: {m_id} | 名字: {m_name}] 处于曾订阅/离线状态，首次巡查尝试建立历史归档 (past_messages)", is_debug=True)
        else:
            sub_info = get_member_subscription(account_id, m_id) or {}
            sub_state_txt = sub_info.get("state", "未订阅")
            log_all(f"⏸️ [成员ID: {m_id} | 名字: {m_name}] 订阅状态为【{sub_state_txt}】，跳过实时抓取 (保留社媒/博客/离线归档)", is_debug=True)
            return None

    id_list, id_set = load_sent_ids(group_type, m_id)
    l_time_ref = [l_time]

    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            acc_cfg = account_cfg if account_cfg is not None else cfg.ACCOUNTS.get(account_id, {})
            is_mobile = acc_cfg.get("auth_method") == "mobile"

            # ── URL 构建 ──
            if is_mobile:
                base = acc_cfg.get("api_base") or get_mobile_api_base(account_id, account_cfg=acc_cfg)
            elif acc_cfg.get("api_base"):
                base = acc_cfg["api_base"]
            elif group_type.lower() == "yodel":
                base = "https://api.service.yodel-app.com"
            else:
                base = f"https://api.message.{group_type}.com"

            url = (
                f"{base.rstrip('/')}/v2/groups/{m_id}/timeline"
                f"?updated_from={quote(l_time_ref[0])}&count=200&order=asc"
            )
            past_url = f"{base.rstrip('/')}/v2/groups/{m_id}/past_messages"

            # ── Header 构建 ──
            if is_mobile:
                headers = get_mobile_headers(account_id, account_cfg=acc_cfg)
            else:
                cookie_str = "; ".join(f"{k}={v}" for k, v in (cred.get("cookies") or {}).items())
                headers = get_web_headers(
                    group_type, cred.get("token", ""),
                    app_tag=acc_cfg.get("app_tag"),
                    api_base=acc_cfg.get("api_base"),
                    web_origin=acc_cfg.get("web_origin"),
                )
                headers["cookie"] = cookie_str

            # 首次请求成功是正常路径，不额外打印“开始请求”噪声；只有真正进入
            # 重试时才保留尝试次数，失败分支本身会记录具体状态码/异常类型。
            if attempt > 1:
                log_all(
                    f"🔁 [成员ID: {m_id} | 名字: {m_name}] 重试 API 请求 "
                    f"(尝试 {attempt}/{MAX_FETCH_ATTEMPTS})",
                    is_debug=True,
                )
            async with _semaphore:
                resp = await _http_client.get(url, headers=headers)

            if resp.status_code == 200:
                try:
                    msgs = resp.json().get("messages", [])
                except ValueError:
                    log_all(f"🚨 [成员ID: {m_id} | 名字: {m_name}] API 响应 HTTP 200 但不是合法 JSON", is_error=True)
                    _health_tracker().record_member_fetch(m_name, False, ErrorTier.TRANSIENT, "API 响应非 JSON")
                    return None

                if pending_message_ids:
                    # 归档索引只存消息本体，投递状态表仍不携带正文；
                    # API 返回的新版本优先，归档记录用于补齐被截断的历史。
                    archived_pending = archive.load_messages_by_ids(m_name, pending_message_ids)
                    if archived_pending:
                        existing_ids = {
                            str(item.get("id") or item.get("updated_at") or "")
                            for item in msgs
                        }
                        msgs.extend(
                            item for item in archived_pending
                            if str(item.get("id") or item.get("updated_at") or "") not in existing_ids
                        )
                        log_all(
                            f"♻️ [成员ID: {m_id} | 名字: {m_name}] 恢复待投递消息 "
                            f"{len(archived_pending)} 条",
                            is_debug=True,
                        )

                # 首次加入监控时，额外尝试拉取过去 24 小时历史消息 (/past_messages)
                if is_first_fetch:
                    try:
                        async with _semaphore:
                            past_resp = await _http_client.get(past_url, headers=headers)
                        if past_resp.status_code == 200:
                            past_msgs = past_resp.json().get("messages", [])
                            if past_msgs:
                                log_all(f"📥 [成员ID: {m_id} | 名字: {m_name}] 首次巡查：成功拉取 {len(past_msgs)} 条订阅前历史消息 (past_messages)", is_debug=True)
                                existing_ids = {str(m.get("id") or m.get("updated_at", "")) for m in msgs}
                                for pm in past_msgs:
                                    if str(pm.get("id") or pm.get("updated_at", "")) not in existing_ids:
                                        msgs.append(pm)
                    except httpx.TimeoutException:
                        log_all(f"⚠️ [成员ID: {m_id} | 名字: {m_name}] 拉取 past_messages 超时，已跳过历史补偿", is_debug=True)
                    except httpx.RequestError as e:
                        log_all(f"⚠️ [成员ID: {m_id} | 名字: {m_name}] 拉取 past_messages 网络失败: {format_httpx_error(e)}", is_debug=True)
                    except ValueError:
                        log_all(f"⚠️ [成员ID: {m_id} | 名字: {m_name}] past_messages 响应不是合法 JSON", is_debug=True)
                    except Exception as e:  # 可选历史补偿不可阻断实时抓取，但必须可追踪。
                        log_all(f"⚠️ [成员ID: {m_id} | 名字: {m_name}] past_messages 未处理异常: {type(e).__name__}: {e}", is_error=True)

                effective_skip_types = skip_publish_types if skip_publish_types is not None else cfg.SKIP_PUBLISH_TYPES
                new_msgs = sorted(
                    [
                        m for m in msgs
                        if m.get("updated_at")
                        and m.get("updated_at") >= l_time_ref[0]
                        and m.get("publish_type") not in effective_skip_types
                    ],
                    key=lambda x: x["updated_at"],
                )
                truly_new = [m for m in new_msgs if str(m.get("id") or m.get("updated_at", "")) not in id_set]
                log_all(
                    f"📥 [成员ID: {m_id} | 名字: {m_name}] API 成功 | HTTP 200 | "
                    f"原始消息 {len(msgs)} 条 | 待处理 {len(truly_new)} 条",
                    is_debug=True,
                )

                if truly_new:
                    log_response(resp.text)
                elif is_first_fetch:
                    archive.set_timeline_watermark(group_type, m_id, l_time_ref[0])

                _health_tracker().record_member_fetch(m_name, True)
                return (new_msgs, id_list, id_set, l_time_ref, time_file, file_lock)

            elif resp.status_code == 401:
                log_response(resp.text)
                body_snippet = resp.text[:300] if resp.text else "(空响应)"
                if attempt >= MAX_FETCH_ATTEMPTS:
                    log_all(f"🔥 [成员ID: {m_id} | 名字: {m_name}] HTTP 401 达到最大尝试次数，放弃轮询 | {body_snippet}", is_error=True)
                    _health_tracker().record_member_fetch(m_name, False, ErrorTier.PERSISTENT, f"401 认证失败 (已重试{MAX_FETCH_ATTEMPTS}次)")
                    return None
                log_all(
                    f"⚠️ [成员ID: {m_id} | 名字: {m_name}] 触发 HTTP 401，尝试刷新账号 {account_id} token "
                    f"(尝试 {attempt}/{MAX_FETCH_ATTEMPTS})...",
                    is_error=True,
                )
                if is_mobile:
                    if not await refresh_mobile_token(
                        account_id, target_group, old_token=cred.get("token"), account_cfg=acc_cfg
                    ):
                        log_all(f"🔥 {m_name} 账号移动端刷新失败，放弃本次轮询", is_error=True)
                        state = get_refresh_state(account_id)
                        tier = ErrorTier.PERSISTENT if state.get("kind") == "credential_invalid" else ErrorTier.TRANSIENT
                        _health_tracker().record_member_fetch(m_name, False, tier, "移动端 Token 刷新失败")
                        return None
                else:
                    if not await refresh_token(
                        account_id, target_group, old_token=cred["token"], account_cfg=acc_cfg
                    ):
                        log_all(f"🔥 {m_name} 账号刷新失败，放弃本次轮询", is_error=True)
                        state = get_refresh_state(account_id)
                        tier = ErrorTier.PERSISTENT if state.get("kind") == "credential_invalid" else ErrorTier.TRANSIENT
                        _health_tracker().record_member_fetch(m_name, False, tier, "Web Token 刷新失败")
                        return None
                continue

            else:
                log_response(resp.text)
                body_snippet = resp.text[:300] if resp.text else "(空响应)"
                log_all(f"🚨 {m_name} 异常状态码 HTTP {resp.status_code} | {body_snippet}", is_error=True)
                _health_tracker().record_member_fetch(m_name, False, ErrorTier.TRANSIENT, f"HTTP {resp.status_code}")
                return None

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            log_all(
                f"🔥 {m_name} 网络错误 (尝试 {attempt}/{MAX_FETCH_ATTEMPTS}): {format_httpx_error(e)}",
                is_error=True,
            )
            if attempt < MAX_FETCH_ATTEMPTS:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1.5)  # nosec B311
                log_all(f"⏳ {m_name} {delay:.1f}s 后重试...", is_debug=True)
                await asyncio.sleep(delay)
            else:
                log_all(f"🚨 {m_name} 达到最大重试次数，放弃", is_error=True)
                _health_tracker().record_member_fetch(m_name, False, ErrorTier.TRANSIENT, f"网络错误: {format_httpx_error(e)}")
                return None

        except httpx.RequestError as e:
            log_all(
                f"🔥 {m_name} HTTP 请求失败 (尝试 {attempt}/{MAX_FETCH_ATTEMPTS}): {format_httpx_error(e)}",
                is_error=True,
            )
            if attempt < MAX_FETCH_ATTEMPTS:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1.5)  # nosec B311
                await asyncio.sleep(delay)
                continue
            _health_tracker().record_member_fetch(m_name, False, ErrorTier.TRANSIENT, "HTTP 请求失败")
            return None

        except RuntimeError as e:
            if "Event loop is closed" in str(e) or "closed" in str(e):
                log_all(f"⚠️ {m_name} 检测到连接池 Loop 变动，自动重置客户端并重试...", is_debug=True)
                from src import http_pool
                _http_client = await http_pool.reset_general_client()
                if attempt < MAX_FETCH_ATTEMPTS:
                    await asyncio.sleep(1.0)
                    continue
            log_all(f"🔥 {m_name} 运行时异常: {e}", is_error=True)
            _health_tracker().record_member_fetch(m_name, False, ErrorTier.TRANSIENT, f"RuntimeError: {e}")
            return None

        except Exception as e:
            log_all(f"🔥 {m_name} 巡查异常 ({type(e).__name__}): {e}", is_error=True)
            _health_tracker().record_member_fetch(m_name, False, ErrorTier.TRANSIENT, f"未预料错误: {type(e).__name__}")
            return None

    return None


async def _push_member_messages(member: dict, new_msgs: list,
                                 id_list: list, id_set: set,
                                 l_time_ref: list,
                                 time_file: str, file_lock) -> bool:
    """
    Phase 2（按路由 lane 推送）：消息仍按时间顺序准备，但每个路由
    只允许在自己的前序消息完成后继续；某路由失败不会阻断其它路由。
    返回 True 表示本批没有待补偿路由，False 表示仍有持久化积压。
    """
    m_name = member["m_name"]
    truly_new = [m for m in new_msgs
                 if str(m.get("id") or m.get("updated_at", "")) not in id_set]
    lane_context = _DeliveryLaneContext()
    context_token = _delivery_lane_context.set(lane_context)
    try:
        for msg in new_msgs:
            handled_before = lane_context.handled_count
            ok = await _handle_message(member, msg, id_list, id_set, l_time_ref)
            if not ok and lane_context.handled_count == handled_before:
                # 旧扩展/单测可能替换了只返回 bool 的 _handle_message；
                # 无法获得 lane 结果时保持原先的“失败即停”安全语义。
                archive.set_timeline_watermark(member["group_type"], member["m_id"], l_time_ref[0])
                if time_file:
                    await write_time_record(time_file, file_lock, l_time_ref[0])
                _health_tracker().record_member_push(m_name, False)
                return False
    except asyncio.CancelledError:
        # 路由状态和 sent_id 在单条消息成功后已落盘；取消可能发生在
        # 后续发送间隔或下一条消息等待期间。先 checkpoint 最后一条成功
        # 消息的 SQLite 水位线，避免恢复后重复扫描已完成进度，再保留
        # 取消语义让上层结束本轮。SQLite 是当前水位线的主来源，旧时间
        # 文件同步在取消路径跳过，下一轮仍会以数据库值为准。
        archive.set_timeline_watermark(member["group_type"], member["m_id"], l_time_ref[0])
        _health_tracker().record_member_push(m_name, False)
        raise
    finally:
        _delivery_lane_context.reset(context_token)

    # 待补偿路由已写入 delivery_pending_routes，成员总水位可以推进到
    # 本批最后一条；下一次抓取会按最早 pending 时间回放归档消息。
    archive.set_timeline_watermark(member["group_type"], member["m_id"], l_time_ref[0])
    if time_file:
        await write_time_record(time_file, file_lock, l_time_ref[0])

    new_count = len(truly_new)
    if lane_context.unresolved:
        log_all(
            f"⚠️ {m_name} 已处理 {new_count} 条消息，"
            f"待补偿 {len(lane_context.unresolved)} 条消息/路由 lane",
            is_error=True,
        )
        _health_tracker().record_member_push(m_name, False)
        return False
    if new_count > 0:
        log_all(f"✅ {m_name} 推送 {new_count} 条新消息")
    _health_tracker().record_member_push(m_name, True)
    return True
