# ============================================================
# fetcher.py — 核心抓取逻辑：拉取成员消息并分发推送
# ============================================================
import asyncio
import os
import random
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

import config.config as cfg
from src import archive
from src.logger import error_logger, format_httpx_error, log_all, log_response
from config.credentials import (
    ACCOUNT_CREDS, get_file_lock, get_mobile_api_base, get_mobile_headers,
    get_web_headers, refresh_mobile_token, refresh_token, write_time_record,
)
from src.dedup import load_sent_ids, save_sent_id
from src.translator import translate_text
from src.notifier import send_member_message
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
    if cfg.ENABLE_TRANSLATION and original_text.strip():
        raw = await translate_text(original_text, m_name, group_type)
        if raw.startswith("[翻译失败") or raw.startswith("[消息过长"):
            log_all(f"⚠️ {m_name} 翻译失败，仅推送原文 ({raw})", is_error=True)
        elif raw.strip() == original_text.strip():
            log_all(f"ℹ️ {m_name} 翻译结果与原文一致，跳过翻译推送")
        else:
            translated = raw
            if error_logger:
                error_logger.info(f"🌐 翻译结果 ({m_name}): {translated[:150]}")

    # 归档（后台执行，不阻塞推送；开关 archive.enabled）
    archive.schedule_archive(member, msg, translated)

    # 推送 QQ
    chain = build_message_chain(m_name, updated, msg, translated)
    if not await send_member_message(member, chain):
        log_all(f"⚠️ {m_name} 消息推送失败，保留时间戳等待下次重试", is_error=True)
        return False

    save_sent_id(group_type, m_id, msg_id, id_list, id_set)
    l_time_ref[0] = updated
    delay = max(0, cfg.QQ_SEND_INTERVAL + random.uniform(-0.3, 0.5))  # 基于配置值随机微调
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
    account_id   = member["account_id"]
    group_type   = member["group_type"]
    m_id         = member["m_id"]
    m_name       = member["m_name"]
    # 只推 TG 的成员可以没有 QQ 群；0 表示告警不走 NapCat
    target_groups = member.get("target_groups") or []
    target_group  = target_groups[0] if target_groups else 0

    cred = ACCOUNT_CREDS.get(account_id)
    if not cred:
        log_all(f"🚨 {m_name} 账号 {account_id} 无可用凭据", is_error=True)
        _health_tracker().record_member_fetch(m_name, False, ErrorTier.PERSISTENT, f"账号 {account_id} 无可用凭据")
        return None

    os.makedirs(cfg.TIME_RECORD_DIR, exist_ok=True)
    time_file = os.path.join(cfg.TIME_RECORD_DIR, f"time_{group_type}_{m_id}.txt")
    file_lock = get_file_lock(time_file)

    async with file_lock:
        if not os.path.exists(time_file):
            l_time = (
                datetime.now(timezone.utc) - timedelta(hours=cfg.BACKTRACK_HOURS)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            with open(time_file, "r", encoding="utf-8") as f:
                l_time = f.read().strip()

    id_list, id_set = load_sent_ids(group_type, m_id)
    l_time_ref = [l_time]

    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            acc_cfg = cfg.ACCOUNTS.get(account_id, {})
            is_mobile = acc_cfg.get("auth_method") == "mobile"

            # ── URL 构建 ──
            if is_mobile:
                base = acc_cfg.get("api_base") or get_mobile_api_base(account_id)
                url = (
                    f"{base}/v2/groups/{m_id}/timeline"
                    f"?updated_from={quote(l_time_ref[0])}&count=200&order=asc"
                )
            elif acc_cfg.get("api_base"):
                url = (
                    f"{acc_cfg['api_base']}/v2/groups/{m_id}/timeline"
                    f"?updated_from={quote(l_time_ref[0])}&count=200&order=asc"
                )
            else:
                url = (
                    f"https://api.message.{group_type}.com/v2/groups/{m_id}/timeline"
                    f"?updated_from={quote(l_time_ref[0])}&count=200&order=asc"
                )

            # ── Header 构建 ──
            if is_mobile:
                headers = get_mobile_headers(account_id)
            else:
                # .get 防御：认证方式刚切换、凭证尚未重建时结构可能错位，
                # 缺字段应表现为 401 触发告警，而不是 KeyError 炸掉本轮
                cookie_str = "; ".join(f"{k}={v}" for k, v in (cred.get("cookies") or {}).items())
                headers = get_web_headers(
                    group_type, cred.get("token", ""),
                    app_tag=acc_cfg.get("app_tag"),
                    api_base=acc_cfg.get("api_base"),
                    web_origin=acc_cfg.get("web_origin"),
                )
                headers["cookie"] = cookie_str

            async with _semaphore:
                resp = await _http_client.get(url, headers=headers)

            if resp.status_code == 200:
                try:
                    msgs = resp.json().get("messages", [])
                except Exception:
                    log_all(f"🚨 {m_name} API 响应不是合法 JSON", is_error=True)
                    _health_tracker().record_member_fetch(m_name, False, ErrorTier.TRANSIENT, "API 响应非 JSON")
                    return None

                new_msgs = sorted(
                    [
                        m for m in msgs
                        if m.get("updated_at")
                        and m.get("updated_at") >= l_time_ref[0]
                        and m.get("publish_type") not in cfg.SKIP_PUBLISH_TYPES
                    ],
                    key=lambda x: x["updated_at"],
                )
                if new_msgs and any(
                    str(m.get("id") or m.get("updated_at", "")) not in id_set
                    for m in new_msgs
                ):
                    log_response(resp.text)

                _health_tracker().record_member_fetch(m_name, True)
                return (new_msgs, id_list, id_set, l_time_ref, time_file, file_lock)

            elif resp.status_code == 401:
                log_response(resp.text)
                body_snippet = resp.text[:300] if resp.text else "(空响应)"
                if attempt >= MAX_FETCH_ATTEMPTS:
                    log_all(f"🔥 {m_name} 已达最大尝试次数，放弃本次轮询 | {body_snippet}", is_error=True)
                    _health_tracker().record_member_fetch(m_name, False, ErrorTier.PERSISTENT, f"401 认证失败 (已重试{MAX_FETCH_ATTEMPTS}次)")
                    return None
                log_all(
                    f"⚠️ {m_name} 触发 401，刷新账号 {account_id} token "
                    f"(尝试 {attempt}/{MAX_FETCH_ATTEMPTS})... | {body_snippet}",
                    is_error=True,
                )
                if is_mobile:
                    if not await refresh_mobile_token(account_id, target_group, old_token=cred.get("token")):
                        log_all(f"🔥 {m_name} 账号移动端刷新失败，放弃本次轮询", is_error=True)
                        _health_tracker().record_member_fetch(m_name, False, ErrorTier.PERSISTENT, "移动端 Token 刷新失败")
                        return None
                else:
                    if not await refresh_token(account_id, target_group, old_token=cred["token"]):
                        log_all(f"🔥 {m_name} 账号刷新失败，放弃本次轮询", is_error=True)
                        _health_tracker().record_member_fetch(m_name, False, ErrorTier.PERSISTENT, "Web Token 刷新失败")
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
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
                log_all(f"⏳ {m_name} {delay:.1f}s 后重试...", is_debug=True)
                await asyncio.sleep(delay)
            else:
                log_all(f"🚨 {m_name} 达到最大重试次数，放弃", is_error=True)
                _health_tracker().record_member_fetch(m_name, False, ErrorTier.TRANSIENT, f"网络错误: {format_httpx_error(e)}")
                return None

        except Exception as e:
            log_all(f"🔥 {m_name} 未预料的错误:\n{traceback.format_exc()}", is_error=True)
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
            await write_time_record(time_file, file_lock, l_time_ref[0])
            _health_tracker().record_member_push(m_name, False)
            return False

    await write_time_record(time_file, file_lock, l_time_ref[0])

    new_count = len(truly_new)
    if new_count > 0:
        log_all(f"✅ {m_name} 推送 {new_count} 条新消息")
    else:
        log_all(f"✅ {m_name} 无新消息", is_debug=True)
    _health_tracker().record_member_push(m_name, True)
    return True
