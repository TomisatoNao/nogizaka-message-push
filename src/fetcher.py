# ============================================================
# fetcher.py — 核心抓取逻辑：拉取成员消息并分发推送
# ============================================================
import asyncio
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
from src.notifier import send_member_message_detailed
from src.delivery_state import mark_successful_routes, successful_routes
from src.health import ErrorTier, get_tracker as _health_tracker
from src.platforms.napcat import build_message_chain

# ---- 模块级状态（由 initialize() 在 main() 中注入） ----
_http_client: httpx.AsyncClient  = None   # type: ignore
_semaphore:   asyncio.Semaphore  = None   # type: ignore

MAX_FETCH_ATTEMPTS = 2
RETRY_BASE_DELAY = 2.0   # 基础退避秒数，实际延迟 = base * 2^(attempt-1) + jitter


def initialize(client: httpx.AsyncClient, semaphore: asyncio.Semaphore) -> None:
    """注入共享的 HTTP 客户端和并发信号量。"""
    global _http_client, _semaphore
    _http_client = client
    _semaphore   = semaphore


async def fetch_member_messages(member: dict):
    """公开接口：抓取单个成员消息。"""
    return await _fetch_member_messages(member)


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
    返回 True 表示本条处理成功，False 表示发送失败需中断本轮。
    l_time_ref 是单元素列表，用于在此函数内修改外层的 l_time 变量。
    """
    m_name     = member["m_name"]
    group_type = member["group_type"]
    m_id       = member["m_id"]

    updated       = msg.get("updated_at", "")
    msg_id        = str(msg.get("id") or updated)
    original_text = msg.get("text", "")

    if msg_id in id_set:
        l_time_ref[0] = updated
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
            log_all(f"🌐 [成员ID: {m_id} | 名字: {m_name}] 翻译完成 ({trans_model}): {translated[:100]}...", is_debug=True)

    # 归档（先行下载媒体并持久化，保证本地素材就绪供各推送通道复用）
    await archive.archive_message(member, msg, translated)

    # 推送各通道（若含有媒体，各通道直接复用本地素材，免去重复网络请求）
    chain = build_message_chain(m_name, updated, msg, translated, model_name=trans_model)
    delivered_routes = successful_routes(group_type, m_id, msg_id)
    report = await send_member_message_detailed(
        member, chain, skip_route_ids=delivered_routes
    )
    mark_successful_routes(
        group_type, m_id, msg_id,
        {attempt.route_id for attempt in report.attempts if attempt.ok},
    )
    if report.failure_count:
        log_all(f"⚠️ [成员ID: {m_id} | 名字: {m_name}] 消息推送失败，保留时间戳等待下次重试", is_error=True)
        return False
    else:
        log_all(f"📤 [成员ID: {m_id} | 名字: {m_name}] 成功分发 1 条消息 (ID: {msg_id})", is_debug=True)

    save_sent_id(group_type, m_id, msg_id, id_list, id_set)
    l_time_ref[0] = updated
    delay = max(0, cfg.QQ_SEND_INTERVAL + random.uniform(-0.3, 0.5))  # nosec B311 -- 基于配置值随机微调
    await asyncio.sleep(delay)
    return True


# ──────────────────────────────────────────────
# 单成员轮询
# ──────────────────────────────────────────────
async def _fetch_member_messages(member: dict):
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

    is_first_fetch = False
    if not l_time:
        is_first_fetch = True
        l_time = (
            datetime.now(timezone.utc) - timedelta(hours=cfg.BACKTRACK_HOURS)
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
            acc_cfg = cfg.ACCOUNTS.get(account_id, {})
            is_mobile = acc_cfg.get("auth_method") == "mobile"

            # ── URL 构建 ──
            if is_mobile:
                base = acc_cfg.get("api_base") or get_mobile_api_base(account_id)
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
                headers = get_mobile_headers(account_id)
            else:
                cookie_str = "; ".join(f"{k}={v}" for k, v in (cred.get("cookies") or {}).items())
                headers = get_web_headers(
                    group_type, cred.get("token", ""),
                    app_tag=acc_cfg.get("app_tag"),
                    api_base=acc_cfg.get("api_base"),
                    web_origin=acc_cfg.get("web_origin"),
                )
                headers["cookie"] = cookie_str

            log_all(f"🔍 [成员ID: {m_id} | 名字: {m_name}] 请求 API (尝试 {attempt}/{MAX_FETCH_ATTEMPTS})", is_debug=True)
            async with _semaphore:
                resp = await _http_client.get(url, headers=headers)

            if resp.status_code == 200:
                try:
                    msgs = resp.json().get("messages", [])
                except ValueError:
                    log_all(f"🚨 [成员ID: {m_id} | 名字: {m_name}] API 响应 HTTP 200 但不是合法 JSON", is_error=True)
                    _health_tracker().record_member_fetch(m_name, False, ErrorTier.TRANSIENT, "API 响应非 JSON")
                    return None

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

                new_msgs = sorted(
                    [
                        m for m in msgs
                        if m.get("updated_at")
                        and m.get("updated_at") >= l_time_ref[0]
                        and m.get("publish_type") not in cfg.SKIP_PUBLISH_TYPES
                    ],
                    key=lambda x: x["updated_at"],
                )
                truly_new = [m for m in new_msgs if str(m.get("id") or m.get("updated_at", "")) not in id_set]
                log_all(f"📥 [成员ID: {m_id} | 名字: {m_name}] HTTP 200 | 原始消息 {len(msgs)} 条 | 新增待推送 {len(truly_new)} 条", is_debug=True)

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
                    if not await refresh_mobile_token(account_id, target_group, old_token=cred.get("token")):
                        log_all(f"🔥 {m_name} 账号移动端刷新失败，放弃本次轮询", is_error=True)
                        state = get_refresh_state(account_id)
                        tier = ErrorTier.PERSISTENT if state.get("kind") == "credential_invalid" else ErrorTier.TRANSIENT
                        _health_tracker().record_member_fetch(m_name, False, tier, "移动端 Token 刷新失败")
                        return None
                else:
                    if not await refresh_token(account_id, target_group, old_token=cred["token"]):
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
    Phase 2（串行推送）：按时间顺序逐条推送 → 写时间戳。
    返回 True 表示全部推送成功，False 表示有消息推送失败。
    """
    m_name = member["m_name"]
    truly_new = [m for m in new_msgs
                 if str(m.get("id") or m.get("updated_at", "")) not in id_set]

    for msg in new_msgs:
        ok = await _handle_message(member, msg, id_list, id_set, l_time_ref)
        if not ok:
            archive.set_timeline_watermark(member["group_type"], member["m_id"], l_time_ref[0])
            if time_file:
                await write_time_record(time_file, file_lock, l_time_ref[0])
            _health_tracker().record_member_push(m_name, False)
            return False

    archive.set_timeline_watermark(member["group_type"], member["m_id"], l_time_ref[0])
    if time_file:
        await write_time_record(time_file, file_lock, l_time_ref[0])

    new_count = len(truly_new)
    if new_count > 0:
        log_all(f"✅ {m_name} 推送 {new_count} 条新消息")
    else:
        log_all(f"✅ {m_name} 无新消息", is_debug=True)
    _health_tracker().record_member_push(m_name, True)
    return True
