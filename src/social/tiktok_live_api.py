"""
social/tiktok_live_api.py — TikTok 直播轻量探测客户端

为什么需要它：开播检测要做到「尽可能第一时间发现」，就必须高频轮询（5~10 秒）。
用 yt-dlp 做这件事太重（实测 0.9~1.3s / 次，内部还要抓被 WAF 拦的主页），
高频跑既慢又容易触发风控。

这里直接用 TikTok 自己的 webcast 接口，全部免登录：

  * room/info_by_user  —— 用 uniqueId 查开播状态。实测未开播时
    **0.3~0.6s / 约 120 字节**，比 yt-dlp 轻一个数量级；开播时直接带回
    room_id、标题、开播时间，甚至拉流地址（可直接开录，省掉一次 yt-dlp 解析）。
  * room/check_alive   —— 已知 room_id 时判断房间是否还活着。
    实测 **0.22s / 约 100 字节**，用于录制期间的断流判定。

字段命名与 yt-dlp 的 TikTokLiveIE 保持一致（status==2 为直播中、
stream_url.flv_pull_url / hls_pull_url / live_core_sdk_data 等），
避免自行臆测接口结构。

关于「事件驱动 / WebSocket」：
  TikTok 的 webcast WebSocket（弹幕/礼物事件流）**必须先有 room_id 才能连接**，
  也就是只能订阅「已经开播」的房间内事件，无法用来发现开播本身；
  TikTok 也没有公开的开播 Webhook。因此开播检测只能高频轮询，
  本模块的作用就是把单次轮询成本压到最低。
  房间内事件流可用于「直播结束」的实时感知 —— 见 check_alive()。
"""

import json
import logging
import time

import requests

log = logging.getLogger("collink")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

INFO_BY_USER = "https://webcast.tiktok.com/webcast/room/info_by_user/"
ROOM_INFO = "https://webcast.tiktok.com/webcast/room/info/"
CHECK_ALIVE = "https://webcast.tiktok.com/webcast/room/check_alive/"

# 画质优先级（高 → 低）。TikTok 有两套命名：
#   flv_pull_url 用大写（FULL_HD1 / HD1 / SD2 / SD1）
#   live_core_sdk_data 用小写（origin / uhd / hd / sd / ld / ao）
# 统一转小写后按下表排名，未知名字排在已知之后。
_QUALITY_RANK = {
    "origin": 0, "origion": 0, "full_hd1": 1, "uhd": 1,
    "hd1": 2, "hd": 2,
    "sd2": 3, "sd1": 4, "sd": 4,
    "ld1": 5, "ld": 5,
}
# 绝对不能选的画质：ao = audio only（只有音轨，选它会录出没画面的文件）
_AUDIO_ONLY_QUALITIES = {"ao", "audio", "audio_only"}

# 明确表示「未开播」的 status_code，不算错误
_OFFLINE_CODES = {30003}          # room has finished
# 明确表示「账号不存在 / 参数错误」的 status_code
_NOT_FOUND_CODES = {19881007, 10201}


class TikTokLiveApiError(RuntimeError):
    """接口异常（网络错误 / 被风控）—— 让上层退避，不要当成「未开播」。"""


class TikTokLiveApi:
    """webcast 轻量探测客户端（线程安全：每个 fetcher 持有一个实例）。"""

    def __init__(self, timeout: int = 15, proxy: str = ""):
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _UA,
            "Referer": "https://www.tiktok.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ja,en;q=0.8",
        })
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})

    # ── 开播探测 ─────────────────────────────────────────

    def probe_live(self, account: str) -> dict:
        """用 uniqueId 探测开播状态（单次请求，最轻量）。

        :return: {"is_live": bool, "room_id": str, "title": str,
                  "started_at": float, "stream_urls": {...}}
        :raises TikTokLiveApiError: 网络异常或响应无法解析（上层应退避重试）
        """
        try:
            r = self._session.get(
                INFO_BY_USER,
                params={"aid": "1988", "unique_id": account,
                        "sourceType": "54"},
                timeout=self._timeout,
            )
        except Exception as e:
            raise TikTokLiveApiError(f"info_by_user 请求失败: {e}") from e

        if r.status_code != 200:
            raise TikTokLiveApiError(f"info_by_user HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise TikTokLiveApiError(f"info_by_user 返回非 JSON: {e}") from e

        code = body.get("status_code")
        data = body.get("data") or {}

        # 未开播的两种表现：status_code=30003（room has finished），
        # 或 status_code=0 但 data 为空对象
        if code in _OFFLINE_CODES:
            return {"is_live": False}
        if code in _NOT_FOUND_CODES:
            raise TikTokLiveApiError(
                f"账号无效或参数错误（status_code={code}，"
                f"message={data.get('message') or body.get('message')}）")
        if code not in (0, None):
            # 未知非零码 —— 当作接口异常，交给上层退避（可能是风控）
            raise TikTokLiveApiError(
                f"info_by_user 返回 status_code={code} "
                f"message={data.get('message') or body.get('message')}")

        # 两种响应形态都要支持：
        #   room/info_by_user → data 本身就是 room 对象（实测确认）
        #   room/info         → room 嵌在 data.room 下
        room = data.get("room") if isinstance(data.get("room"), dict) else None
        if room is None:
            room = data if (data.get("id_str") or data.get("id")
                            or "status" in data) else {}
        if not room:
            return {"is_live": False}

        # yt-dlp 口径：status == 2 直播中，4 已结束
        status = _to_int(room.get("status"))
        if status is not None and status != 2:
            return {"is_live": False}

        room_id = (str(room.get("id_str") or "") or str(room.get("id") or "")
                   or str(data.get("room_id_str") or "")
                   or str(data.get("room_id") or ""))
        started = _to_int(room.get("create_time")) or 0

        owner = room.get("owner") or {}
        return {
            "is_live": True,
            "room_id": room_id,
            "title": str(room.get("title") or "")[:200],
            "started_at": float(started) if started else time.time(),
            "stream_urls": extract_stream_urls(room),
            "nickname": str(owner.get("nickname") or ""),
            "unique_id": str(owner.get("display_id")
                             or owner.get("unique_id") or ""),
            "user_count": _to_int(room.get("user_count")) or 0,
        }

    def room_info(self, room_id: str) -> dict:
        """已知 room_id 时拉取房间详情（主要用于刷新拉流地址）。"""
        if not room_id:
            return {}
        try:
            r = self._session.get(
                ROOM_INFO, params={"aid": "1988", "room_id": room_id},
                timeout=self._timeout,
            )
            if r.status_code != 200:
                return {}
            return (r.json().get("data") or {})
        except Exception as e:
            log.debug("[tiktok_live:api] room_info 失败: %s", str(e)[:120])
            return {}

    def check_alive(self, room_id: str) -> bool | None:
        """判断房间是否仍在直播（最轻的一个接口，约 100 字节）。

        :return: True 活着 / False 已结束 / None 无法判断（网络异常）
        """
        if not room_id:
            return None
        try:
            r = self._session.get(
                CHECK_ALIVE,
                params={"aid": "1988", "room_ids": str(room_id)},
                timeout=self._timeout,
            )
            if r.status_code != 200:
                return None
            items = (r.json().get("data") or [])
            for it in items:
                if str(it.get("room_id_str") or it.get("room_id")) == str(room_id):
                    return bool(it.get("alive"))
            return None
        except Exception as e:
            log.debug("[tiktok_live:api] check_alive 失败: %s", str(e)[:120])
            return None


# ── 拉流地址解析 ──────────────────────────────────────────

def extract_stream_urls(room: dict) -> dict:
    """从 room 数据中解析各画质拉流地址。

    字段口径对齐 yt-dlp TikTokLiveIE：
      stream_url.live_core_sdk_data.pull_data.stream_data → {画质: {main: {flv, hls}}}
      stream_url.flv_pull_url  → {画质: url}
      stream_url.hls_pull_url / hls_pull_url_map
    :return: {"flv": {quality: url}, "hls": {quality: url}}
    """
    su = room.get("stream_url") or {}
    flv: dict[str, str] = {}
    hls: dict[str, str] = {}

    # 1) live_core_sdk_data（画质最全，含 FULL_HD1）
    raw = (((su.get("live_core_sdk_data") or {}).get("pull_data") or {})
           .get("stream_data"))
    if isinstance(raw, str) and raw.strip():
        try:
            sdk = json.loads(raw)
            for quality, entry in (sdk.get("data") or {}).items():
                main = (entry or {}).get("main") or {}
                if main.get("flv"):
                    flv[quality] = main["flv"]
                if main.get("hls"):
                    hls[quality] = main["hls"]
        except (ValueError, AttributeError):
            pass

    # 2) flv_pull_url / hls_pull_url_map
    for quality, url in (su.get("flv_pull_url") or {}).items():
        if url and quality not in flv:
            flv[quality] = url
    for quality, url in (su.get("hls_pull_url_map") or {}).items():
        if url and quality not in hls:
            hls[quality] = url
    if su.get("hls_pull_url") and "DEFAULT" not in hls:
        hls["DEFAULT"] = su["hls_pull_url"]
    if su.get("rtmp_pull_url") and "DEFAULT" not in flv:
        flv["DEFAULT"] = su["rtmp_pull_url"]

    return {"flv": flv, "hls": hls}


def best_stream_url(stream_urls: dict) -> str:
    """挑选最高画质地址。

    优先 FLV（直播连续流，ffmpeg -c copy 最稳），其次 HLS。
    **始终排除 ao（audio only）画质** —— 选中它会录出没有画面的文件。
    """
    if not stream_urls:
        return ""
    for group in ("flv", "hls"):
        urls = stream_urls.get(group) or {}
        candidates = [(q, u) for q, u in urls.items()
                      if u and q.lower() not in _AUDIO_ONLY_QUALITIES]
        if not candidates:
            continue
        # 已知画质按排名，未知画质排最后但仍优于 ao
        candidates.sort(key=lambda kv: _QUALITY_RANK.get(kv[0].lower(), 50))
        return candidates[0][1]
    return ""


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
