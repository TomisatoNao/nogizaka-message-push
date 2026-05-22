# ============================================================
# fetcher.py — 核心抓取逻辑：拉取成员消息并分发推送
# ============================================================
import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from config.config import ACCOUNTS, BACKTRACK_HOURS, ENABLE_TRANSLATION, SKIP_PUBLISH_TYPES, TIME_RECORD_DIR
from src.logger import error_logger, log_all, log_response
from config.credentials import ACCOUNT_CREDS, get_file_lock, get_web_headers, refresh_token, write_time_record
from src.dedup import load_sent_ids, save_sent_id
from src.translator import translate_text
from src.platforms.bilibili import post_dynamic, resolve_cookie
from src.notifier import send_member_message
from src.platforms.napcat import build_message_chain

# ---- 模块级状态（由 initialize() 在 main() 中注入） ----
_http_client: httpx.AsyncClient  = None   # type: ignore
_semaphore:   asyncio.Semaphore  = None   # type: ignore

MAX_FETCH_ATTEMPTS = 2


def initialize(client: httpx.AsyncClient, semaphore: asyncio.Semaphore) -> None:
    """注入共享的 HTTP 客户端和并发信号量。"""
    global _http_client, _semaphore
    _http_client = client
    _semaphore   = semaphore


# ──────────────────────────────────────────────
# 单条消息处理
# ──────────────────────────────────────────────
async def _handle_message(member: dict, msg: dict,
                           id_list: list, id_set: set, l_time_ref: list) -> bool:
    """
    翻译 → 推送 QQ → 同步 B站 → 记录状态。
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
    if ENABLE_TRANSLATION and original_text.strip():
        raw = await translate_text(original_text)
        if raw.startswith("[翻译失败") or raw.startswith("[消息过长"):
            log_all(f"⚠️ {m_name} 翻译失败，仅推送原文 ({raw})", is_error=True)
        else:
            translated = raw
            if error_logger:
                error_logger.info(f"🌐 翻译结果 ({m_name}): {translated[:150]}")

    # 推送 QQ
    chain = build_message_chain(m_name, updated, msg, translated)
    if not await send_member_message(member, chain):
        log_all(f"⚠️ {m_name} 消息推送失败，保留时间戳等待下次重试", is_error=True)
        return False

    # 同步 B站（无正文时跳过，避免只推送名字+时间戳）
    if member.get("post_to_bilibili") and original_text.strip():
        cookie, bili_jct = resolve_cookie(member)
        jst_time = (
            datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .astimezone(timezone(timedelta(hours=9)))
            .strftime("%m/%d %H:%M:%S")
        )
        bili_text = f"{m_name} {jst_time}\n{original_text}"
        if translated:
            bili_text += f"\n\n📝 翻译：\n{translated}"
        await post_dynamic(bili_text, cookie, bili_jct)

    save_sent_id(group_type, m_id, msg_id, id_list, id_set)
    l_time_ref[0] = updated
    await asyncio.sleep(1.5)
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
    target_group = member["target_groups"][0]

    cred = ACCOUNT_CREDS.get(account_id)
    if not cred:
        log_all(f"🚨 {m_name} 账号 {account_id} 无可用凭据", is_error=True)
        return None

    os.makedirs(TIME_RECORD_DIR, exist_ok=True)
    time_file = os.path.join(TIME_RECORD_DIR, f"time_{group_type}_{m_id}.txt")
    file_lock = get_file_lock(time_file)

    async with file_lock:
        if not os.path.exists(time_file):
            l_time = (
                datetime.now(timezone.utc) - timedelta(hours=BACKTRACK_HOURS)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            with open(time_file, "r", encoding="utf-8") as f:
                l_time = f.read().strip()

    id_list, id_set = load_sent_ids(group_type, m_id)
    l_time_ref = [l_time]

    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            acc_cfg = ACCOUNTS.get(account_id, {})
            api_base = acc_cfg.get("api_base")
            if api_base:
                url = (
                    f"{api_base}/v2/groups/{m_id}/timeline"
                    f"?updated_from={quote(l_time_ref[0])}&count=200&order=asc"
                )
            else:
                url = (
                    f"https://api.message.{group_type}.com/v2/groups/{m_id}/timeline"
                    f"?updated_from={quote(l_time_ref[0])}&count=200&order=asc"
                )
            cookie_str = "; ".join(f"{k}={v}" for k, v in cred["cookies"].items())
            headers = get_web_headers(
                group_type, cred["token"],
                app_tag=acc_cfg.get("app_tag"),
                api_base=api_base,
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
                    return None

                new_msgs = sorted(
                    [
                        m for m in msgs
                        if m.get("updated_at")
                        and m.get("updated_at") >= l_time_ref[0]
                        and m.get("publish_type") not in SKIP_PUBLISH_TYPES
                    ],
                    key=lambda x: x["updated_at"],
                )
                if new_msgs and any(
                    str(m.get("id") or m.get("updated_at", "")) not in id_set
                    for m in new_msgs
                ):
                    log_response(resp.text)

                return (new_msgs, id_list, id_set, l_time_ref, time_file, file_lock)

            elif resp.status_code == 401:
                log_response(resp.text)
                if attempt >= MAX_FETCH_ATTEMPTS:
                    log_all(f"🔥 {m_name} 已达最大尝试次数，放弃本次轮询", is_error=True)
                    return None
                log_all(
                    f"⚠️ {m_name} 触发 401，刷新账号 {account_id} token "
                    f"(尝试 {attempt}/{MAX_FETCH_ATTEMPTS})...",
                    is_error=True,
                )
                if not await refresh_token(account_id, target_group, old_token=cred["token"]):
                    log_all(f"🔥 {m_name} 账号刷新失败，放弃本次轮询", is_error=True)
                    return None
                continue

            else:
                log_response(resp.text)
                log_all(f"🚨 {m_name} 异常状态码 HTTP {resp.status_code}", is_error=True)
                return None

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            log_all(f"🔥 {m_name} 网络错误 (尝试 {attempt}/{MAX_FETCH_ATTEMPTS}): {e}", is_error=True)
            if attempt < MAX_FETCH_ATTEMPTS:
                await asyncio.sleep(2)
            else:
                log_all(f"🚨 {m_name} 达到最大重试次数，放弃", is_error=True)

        except Exception:
            log_all(f"🔥 {m_name} 未预料的错误:\n{traceback.format_exc()}", is_error=True)
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
            return False

    await write_time_record(time_file, file_lock, l_time_ref[0])

    new_count = len(truly_new)
    if new_count > 0:
        log_all(f"✅ {m_name} 推送 {new_count} 条新消息")
    else:
        log_all(f"✅ {m_name} 无新消息", is_debug=True)
    return True
