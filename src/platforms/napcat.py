# ============================================================
# napcat.py — NapCat/OneBot HTTP QQ 消息发送 & 消息链构造
# ============================================================
import asyncio
import json
from src.utils import utc_to_jst

import httpx

import config.config as cfg
from src.constants import ROLE_KEY, ROLE_TRANSLATION
from src.logger import format_httpx_error, log_all

# ---- 模块级状态（由 initialize() 在 main() 中注入） ----
_client: httpx.AsyncClient = None   # type: ignore


def initialize(client: httpx.AsyncClient) -> None:
    """注入共享的 AsyncClient 实例。"""
    global _client
    _client = client


# ──────────────────────────────────────────────
# 消息链构造
# ──────────────────────────────────────────────
def build_message_chain(
    m_name: str,
    updated: str,
    msg: dict,
    translated_text: str = "",
    model_name: str = "",
) -> list[dict]:
    """
    将一条 API 消息体转换为 QQ 消息链。
    updated 格式：'%Y-%m-%dT%H:%M:%SZ'（UTC）
    NapCat 支持文本与图片/视频/语音在同一个消息链里发送，
    因此这里始终把成员名和时间戳作为第一段，媒体段紧随其后。
    """
    jst_time = utc_to_jst(updated)
    original = msg.get("text", "")
    # 有正文时加换行分隔；纯媒体消息也保留成员名和时间戳。
    header = f"{m_name} {jst_time}\n{original}" if original else f"{m_name} {jst_time}"
    chain: list[dict] = [
        {"type": "text", "data": {"text": header}}
    ]

    file_url = msg.get("file")
    if file_url:
        media_type = cfg.MEDIA_TYPE_MAP.get(msg.get("type", ""))
        if media_type:
            media_data = {"file": file_url}
            # 部分上游会额外提供文件名/MIME；保留这些提示供 QQ 官方 Bot
            # 上传层生成正确的 file_name，旧版消息没有这些字段时行为不变。
            for key in ("filename", "file_name", "name", "mime_type", "content_type"):
                value = msg.get(key)
                if value:
                    media_data[key] = value
            chain.append({"type": media_type, "data": media_data})
        else:
            log_all(f"⚠️ 未知媒体类型 '{msg.get('type')}'，跳过媒体段")

    if translated_text:
        sep = f"\n\n─── 🌐 译文 ({model_name}) ───\n\n" if model_name else "\n\n─── 🌐 译文 ───\n\n"
        # 打上角色标记，供下游通道识别翻译段（发送前会被剥离）
        chain.append({
            "type": "text",
            "data": {"text": f"{sep}{translated_text}"},
            ROLE_KEY: ROLE_TRANSLATION,
            "_model_name": model_name,
        })

    return chain


def strip_internal_keys(message_chain: list[dict]) -> list[dict]:
    """剥离消息链各段中以 _ 开头的内部字段（如 _role），
    保证发给 OneBot 的 payload 只含协议字段。"""
    return [
        {k: v for k, v in item.items() if not k.startswith("_")}
        for item in message_chain
    ]


def _split_video_record_chain(message_chain: list[dict]) -> list[list[dict]]:
    """
    NapCat/OneBot 的 video、record 段在部分版本里和 text 混发时会吞掉文本段。
    为保证成员名和时间戳一定可见，视频/语音消息拆成文本批次和媒体批次；
    所有文本段会合并在一起，避免正文和翻译分成两条消息。
    图片保持原有的图文合并消息链。
    """
    if not any(item.get("type") in {"video", "record"} for item in message_chain):
        return [message_chain]

    text_batch: list[dict] = []
    media_batches: list[list[dict]] = []
    other_batches: list[list[dict]] = []
    for item in message_chain:
        msg_type = item.get("type")
        if msg_type == "text":
            text_batch.append(item)
            continue
        if msg_type in {"video", "record"}:
            media_batches.append([item])
            continue
        # 理论上 video/record 消息不会混入 image；这里保守地单独发送未知非文本段。
        other_batches.append([item])

    batches: list[list[dict]] = []
    if text_batch:
        batches.append(text_batch)
    batches.extend(media_batches)
    batches.extend(other_batches)
    return batches


# ──────────────────────────────────────────────
# 发送
# ──────────────────────────────────────────────
async def _post_message(group_id: int, message_chain: list[dict], max_retries: int) -> bool:
    payload_str = json.dumps(
        {"group_id": group_id, "message": strip_internal_keys(message_chain)},
        ensure_ascii=False,
    )
    if cfg.DEBUG_LOG_QQ_PAYLOAD:
        log_all(
            f"📤 发送体: {len(payload_str)} 字节 | 预览: {payload_str[:200]}",
            is_debug=True,
        )

    for attempt in range(max_retries):
        try:
            resp = await _client.post(
                cfg.QQ_BOT_API,
                content=payload_str,
                headers={"Content-Type": "application/json", "User-Agent": cfg.QQ_USER_AGENT},
            )
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except Exception:
                    log_all(
                        f"⚠️ Bot 返回非 JSON 响应: {resp.text[:200]}",
                        is_error=True,
                    )
                    return True
                if body.get("status") == "ok" or body.get("retcode") == 0:
                    return True
                log_all(f"⚠️ Bot 返回业务失败: {body}", is_error=True)
                return False
            else:
                log_all(
                    f"📡 异常响应 ({attempt + 1}/{max_retries}): "
                    f"HTTP {resp.status_code} | {resp.text[:200]}",
                    is_error=True,
                )
                if resp.status_code == 502:
                    log_all("🔥 502，代理/协议问题", is_error=True)
                # 4xx（除 429）不可重试，直接放弃
                if resp.status_code < 500 and resp.status_code != 429:
                    return False

        except Exception as e:
            log_all(
                f"🔥 发送异常 ({attempt + 1}/{max_retries}): {format_httpx_error(e)}",
                is_error=True,
            )

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)

    return False


async def send_qq_message(
    group_id: int,
    message_chain: list[dict],
    max_retries: int = 3,
) -> bool:
    batches = _split_video_record_chain(message_chain)
    for batch in batches:
        if not await _post_message(group_id, batch, max_retries):
            log_all("❌ QQ 消息发送彻底失败", is_error=True)
            return False

    return True
