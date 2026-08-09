# ============================================================
# qq_official.py — QQ 开放平台官方 Bot 单聊推送（支持多 Bot）
# ============================================================
import asyncio
import base64
import time

import httpx

# 统一通过 cfg.X 访问，热重载后标量值（超时、限速间隔等）才能生效
import config.config as cfg
from src.logger import log_all

_MEDIA_FILE_TYPES = {
    "image": 1,
    "video": 2,
    "record": 3,
}


# ──────────────────────────────────────────────
# QQ 官方 Bot 实例类
# ──────────────────────────────────────────────
class QQOfficialBot:
    """单个 QQ 官方 Bot 实例，独立管理 token 和发送状态。"""

    def __init__(self, name: str, app_id: str, client_secret: str, target_openid: str,
                 group_openid: str = "", member_filter: list[str] | None = None):
        self.name = name
        self.app_id = app_id
        self.client_secret = client_secret
        self.target_openid = target_openid
        self.group_openid = group_openid
        self.member_filter: list[str] = member_filter or []

        # 实例级状态
        self._client: httpx.AsyncClient = None
        self._lock: asyncio.Lock = None
        self._access_token: str = ""
        self._token_expire_at: float = 0.0
        self._last_send_ts: float = 0.0

    def initialize(self, client: httpx.AsyncClient) -> None:
        """注入共享的 AsyncClient 实例，并创建串行化锁。"""
        self._client = client
        self._lock = asyncio.Lock()

    def is_configured(self) -> bool:
        """凭证完整即可（target_openid 允许为空——群推送专用 Bot 不需要单聊目标）。"""
        return bool(self.app_id and self.client_secret)

    async def ensure_access_token(self) -> bool:
        """获取并缓存 access_token。"""
        if not self.is_configured():
            log_all(
                f"🚨 官方 QQ Bot [{self.name}] 配置不完整，请检查 APP_ID / CLIENT_SECRET",
                is_error=True,
            )
            return False

        now = time.time()
        if self._access_token and now < self._token_expire_at - 60:
            return True

        try:
            resp = await self._client.post(
                cfg.QQ_OFFICIAL_TOKEN_URL,
                json={
                    "appId": self.app_id,
                    "clientSecret": self.client_secret,
                },
                headers={"Content-Type": "application/json"},
                timeout=cfg.QQ_OFFICIAL_TIMEOUT,
            )
        except Exception as e:
            log_all(
                f"🔥 官方 QQ Bot [{self.name}] 获取 access_token 异常: {type(e).__name__}: {e}",
                is_error=True,
            )
            return False

        if resp.status_code != 200:
            log_all(
                f"🚨 官方 QQ Bot [{self.name}] 获取 access_token 失败: HTTP {resp.status_code} | {resp.text[:200]}",
                is_error=True,
            )
            return False

        try:
            data = resp.json()
        except ValueError:
            log_all(f"🚨 官方 QQ Bot [{self.name}] token 响应不是合法 JSON", is_error=True)
            return False

        token = data.get("access_token")
        if not token:
            log_all(
                f"🚨 官方 QQ Bot [{self.name}] token 响应缺少 access_token: {data}",
                is_error=True,
            )
            return False

        expires_in = int(data.get("expires_in", 7200))
        self._access_token = token
        self._token_expire_at = now + expires_in
        log_all(
            f"✅ 官方 QQ Bot [{self.name}] access_token 已更新，有效期约 {expires_in}s",
            is_debug=True,
        )
        return True

    async def _wait_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_send_ts
        if elapsed < cfg.QQ_OFFICIAL_MIN_INTERVAL:
            await asyncio.sleep(cfg.QQ_OFFICIAL_MIN_INTERVAL - elapsed)

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"QQBot {self._access_token}",
            "Content-Type": "application/json",
        }

    async def _post_json(self, url: str, payload: dict, max_retries: int = 3) -> httpx.Response | None:
        for attempt in range(max_retries):
            await self._wait_rate_limit()
            try:
                resp = await self._client.post(
                    url,
                    json=payload,
                    headers=self._auth_headers(),
                    timeout=cfg.QQ_OFFICIAL_TIMEOUT,
                )
                self._last_send_ts = time.monotonic()

                if resp.status_code in {200, 201}:
                    return resp

                log_all(
                    f"⚠️ 官方 QQ Bot [{self.name}] 请求失败 ({attempt + 1}/{max_retries}): "
                    f"HTTP {resp.status_code} | {resp.text[:200]}",
                    is_error=True,
                )
                if resp.status_code == 401:
                    self._access_token = ""
                    if not await self.ensure_access_token():
                        return None
                elif resp.status_code in {429, 500, 502, 503, 504} and attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None

            except Exception as e:
                log_all(
                    f"🔥 官方 QQ Bot [{self.name}] 请求异常 ({attempt + 1}/{max_retries}): "
                    f"{type(e).__name__}: {e}",
                    is_error=True,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        return None

    async def send_text(self, text: str, max_retries: int = 3) -> bool:
        """向配置的目标 openid 发送单聊纯文本消息。"""
        if not text.strip():
            return False

        async with self._lock:
            if not await self.ensure_access_token():
                return False

            url = f"{cfg.QQ_OFFICIAL_API_BASE}/v2/users/{self.target_openid}/messages"
            payload = {
                "content": text[:1900],
                "msg_type": 0,
            }
            return await self._post_json(url, payload, max_retries) is not None

    def _target_base(self, scope: str, target_openid: str) -> str:
        """scope: 'users' | 'groups'。构造 v2 目标基础 URL。"""
        return f"{cfg.QQ_OFFICIAL_API_BASE}/v2/{scope}/{target_openid}"

    async def _upload_media(self, media_type: str, content: bytes,
                            *, scope: str = "users", target_openid: str | None = None) -> str | None:
        if not await self.ensure_access_token():
            return None

        file_type = _MEDIA_FILE_TYPES.get(media_type)
        if not file_type:
            return None

        openid = target_openid or self.target_openid
        url = f"{self._target_base(scope, openid)}/files"
        payload = {
            "file_type": file_type,
            "file_data": base64.b64encode(content).decode("ascii"),
            "srv_send_msg": False,
        }
        resp = await self._post_json(url, payload)
        if resp is None:
            return None

        try:
            data = resp.json()
        except ValueError:
            log_all(f"⚠️ 官方 QQ Bot [{self.name}] 媒体上传响应不是合法 JSON", is_error=True)
            return None

        file_info = data.get("file_info") or data.get("data", {}).get("file_info")
        if not file_info:
            log_all(
                f"⚠️ 官方 QQ Bot [{self.name}] 媒体上传响应缺少 file_info: {data}",
                is_error=True,
            )
            return None
        return file_info

    async def _send_uploaded_media(self, file_info: str,
                                    *, scope: str = "users", target_openid: str | None = None) -> bool:
        openid = target_openid or self.target_openid
        url = f"{self._target_base(scope, openid)}/messages"
        payload = {
            "msg_type": 7,
            "media": {
                "file_info": file_info,
            },
        }
        return await self._post_json(url, payload) is not None

    async def _send_chain(self, scope: str, target_openid: str, member: dict,
                          message_chain: list[dict],
                          media_payloads: list[tuple[str, bytes | None]] | None = None) -> bool:
        """共享的链式发送核心：文字 + 媒体。scope='users'|'groups'。"""
        async with self._lock:
            if not await self.ensure_access_token():
                return False

            ok = True
            text = chain_to_text(message_chain)
            if text:
                url = f"{self._target_base(scope, target_openid)}/messages"
                if await self._post_json(url, {"content": text[:1900], "msg_type": 0}) is None:
                    ok = False

            if media_payloads is None:
                media_payloads = await download_media_payloads(member, message_chain)
            for media_type, content in media_payloads:
                if content is None:
                    ok = False
                    continue
                file_info = await self._upload_media(media_type, content, scope=scope, target_openid=target_openid)
                if not file_info or not await self._send_uploaded_media(file_info, scope=scope, target_openid=target_openid):
                    ok = False

            return ok

    async def send_message_chain(self, member: dict, message_chain: list[dict],
                                 media_payloads: list[tuple[str, bytes | None]] | None = None) -> bool:
        """向配置的目标 openid 发送单聊完整消息链。"""
        return await self._send_chain("users", self.target_openid, member, message_chain, media_payloads)

    async def send_message_chain_to_group(self, group_openid: str, member: dict,
                                          message_chain: list[dict],
                                          media_payloads: list[tuple[str, bytes | None]] | None = None) -> bool:
        """向指定群聊发送完整消息链。"""
        return await self._send_chain("groups", group_openid, member, message_chain, media_payloads)

    async def send_group_text(self, group_openid: str, text: str, max_retries: int = 3) -> bool:
        """向指定群聊发送纯文本消息。"""
        if not text.strip():
            return False
        async with self._lock:
            if not await self.ensure_access_token():
                return False
            url = f"{self._target_base('groups', group_openid)}/messages"
            return await self._post_json(url, {"content": text[:1900], "msg_type": 0}, max_retries) is not None


# ──────────────────────────────────────────────
# 模块级 Bot 注册表 & 媒体下载
# ──────────────────────────────────────────────
_bots: list[QQOfficialBot] = []
_client: httpx.AsyncClient | None = None   # 媒体下载用（与各 Bot 共享同一实例）


async def _download_media(file_url: str, source_headers: dict[str, str]) -> bytes | None:
    """下载 message 私有媒体资源。用独立的媒体超时 —— 25MB 视频跑不进 API 的 15s。"""
    try:
        resp = await _client.get(
            file_url,
            headers=source_headers,
            follow_redirects=True,
            timeout=cfg.QQ_OFFICIAL_MEDIA_TIMEOUT,
        )
    except Exception as e:
        log_all(f"🔥 官方 QQ Bot 下载媒体异常: {type(e).__name__}: {e}", is_error=True)
        return None

    if resp.status_code != 200:
        log_all(f"⚠️ 官方 QQ Bot 下载媒体失败: HTTP {resp.status_code}", is_error=True)
        return None

    content = resp.content
    if len(content) > cfg.QQ_OFFICIAL_MEDIA_MAX_BYTES:
        log_all(f"⚠️ 官方 QQ Bot 媒体过大，跳过 ({len(content)} bytes)", is_error=True)
        return None
    return content


async def download_media_payloads(member: dict,
                                  message_chain: list[dict]) -> list[tuple[str, bytes | None]]:
    """把消息链中的媒体段逐个下载为 (type, bytes|None)，None 表示该项下载失败。
    多 Bot 场景下由 notifier 调用一次，避免同一文件按 Bot 数重复下载。"""
    items = media_items(message_chain)
    if not items:
        return []

    from config.credentials import get_source_headers_for_account

    headers = get_source_headers_for_account(member["account_id"], member["group_type"])
    payloads: list[tuple[str, bytes | None]] = []
    for media_type, file_url in items:
        payloads.append((media_type, await _download_media(file_url, headers)))
    return payloads


def initialize(client: httpx.AsyncClient) -> None:
    """初始化所有配置的官方 Bot 实例。"""
    global _bots, _client
    _client = client
    _bots = []
    for bot_cfg in cfg.QQ_OFFICIAL_BOTS:
        if not bot_cfg.get("app_id"):
            continue  # 跳过未配置的 Bot
        bot = QQOfficialBot(
            name=bot_cfg.get("name", "unnamed"),
            app_id=bot_cfg["app_id"],
            client_secret=bot_cfg.get("client_secret", ""),
            target_openid=bot_cfg.get("target_openid", ""),
            group_openid=bot_cfg.get("group_openid", ""),
            member_filter=bot_cfg.get("member_filter") or [],
        )
        bot.initialize(client)
        _bots.append(bot)
        log_all(f"📝 注册官方 QQ Bot: {bot.name}")


def get_bots() -> list[QQOfficialBot]:
    """返回所有已初始化的 Bot 实例。"""
    return _bots


def get_configured_bots() -> list[QQOfficialBot]:
    """返回所有配置完整、可用于发送的 Bot 实例。"""
    return [bot for bot in _bots if bot.is_configured()]


def has_bots() -> bool:
    """返回是否存在至少一个配置完整的 Bot。"""
    return any(bot.is_configured() for bot in _bots)


# ──────────────────────────────────────────────
# 工具函数（保持原有接口兼容）
# ──────────────────────────────────────────────
def chain_to_text(message_chain: list[dict]) -> str:
    """提取官方 Bot 文本消息内容。"""
    parts: list[str] = []
    for item in message_chain:
        msg_type = item.get("type")
        data = item.get("data", {})
        if msg_type == "text":
            text = data.get("text", "")
            if text:
                parts.append(text)
    return "".join(parts).strip()


def media_items(message_chain: list[dict]) -> list[tuple[str, str]]:
    """提取可发送给官方 Bot 的媒体段，返回 (type, file_url)。"""
    items: list[tuple[str, str]] = []
    for item in message_chain:
        msg_type = item.get("type")
        if msg_type not in _MEDIA_FILE_TYPES:
            continue
        file_url = item.get("data", {}).get("file", "")
        if file_url:
            items.append((msg_type, file_url))
    return items


async def health_check() -> bool:
    """启动时检查所有 Bot 凭证是否可用。"""
    if not _bots:
        return False
    all_ok = True
    for bot in _bots:
        if await bot.ensure_access_token():
            log_all(f"🟢 官方 QQ Bot [{bot.name}] 凭证正常")
        else:
            log_all(f"🔴 官方 QQ Bot [{bot.name}] 凭证无效", is_error=True)
            all_ok = False
    return all_ok


async def send_text(text: str, max_retries: int = 3) -> bool:
    """向所有 Bot 发送纯文本消息（用于报警）。"""
    if not _bots:
        return False
    all_ok = True
    for bot in _bots:
        if not await bot.send_text(text, max_retries):
            all_ok = False
    return all_ok
