# ============================================================
# fetcher.py — 核心抓取逻辑：拉取成员消息并分发推送
# ============================================================
import asyncio
import os
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from config.config import BACKTRACK_HOURS, ENABLE_TRANSLATION, SKIP_PUBLISH_TYPES, TIME_RECORD_DIR
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
async def _handle_message(member: dict, msg: dict, time_file: str, file_lock: asyncio.Lock,
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
        await write_time_record(time_file, file_lock, updated)
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
        bj_time = (
            datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .astimezone(timezone(timedelta(hours=8)))
            .strftime("%m/%d %H:%M:%S")
        )
        bili_text = f"{m_name} {bj_time}\n{original_text}"
        if translated:
            bili_text += f"\n\n📝 翻译：\n{translated}"
        await post_dynamic(bili_text, cookie, bili_jct)

    # 记录状态
    save_sent_id(group_type, m_id, msg_id, id_list, id_set)
    await write_time_record(time_file, file_lock, updated)
    l_time_ref[0] = updated
    await asyncio.sleep(1.5)
    return True


# ──────────────────────────────────────────────
# 单成员轮询
# ──────────────────────────────────────────────
async def fetch_member(member: dict) -> None:
    account_id   = member["account_id"]
    group_type   = member["group_type"]
    m_id         = member["m_id"]
    m_name       = member["m_name"]
    target_group = member["target_group"]

    cred = ACCOUNT_CREDS.get(account_id)
    if not cred:
        log_all(f"🚨 {m_name} 账号 {account_id} 无可用凭据", is_error=True)
        return

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
    l_time_ref = [l_time]   # 用列表包装使其可在子函数内修改

    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            url = (
                f"https://api.message.{group_type}.com/v2/groups/{m_id}/timeline"
                f"?updated_from={quote(l_time_ref[0])}&count=200&order=asc"
            )
            cookie_str = "; ".join(f"{k}={v}" for k, v in cred["cookies"].items())
            headers = get_web_headers(group_type, cred["token"])
            headers["cookie"] = cookie_str

            async with _semaphore:
                resp = await _http_client.get(url, headers=headers)

            # ---- 成功 ----
            if resp.status_code == 200:
                try:
                    msgs = resp.json().get("messages", [])
                except Exception:
                    log_all(f"🚨 {m_name} API 响应不是合法 JSON", is_error=True)
                    return

                # Bug 2 修复：过滤掉 updated_at 为空的异常消息（避免 strptime 崩溃）
                # Bug 1 修复：使用 >= 代替 >，防止同一秒多条消息在下轮被全部过滤掉；
                #             重复推送由 id_set 去重兜底
                new_msgs = sorted(
                    [
                        m for m in msgs
                        if m.get("updated_at")                          # 空值直接跳过
                        and m.get("updated_at") >= l_time_ref[0]
                        and m.get("publish_type") not in SKIP_PUBLISH_TYPES
                    ],
                    key=lambda x: x["updated_at"],
                )
                if new_msgs:
                    log_response(resp.text)

                # 区分"检测到"和"实际推送"，让日志更清晰
                truly_new = [m for m in new_msgs if str(m.get("id") or m.get("updated_at", "")) not in id_set]

                for msg in new_msgs:
                    ok = await _handle_message(
                        member, msg, time_file, file_lock, id_list, id_set, l_time_ref
                    )
                    if not ok:
                        return  # 发送失败，中断本轮，下轮重试

                new_count = len(truly_new)
                if new_count > 0:
                    log_all(f"✅ {m_name} 推送 {new_count} 条新消息")
                else:
                    log_all(f"✅ {m_name} 无新消息", is_debug=True)
                return

            # ---- 401 续期 ----
            elif resp.status_code == 401:
                log_response(resp.text)
                if attempt >= MAX_FETCH_ATTEMPTS:
                    log_all(f"🔥 {m_name} 已达最大尝试次数，放弃本次轮询", is_error=True)
                    return
                log_all(
                    f"⚠️ {m_name} 触发 401，刷新账号 {account_id} token "
                    f"(尝试 {attempt}/{MAX_FETCH_ATTEMPTS})...",
                    is_error=True,
                )
                if not await refresh_token(account_id, target_group, old_token=cred["token"]):
                    log_all(f"🔥 {m_name} 账号刷新失败，放弃本次轮询", is_error=True)
                    return
                continue   # 用新 token 重新请求

            # ---- 其他错误 ----
            else:
                log_response(resp.text)
                log_all(f"🚨 {m_name} 异常状态码 HTTP {resp.status_code}", is_error=True)
                return

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            log_all(f"🔥 {m_name} 网络错误 (尝试 {attempt}/{MAX_FETCH_ATTEMPTS}): {e}", is_error=True)
            if attempt < MAX_FETCH_ATTEMPTS:
                await asyncio.sleep(2)
            else:
                log_all(f"🚨 {m_name} 达到最大重试次数，放弃", is_error=True)

        except Exception:
            log_all(f"🔥 {m_name} 未预料的错误:\n{traceback.format_exc()}", is_error=True)
            return
