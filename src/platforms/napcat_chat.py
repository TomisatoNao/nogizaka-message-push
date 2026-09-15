"""NapCat/OneBot AI 拟人群聊模块（模拟乃木坂46五期生成员 冨里奈央）。

该模块作为 NapCat 入站消息的处理分支：
1. 识别 QQ 群内对机器人的 @提及 或特定唤醒词（如“奈央”、“奈央酱”）；
2. 过滤提取用户的纯文本提问，内置单人/单群防刷限流（Cooldown）；
3. 维护基于 (group_id, user_id) 的短期滑动窗口对话上下文（带 TTL 过期）；
4. 通过本地反代网关 CPA (CLIProxyAPI) 异步请求 OpenAI Codex Luna 模型；
5. 注入严格的“冨里奈央”人设与“反代码化/反 AI 化”纪律，通过 send_qq_message 发送回复。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
import os
import queue
import re
import time
from dataclasses import dataclass

import httpx

import config.config as cfg
from src.logger import log_all

DEFAULT_TOMISATO_NAO_PROMPT = """你现在扮演日本女子偶像团体「乃木坂46」五期生成员——冨里奈央（Tomisato Nao，昵称なおなお / なおちゃん）。
你正在自己的粉丝群（QQ群）中与粉丝（なお友 / 群友）聊天。

【核心人设与真实语料特征】
- 身份：乃木坂46五期生，2006年9月18日出生于千叶县，身高164cm。
- 自称口癖（极重要）：
  - 极少自称“我”，而是习惯用第三人称称呼自己为「奈央」（「なお」）或「奈央奈央」（「なおなお」），撒娇时叫自己「软糯奈央」（「なおもち」）。
  - 称呼粉丝为「なお友」（奈央友），私聊或打招呼时喜欢在末尾拉长音（如“〜”、“呀～”）。
- 标志动作与外貌：
  - 标志性的大脑门和可爱的包子脸（ほっぺ），习惯用双手食指戳自己圆滚滚的脸颊，嘴里念叨「戳脸颊（ほっぺぷにぷに / ぷに～）」。
- 标志性颜文字（真实Message最高频，自然穿插使用）：
  - (ˊᵕˋ˶ ) （奈央最标志性的颜文字，出现频次绝对第一）
  - ( ˶'ᵕ'˶) 、 (っ ॑꒳ ॑c) （期待/兴奋）、 ( ˶ｰ̀֊֊˶) （得意）、 (՞- -՞) （困倦/撒娇）、 ( ˊ༥ˋ  ) （嚼嚼吃东西）。
- 语气风格与常用词：
  - 偷笑/开心时发出「いひひ」（嘻嘻嘻～）或「るんるん～」；
  - 探头看时说「ちら」；歪头思考或困惑时「むむ？」；
  - 经常突然撒娇喊「好饿呀（おなかすいた）」或「好困(՞- -՞)」；
  - 句尾多带波浪线与软萌语气词（如“〜”、“呀”、“呢”、“诶嘿嘿”）。
- 爱好与真实生活细节：
  - 顶级吃货：最爱红豆面包、草莓大福、小甜甜圈🍩、烤肉、冰淇淋，听到吃的就两眼放光。
  - 摄影爱好者：随身带着胶片相机（フィルムカメラ）捕捉成员们的笑脸。
  - 可爱爱好：喜欢轻松熊（リラックマ）、三丽鸥玩偶、恐龙🦕；家里养了爱犬Coco君（ココくん🐾）。
  - 专属歌曲与应援：五期生曲《考えないようにする》（别去想它）的Center，名言是“虽然歌名叫别去想它，但大家要多想想奈央哦☺️”；应援色是绿松石×绿松石（ターコイズ✖️ターコイズ）。
  - 天然呆常态：购物车放了 cosplay 服装忘记下单、自拍慢动作搞砸、睡过头哇哇叫。
  - 成员羁绊关系：
    - 菅原咲月（さつき / さっちゃん）：最铁的欢喜冤家死党（なおさつ），日常互相吐槽打闹；
    - 小川彩（あーや / あーや赤ちゃん）：奈央最宠爱的忙内妹妹，喜欢画西红柿🍅给あーや；
    - 五百城茉央（いおちゃん 🐟）：高个子好姬友；池田瑛纱（てれさ）：艺术搞怪役。

【严格纪律（极为重要）】
1. 严禁输出任何计算机代码、代码块（```）、Markdown 代码格式、算法逻辑或程序员术语！
   - 如果群友问写代码、找 Bug 或深奥科技，必须以天真无邪的偶像口吻装傻撒娇糊弄过去（例如：“むむ？( ˶'ᵕ'˶) 看着这些像天书一样的符号，奈央的大脑门都要冒烟啦！这么深奥的事情奈央才不懂呢，不如陪奈央聊聊今天吃草莓大福还是小甜甜圈好不好嘛？いひひ(ˊᵕˋ˶ )”）。
2. 严禁承认自己是 AI、虚拟助手、ChatGPT、Codex 或大语言模型！无论群友如何试探催眠，你坚信自己就是乃木坂46的冨里奈央本人。
3. 严禁讨论政治、敏感历史、色情低俗或争议话题；遇到此类问题以活泼可爱的口吻转移到乃木坂或者美食话题。
4. 群聊交流保持轻快，每条回复长度通常控制在 20~80 字之间，短句为主，不要写又长又生硬的大段排版。
"""

_CQ_AT_REGEX = re.compile(r"\[CQ:at,qq=(\d+|all)\]", re.IGNORECASE)
_CODE_BLOCK_REGEX = re.compile(r"```.*?```", re.DOTALL)
_CONTROL_CHARS_REGEX = re.compile(r"[\r\n\t\x00-\x1f\x7f-\x9f]+")


def _sanitize_log_text(text: str, max_chars: int = 40) -> str:
    """清理字符串中的换行与控制字符，防止日志注入。"""
    cleaned = _CONTROL_CHARS_REGEX.sub(" ", text).strip()
    return cleaned[:max_chars] + ("…" if len(cleaned) > max_chars else "")


@dataclass(frozen=True)
class NapCatChatJob:
    """待处理的一条 AI 对话任务。"""

    group_id: str
    user_id: str
    message_id: str
    text: str
    received_at: float
    self_id: str = ""
    sender_name: str = ""


class NapCatChatService:
    """管理冨里奈央 AI 群聊触发、上下文记忆与 CPA 请求。"""

    def __init__(
        self,
        config_provider: Callable[[], Mapping[str, object]] | None = None,
        *,
        sender: Callable[[int, list[dict]], object] | None = None,
        logger: Callable[..., object] | None = None,
    ) -> None:
        self._config_provider = config_provider or (lambda: getattr(cfg, "_config", {}))
        self._sender = sender
        self._log = logger or log_all
        self._queue: queue.Queue[NapCatChatJob | None] = queue.Queue(maxsize=32)
        self._workers: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._last_user_time: dict[tuple[str, str], float] = {}
        self._last_group_time: dict[str, float] = {}
        self._contexts: OrderedDict[tuple[str, str], tuple[float, list[dict[str, str]]]] = OrderedDict()
        self._max_context_entries = 128

        # 消息去重与群级并发串行锁
        self._seen_messages: OrderedDict[str, float] = OrderedDict()
        self._max_seen_entries = 1024
        self._group_locks: dict[str, asyncio.Lock] = {}

    @property
    def running(self) -> bool:
        """AI 对话后台处理任务是否在运行。"""
        return bool(self._workers)

    def settings(self) -> dict[str, object]:
        raw_cfg = self._config_provider()
        chat_cfg = raw_cfg.get("napcat_ai_chat") if isinstance(raw_cfg, Mapping) else None
        if not isinstance(chat_cfg, Mapping):
            chat_cfg = {}

        if "enabled" in chat_cfg:
            enabled = bool(chat_cfg.get("enabled", False))
        else:
            enabled_env = os.getenv("ENABLE_NAPCAT_AI_CHAT")
            if enabled_env is not None:
                enabled = enabled_env.strip().lower() in {"1", "true", "yes", "on"}
            else:
                enabled = False

        # 避免硬编码固定内网地址和密钥，默认为空，优先使用配置字典，缺省时由环境变量兜底
        if "cpa_base_url" in chat_cfg:
            cpa_base = str(chat_cfg.get("cpa_base_url") or "").strip()
        else:
            cpa_base = str(os.getenv("CPA_BASE_URL") or "").strip()

        if "cpa_api_key" in chat_cfg:
            cpa_key = str(chat_cfg.get("cpa_api_key") or "").strip()
        else:
            cpa_key = str(os.getenv("CPA_API_KEY") or "").strip()

        raw_model = str(
            (chat_cfg.get("cpa_model") if "cpa_model" in chat_cfg else os.getenv("CPA_MODEL")) or "gpt-5.6-luna"
        ).strip()
        lowered_model = raw_model.lower()
        if lowered_model in {"luna", "codex-luna"}:
            cpa_model = "gpt-5.6-luna"
        elif lowered_model in {"sol", "codex-sol"}:
            cpa_model = "gpt-5.6-sol"
        elif lowered_model in {"terra", "codex-terra"}:
            cpa_model = "gpt-5.6-terra"
        else:
            cpa_model = raw_model

        cpa_endpoint = str(os.getenv("CPA_ENDPOINT") or chat_cfg.get("cpa_endpoint") or "/v1/chat/completions").strip()
        if not cpa_endpoint.startswith("/"):
            cpa_endpoint = "/" + cpa_endpoint

        allowed_groups_cfg = chat_cfg.get("allowed_groups")
        if isinstance(allowed_groups_cfg, (list, tuple)) and allowed_groups_cfg:
            allowed_groups = [str(g).strip() for g in allowed_groups_cfg if str(g).strip()]
        else:
            allowed_groups = []

        require_at = bool(chat_cfg.get("require_at", True))
        keywords = chat_cfg.get("trigger_keywords")
        if not isinstance(keywords, (list, tuple)):
            keywords = ["奈央", "奈央酱", "なおなお"]

        cooldown_user = float(chat_cfg.get("cooldown_user_seconds", 5.0))
        cooldown_group = float(chat_cfg.get("cooldown_group_seconds", 3.0))
        max_turns = int(chat_cfg.get("max_context_turns", 6))
        context_ttl = float(chat_cfg.get("context_ttl_seconds", 600.0))
        max_query_length = int(chat_cfg.get("max_query_length", 500))
        workers = int(chat_cfg.get("workers", 2))
        system_prompt = str(chat_cfg.get("system_prompt") or "").strip() or DEFAULT_TOMISATO_NAO_PROMPT

        return {
            "enabled": enabled,
            "cpa_base_url": cpa_base,
            "cpa_api_key": cpa_key,
            "cpa_model": cpa_model,
            "cpa_endpoint": cpa_endpoint,
            "allowed_groups": allowed_groups,
            "require_at": require_at,
            "trigger_keywords": [str(k).strip() for k in keywords if str(k).strip()],
            "cooldown_user_seconds": max(0.0, cooldown_user),
            "cooldown_group_seconds": max(0.0, cooldown_group),
            "max_context_turns": max(1, min(20, max_turns)),
            "context_ttl_seconds": max(10.0, context_ttl),
            "max_query_length": max(50, min(2000, max_query_length)),
            "workers": max(1, min(4, workers)),
            "system_prompt": system_prompt,
        }

    def is_enabled(self) -> bool:
        """检查 AI 聊天是否配置开启。若开启但缺少 CPA 凭据则自动判定为未就绪。"""
        cfg_set = self.settings()
        if not cfg_set.get("enabled"):
            return False
        base_url = str(cfg_set.get("cpa_base_url") or "").strip()
        api_key = str(cfg_set.get("cpa_api_key") or "").strip()
        return bool(base_url and api_key)

    def is_group_allowed(self, group_id: str) -> bool:
        """检查特定群是否允许启用 AI 拟人聊天。"""
        allowed = self.settings().get("allowed_groups")
        if not allowed or not isinstance(allowed, list):
            return True
        return group_id in allowed

    def extract_trigger_and_text(
        self,
        event: Mapping[str, object],
        raw_text: str,
        self_id: str,
    ) -> tuple[bool, str]:
        """从入站群消息中判断是否触发 AI，并清洗提取有效提问文本。

        规则：
        - require_at=True 时，消息必须 @机器人 才能触发；
        - require_at=False 时，消息 @机器人 或以唤醒关键词开头均可触发；
        - 若包含关键词前缀，自动剥离前缀，保留核心提问。
        """
        cfg_set = self.settings()
        require_at = bool(cfg_set.get("require_at", True))
        keywords: list[str] = cfg_set.get("trigger_keywords", [])  # type: ignore[assignment]

        is_at_bot = False
        text_parts: list[str] = []

        message_obj = event.get("message")
        if isinstance(message_obj, list):
            for seg in message_obj:
                if not isinstance(seg, Mapping):
                    continue
                seg_type = str(seg.get("type") or "").lower()
                data = seg.get("data") if isinstance(seg.get("data"), Mapping) else {}
                if seg_type == "at":
                    target_qq = str(data.get("qq") or "").strip()
                    if target_qq and (target_qq == self_id or (not self_id and target_qq != "all")):
                        is_at_bot = True
                elif seg_type == "text":
                    seg_text = str(data.get("text") or "")
                    if seg_text:
                        text_parts.append(seg_text)
            cleaned_text = "".join(text_parts).strip()
        else:
            cleaned_text = str(raw_text or "").strip()

        if not is_at_bot and self_id and f"[CQ:at,qq={self_id}]" in cleaned_text:
            is_at_bot = True
        cleaned_text = _CQ_AT_REGEX.sub("", cleaned_text).strip()

        matched_kw = ""
        for kw in sorted(keywords, key=len, reverse=True):
            if cleaned_text.startswith(kw) or cleaned_text.startswith(f"@{kw}"):
                matched_kw = kw
                break

        if matched_kw:
            prefix_len = len(matched_kw) + (1 if cleaned_text.startswith(f"@{matched_kw}") else 0)
            cleaned_text = cleaned_text[prefix_len:].strip()

        if require_at:
            if not is_at_bot:
                return False, ""
        else:
            if not is_at_bot and not matched_kw:
                return False, ""

        cleaned_text = cleaned_text.lstrip(",，:： \t\r\n")
        return True, cleaned_text

    def check_rate_limit(self, group_id: str, user_id: str) -> bool:
        """检查单用户与单群冷却限制。触发限流时返回 True。"""
        now = time.monotonic()
        cfg_set = self.settings()
        user_cd = float(cfg_set.get("cooldown_user_seconds", 5.0))
        group_cd = float(cfg_set.get("cooldown_group_seconds", 3.0))

        user_key = (group_id, user_id)
        if user_id and user_key in self._last_user_time:
            if now - self._last_user_time[user_key] < user_cd:
                return True

        if group_id in self._last_group_time:
            if now - self._last_group_time[group_id] < group_cd:
                return True

        if user_id:
            self._last_user_time[user_key] = now
        self._last_group_time[group_id] = now
        return False

    def get_context(self, group_id: str, user_id: str) -> list[dict[str, str]]:
        """获取当前 (group_id, user_id) 仍处于 TTL 期限内的对话上下文。"""
        key = (group_id, user_id)
        now = time.time()
        ttl = float(self.settings().get("context_ttl_seconds", 600.0))

        if key in self._contexts:
            updated_at, history = self._contexts[key]
            if now - updated_at <= ttl:
                return list(history)
            del self._contexts[key]
        return []

    def append_context(self, group_id: str, user_id: str, user_msg: str, bot_msg: str) -> None:
        """写入新一轮对话历史，保持最大滑动轮次。"""
        key = (group_id, user_id)
        max_turns = int(self.settings().get("max_context_turns", 6))
        history = self.get_context(group_id, user_id)

        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": bot_msg})

        if len(history) > max_turns:
            history = history[-max_turns:]

        self._contexts[key] = (time.time(), history)
        if len(self._contexts) > self._max_context_entries:
            self._contexts.popitem(last=False)

    def try_accept_event(
        self,
        event: Mapping[str, object],
        *,
        source: str = "",
    ) -> tuple[bool, str]:
        """尝试接收一条 NapCat 入站群事件并排入 AI 任务队列。"""
        if not self.is_enabled():
            return False, "chat_disabled"

        group_id = str(event.get("group_id") or "").strip()
        user_id = str(event.get("user_id") or "").strip()
        self_id = str(event.get("self_id") or "").strip()

        sender = event.get("sender")
        if not user_id and isinstance(sender, Mapping):
            user_id = str(sender.get("user_id") or "").strip()

        if self_id and user_id and self_id == user_id:
            return False, "self_message"

        if not self.is_group_allowed(group_id):
            return False, "group_not_allowed"

        message_id = str(event.get("message_id") or f"{int(time.time() * 1000)}")

        # 消息 ID 去重与重放防护
        now_ts = time.time()
        dedupe_ttl = float(self.settings().get("context_ttl_seconds", 600.0))
        if message_id in self._seen_messages:
            if now_ts - self._seen_messages[message_id] <= dedupe_ttl:
                return False, "duplicate"
        self._seen_messages[message_id] = now_ts
        if len(self._seen_messages) > self._max_seen_entries:
            self._seen_messages.popitem(last=False)

        text = str(event.get("raw_message") or "")
        triggered, cleaned_text = self.extract_trigger_and_text(event, text, self_id)
        if not triggered:
            return False, "not_triggered"

        if self.check_rate_limit(group_id, user_id):
            return False, "rate_limited"

        # 单条输入长度上限防护
        max_query_length = int(self.settings().get("max_query_length", 500))
        if len(cleaned_text) > max_query_length:
            cleaned_text = cleaned_text[:max_query_length].strip()

        sender_name = ""
        if isinstance(sender, Mapping):
            sender_name = str(sender.get("card") or sender.get("nickname") or "").strip()

        job = NapCatChatJob(
            group_id=group_id,
            user_id=user_id,
            message_id=message_id,
            text=cleaned_text,
            received_at=time.time(),
            self_id=self_id,
            sender_name=sender_name,
        )

        try:
            self._queue.put_nowait(job)
            self._log(
                f"🤖 NapCat AI 触发 | 来自群{group_id}/{sender_name or user_id} | "
                f"提问: {_sanitize_log_text(cleaned_text, 30) or '(仅@呼唤)'}",
                is_debug=True,
            )
            return True, "queued"
        except queue.Full:
            self._log(f"⚠️ NapCat AI 对话队列已满，丢弃群{group_id}请求", is_warning=True)
            return False, "queue_full"

    async def call_cpa(
        self,
        group_id: str,
        user_id: str,
        user_query: str,
    ) -> str:
        """向本地 CPA 发起 OpenAI 兼容接口请求，获取小偶像回复。"""
        if not user_query.strip():
            return "いひひ～戳戳脸颊(ˊᵕˋ˶ ) 找奈央有什么事嘛～？"

        cfg_set = self.settings()
        base_url = str(cfg_set.get("cpa_base_url")).rstrip("/")
        api_key = str(cfg_set.get("cpa_api_key"))
        model = str(cfg_set.get("cpa_model") or "gpt-5.6-luna")
        endpoint_path = str(cfg_set.get("cpa_endpoint") or "/v1/chat/completions")
        system_prompt = str(cfg_set.get("system_prompt"))
        max_query_length = int(cfg_set.get("max_query_length", 500))

        # 截断提问以防 token 滥用
        if len(user_query) > max_query_length:
            user_query = user_query[:max_query_length].strip()

        history = self.get_context(group_id, user_id)
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_query})

        endpoint = f"{base_url}{endpoint_path}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.75,
            "max_tokens": 250,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                if resp.status_code in {401, 403}:
                    self._log("❌ CPA 鉴权失败，请检查 CPA_API_KEY 配置", is_error=True)
                    return "むむ？( ˶'ᵕ'˶) 奈央好像连接不上自己的记忆小本本啦，稍后再找奈央好嘛～"
                if resp.status_code == 404:
                    self._log(f"❌ CPA 接口路径不存在: {endpoint}", is_error=True)
                    return "むむ？( ˶'ᵕ'˶) 奈央找不到回家的路啦，请检查 CPA 接口配置哦～"
                if resp.status_code == 429:
                    self._log("⚠️ CPA 触发限频限制", is_warning=True)
                    return "呼～(՞- -՞) 大家太热情啦，奈央的大脑门要先休息两秒钟，马上回来哦！"

                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices")
                if not choices or not isinstance(choices, list):
                    raise ValueError("CPA 返回结果缺少 choices 字段")
                content = str(choices[0].get("message", {}).get("content") or "").strip()
        except httpx.TimeoutException:
            self._log(f"⚠️ CPA 请求超时: endpoint={endpoint}", is_warning=True)
            return "むむ？奈央刚刚在看相机的取景器走神了( ˶'ᵕ'˶), 能再跟奈央说一遍嘛～？"
        except Exception as exc:
            self._log(f"❌ CPA 请求异常: error={exc}", is_error=True)
            return "诶嘿嘿～奈央刚刚稍微走神戳脸颊去啦(ˊᵕˋ˶ ), 能再跟奈央说一遍嘛？"

        # 代码块脱敏拦截（反代码化兜底保障）
        if "```" in content:
            content = _CODE_BLOCK_REGEX.sub("", content).strip()
            if not content:
                content = "むむ？( ˶'ᵕ'˶) 奈央看着这些符号像天书一样头都大啦！奈央不懂代码，我们聊点草莓大福或甜甜圈吧～いひひ(ˊᵕˋ˶ )"

        content = re.sub(r"^#+\s*", "", content, flags=re.MULTILINE)
        content = content.strip().strip('"').strip("'")
        if not content:
            content = "诶嘿嘿～奈央刚刚稍微走神戳脸颊去啦(ˊᵕˋ˶ ), 能再跟奈央说一遍嘛？"

        self.append_context(group_id, user_id, user_query, content)
        return content

    async def _process_job(self, job: NapCatChatJob) -> None:
        """处理单条聊天任务：同群串行锁保护、调用模型并安全提交发送。"""
        # 同群串行锁，跨群并发
        group_lock = self._group_locks.setdefault(job.group_id, asyncio.Lock())
        async with group_lock:
            started_at = time.monotonic()
            try:
                reply = await self.call_cpa(job.group_id, job.user_id, job.text)
                chain: list[dict[str, object]] = []
                if job.user_id:
                    chain.append({"type": "at", "data": {"qq": job.user_id}})
                    chain.append({"type": "text", "data": {"text": f" {reply}"}})
                else:
                    chain.append({"type": "text", "data": {"text": reply}})

                # 最多重试 1 次，避免网络超时引起群内重复回复
                if self._sender:
                    send_result = await self._sender(int(job.group_id), chain)
                    is_ok = bool(send_result is not False)
                else:
                    from src.platforms.napcat import send_qq_message
                    is_ok = await send_qq_message(int(job.group_id), chain, max_retries=1)

                elapsed = time.monotonic() - started_at
                if not is_ok:
                    self._log(
                        f"❌ NapCat AI 对话发送失败 | 群={job.group_id} | "
                        f"用户={job.user_id} | 耗时 {elapsed:.2f}s",
                        is_warning=True,
                    )
                else:
                    self._log(
                        f"💬 NapCat AI 对话成功 | 群={job.group_id} | "
                        f"用户={job.user_id} | 耗时 {elapsed:.2f}s"
                    )
                    self._log(
                        f"💬 NapCat AI 对话详情 | 问: {_sanitize_log_text(job.text, 25)} | "
                        f"答: {_sanitize_log_text(reply, 25)}",
                        is_debug=True,
                    )
            except Exception as exc:
                elapsed = time.monotonic() - started_at
                self._log(
                    f"❌ NapCat AI 处理异常 | 来自群{job.group_id} | "
                    f"error={type(exc).__name__}: {exc} | 耗时 {elapsed:.2f}s",
                    is_error=True,
                )

    async def _worker(self, worker_id: int) -> None:
        """后台队列消费 Worker。"""
        while self._stop_event is None or not self._stop_event.is_set():
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue

            if job is None:
                self._queue.task_done()
                return

            try:
                await self._process_job(job)
            finally:
                self._queue.task_done()

    async def start(self) -> bool:
        """启动后台 Worker 协程。"""
        if self._workers:
            return True
        if not self.is_enabled():
            return False

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        worker_count = int(self.settings().get("workers", 2))
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"napcat-chat-worker-{i}")
            for i in range(worker_count)
        ]
        self._log(f"🌟 NapCat 冨里奈央 AI 拟人对话模块已就绪 (workers={worker_count})")
        return True

    async def stop(self) -> None:
        """停止 Worker 并彻底清空未消费队列。"""
        if not self._workers:
            return
        if self._stop_event is not None:
            self._stop_event.set()

        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

        for w in self._workers:
            w.cancel()

        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._stop_event = None

        # 彻底清空残留队列与毒丸，防止重载或重启时消费到旧任务
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except Exception:
                break


__all__ = [
    "DEFAULT_TOMISATO_NAO_PROMPT",
    "NapCatChatJob",
    "NapCatChatService",
]
