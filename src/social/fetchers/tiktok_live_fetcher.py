"""
fetchers/tiktok_live_fetcher.py — TikTok 直播监控

职责：
  * 持续检测目标账号是否开播（默认 30s，可配置）
  * 一旦开播立即启动 LiveRecorder 录制（不等直播结束）
  * 程序启动时若目标已在直播 → 同样立即录制
  * 通过 SQLite 会话状态实现「同一场直播只录一次」：
      - 已有 recording 会话且录制进程存活 → 不重复开新任务
      - 会话残留但进程已死（上次异常退出）→ 续录到同一目录，不覆盖旧分段
  * fetch() 返回「开播提醒」Post，交由既有 SyncManager 推送
  * 录制结束由 LiveRecorder 回调 → 推送「录制完成」通知 + 自动发送录像文件

开播检测（三通道，按「越轻越靠前」排序，全部免登录）：
  1. webcast `room/info_by_user`（主）—— 单次请求、未开播时约 120 字节、
     实测 0.3~0.6s。开播时直接带回 room_id / 标题 / 开播时间 / 拉流地址，
     可以省掉一次 yt-dlp 解析，直接开录。这是支撑 5~10s 高频轮询的关键。
  2. yt-dlp `tiktok:live`（备）—— 实测 0.9~1.3s，作为通道 1 异常时的兜底
  3. api-live/user/room + /live 页面正则（末）

关于「事件驱动 / WebSocket 推送」：
  TikTok **没有**公开的开播 Webhook；其 webcast WebSocket（弹幕/礼物事件流）
  必须先持有 room_id 才能连接，只能订阅「已开播」房间的房内事件，
  无法用于发现开播本身。因此开播检测只能退化为高频轮询 ——
  本实现把单次探测成本压到最低（约 120 字节 / 0.3s），
  使 `interval_seconds: 8` 这样的高频轮询在长期运行下依然轻量。
  直播「结束」则可用 `check_alive`（约 100 字节）实时感知。
"""

import logging
import os
import re
import time

import requests

from src.social.fetchers.social_base import SocialFetcher
from src.social.models import Post
from src.social.formatter import build_live_start_message, fmt_ts
from src.social.live_recorder import LiveRecorder
from src.social.tiktok_live_api import (
    TikTokLiveApi,
    TikTokLiveApiError,
    best_stream_url,
)

log = logging.getLogger("collink")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

LIVE_URL = "https://www.tiktok.com/@{account}/live"
ROOM_API = "https://www.tiktok.com/api-live/user/room/"


class TikTokLiveFetcher(SocialFetcher):
    """TikTok 直播开播检测 + 录制调度。"""

    platform_name = "tiktok_live"
    kinds = ("live",)
    _MAX_COMPLETION_DELIVERY_ATTEMPTS = 3

    def __init__(self, config: dict, store=None, downloader=None,
                 on_recording_finished=None):
        """:param on_recording_finished: 录制完成回调（由 SyncManager 注入，用于推送 QQ）"""
        super().__init__(config, store, downloader)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _UA,
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "ja,en;q=0.8",
        })
        self._recorders: dict[str, LiveRecorder] = {}   # session_key -> recorder
        self._on_recording_finished = on_recording_finished
        # 轻量探测客户端（支撑 5~10s 高频轮询）
        self._api = TikTokLiveApi(timeout=self._dl.timeout,
                                  proxy=config.get("proxy") or "")
        # 首次运行守卫对直播无意义（开播就是要立刻通知），显式关闭
        self._first_poll_done = False

    def set_recording_callback(self, cb) -> None:
        self._on_recording_finished = cb

    # ── 主流程 ───────────────────────────────────────────

    def fetch(self) -> list[Post]:
        """检测所有账号；已在直播的账号也会被立即拾起（含程序刚启动的情况）。"""
        self._reap_finished()
        self._retry_pending_completion_notifications()
        if not self._first_poll_done:
            self._first_poll_done = True
            self._recover_stale_sessions()

        posts: list[Post] = []
        for account in self.accounts:
            log.debug("[tiktok_live] 🔎 检测 @%s 是否开播", account)
            try:
                p = self._check_account(account)
                if p:
                    posts.append(p)
            except Exception as e:
                log.warning("[tiktok_live] @%s 检测失败: %s", account,
                            str(e).replace("\n", " ")[:200])
        return posts

    def _check_account(self, account: str) -> Post | None:
        state = self._detect_live(account)
        if not state or not state.get("is_live"):
            return None

        room_id = str(state.get("room_id") or "")
        session_key = f"tiktok_live_{account}_{room_id or 'unknown'}"
        display = self.display_name(account)
        live_url = LIVE_URL.format(account=account)

        # ── 防重复录制 ───────────────────────────────────
        if session_key in self._recorders and self._recorders[session_key].is_alive():
            log.debug("[tiktok_live] %s 已在录制中（本进程），跳过", display)
            # 录制已在跑，但开播提醒可能上次发送失败 → 补发一次（不重启录制）
            sess = self._store.get_live_session(session_key)
            if sess and not sess.get("notified_start"):
                log.info("[tiktok_live] %s 开播提醒尚未送达，补发一次", display)
                return self._build_start_post(
                    account, display, live_url, session_key, room_id,
                    float(sess.get("started_at") or time.time()))
            return None
        if self._store.is_recording(session_key):
            log.info("[tiktok_live] %s 该场直播已有录制任务在运行，跳过", display)
            return None
        sess_existing = self._store.get_live_session(session_key)
        if sess_existing and sess_existing.get("status") == "finished":
            log.debug("[tiktok_live] %s 该场直播（room=%s）已录制完成，跳过",
                      display, room_id)
            return None

        # ── 启动录制 ─────────────────────────────────────
        started_at = float(state.get("started_at") or time.time())
        title = str(state.get("title") or "")
        session = self._store.begin_live_session(
            session_key, platform=self.platform_name, account=account,
            room_id=room_id, title=title, output_dir="", started_at=started_at,
        )
        recorder = LiveRecorder(
            platform=self.platform_name,
            account=account,
            display_name=display,
            room_id=room_id,
            live_url=live_url,
            title=title,
            config=self._config,
            platform_cfg=self.cfg,
            downloader=self._dl,
            store=self._store,
            session_key=session_key,
            session=session,
            on_finish=self._handle_recording_finished,
            # 探测阶段已拿到拉流地址 → 录制线程无需再解析一次，立刻开录
            initial_stream_url=best_stream_url(state.get("stream_urls") or {}),
            stream_resolver=lambda: self._resolve_stream(account, room_id),
        )
        recorder.start()
        self._recorders[session_key] = recorder
        log.info("[tiktok_live] 🔴 发现直播 %s（room=%s），已启动录制",
                 display, room_id or "?")

        # 已通知过开播（例如崩溃后续录）→ 不重复发提醒
        if session.get("notified_start"):
            return None
        return self._build_start_post(account, display, live_url,
                                      session_key, room_id, started_at)

    def _build_start_post(self, account: str, display: str, live_url: str,
                          session_key: str, room_id: str,
                          started_at: float) -> Post:
        """构造开播提醒 Post。

        注意：notified_start 标记在 mark_synced() 里（即**发送成功后**）才写，
        因此 QQ 发送失败时下一轮会自动补发，不会静默丢失开播提醒。
        """
        return Post(
            platform="tiktok_live",
            post_id=f"{session_key}_start",
            author=display,
            text=build_live_start_message(
                author=display,
                start_time=fmt_ts(started_at) or time.strftime("%Y-%m-%d %H:%M:%S"),
                live_url=live_url,
            ),
            media=[],
            timestamp="",
            extra={
                "account": account,
                "kind": "live_start",
                "url": live_url,
                "session_key": session_key,
                "room_id": room_id,
                # 该文本已是最终格式，通知 forwarder 原样发送（不套模板、不翻译）
                "raw_message": True,
            },
        )

    def _resolve_stream(self, account: str, room_id: str) -> str | None:
        """录制线程断流重连时的取流回调 —— 优先走轻量接口，失败返回 None 交给 yt-dlp。

        返回 None 有两种含义（由录制线程按宽限期处理）：确实已下播，或接口暂时异常。
        """
        # room_id 已知时 check_alive 最省（约 100 字节）
        if room_id:
            alive = self._api.check_alive(room_id)
            if alive is False:
                return None
            if alive:
                info = self._api.room_info(room_id)
                room = info.get("room") or info
                from src.social.tiktok_live_api import extract_stream_urls
                url = best_stream_url(extract_stream_urls(room))
                if url:
                    return url
        try:
            state = self._api.probe_live(account)
        except TikTokLiveApiError:
            return None
        if not state.get("is_live"):
            return None
        return best_stream_url(state.get("stream_urls") or {}) or None

    # ── 开播检测 ─────────────────────────────────────────

    def _detect_live(self, account: str) -> dict | None:
        """返回 {is_live, room_id, title, started_at, stream_urls} —— 三通道回退。"""
        # 通道 1：webcast 轻量探测（最快最省，支撑高频轮询）
        if self.cfg.get("fast_detect", True):
            try:
                state = self._api.probe_live(account)
                if state.get("is_live"):
                    log.debug("[tiktok_live] 快速探测命中：@%s 直播中 room=%s",
                              account, state.get("room_id"))
                return state
            except TikTokLiveApiError as e:
                # 接口异常（网络/风控）→ 不能当成「未开播」，继续走下一通道
                log.debug("[tiktok_live] 快速探测失败，回退 yt-dlp: %s",
                          str(e).replace("\n", " ")[:160])

        # 通道 2：yt-dlp
        try:
            info = self._dl.extract_info_strict(
                LIVE_URL.format(account=account), platform_cfg=self.cfg,
            )
            if info:
                if info.get("is_live"):
                    return {
                        "is_live": True,
                        "room_id": str(info.get("id") or ""),
                        "title": info.get("title") or "",
                        "started_at": info.get("timestamp") or time.time(),
                    }
                return {"is_live": False}
        except Exception as e:
            msg = str(e)
            if "not currently live" in msg.lower() or "UserNotLive" in msg:
                return {"is_live": False}
            log.debug("[tiktok_live] yt-dlp 检测异常，回退 HTTP API: %s",
                      msg.replace("\n", " ")[:160])

        # 通道 3：api-live/user/room + /live 页面
        return self._detect_live_http(account)

    def _detect_live_http(self, account: str) -> dict | None:
        try:
            r = self._session.get(
                ROOM_API,
                params={"aid": "1988", "sourceType": "54", "uniqueId": account},
                timeout=self._dl.timeout,
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            data = r.json() or {}
            live_room = ((data.get("data") or {}).get("liveRoom") or {})
            status = int(live_room.get("status") or 0)
            room_id = str(((data.get("data") or {}).get("user") or {})
                          .get("roomId") or live_room.get("roomId") or "")
            # status: 2 = 直播中，4 = 已结束
            if status == 2:
                return {
                    "is_live": True,
                    "room_id": room_id,
                    "title": live_room.get("title") or "",
                    "started_at": float(live_room.get("startTime") or 0) or time.time(),
                }
            if status:
                return {"is_live": False}
        except Exception as e:
            log.debug("[tiktok_live] api-live 检测失败: %s",
                      str(e).replace("\n", " ")[:160])

        # 通道 3：直接抓 /live 页面（最后兜底）
        try:
            r = self._session.get(LIVE_URL.format(account=account),
                                  timeout=self._dl.timeout)
            if r.status_code != 200:
                return None
            text = r.text
            m = re.search(r'"roomId"\s*:\s*"?(\d+)"?', text)
            room_id = m.group(1) if m else ""
            live_now = ('"status":2' in text.replace(" ", "")
                        or '"isLiveBroadcast":true' in text.replace(" ", ""))
            if live_now and room_id:
                return {"is_live": True, "room_id": room_id,
                        "title": "", "started_at": time.time()}
            return {"is_live": False}
        except Exception:
            return None

    # ── 恢复与清理 ───────────────────────────────────────

    def _recover_stale_sessions(self) -> None:
        """程序重启后把「上次崩溃时仍在录制」的会话标记为 crashed。

        标记后 is_recording() 返回 False，下一轮检测若主播仍在直播，
        会以同一 session_key 续录到同一目录（分段编号自动接续）。
        """
        stale = self._store.stale_recording_sessions(self.platform_name)
        for s in stale:
            self._store.update_live_session(s["session_key"], status="crashed")
            log.info("[tiktok_live] 🔧 恢复：会话 %s 上次异常中断，将在主播仍开播时续录",
                     s["session_key"])

    def _reap_finished(self) -> None:
        """回收已结束的录制线程。"""
        for key in [k for k, r in self._recorders.items() if not r.is_alive()]:
            self._recorders.pop(key, None)

    def _result_from_session(self, session: dict):
        """把持久化会话重建为录制结果，以便进程重启后重试完成通知。"""
        output_dir = str(session.get("output_dir") or "")
        parts = []
        if output_dir and os.path.isdir(output_dir):
            try:
                parts = sorted(
                    os.path.abspath(os.path.join(output_dir, name))
                    for name in os.listdir(output_dir)
                    if name.startswith("part_")
                    and os.path.isfile(os.path.join(output_dir, name))
                    and os.path.getsize(os.path.join(output_dir, name)) > 0
                )
            except OSError:
                pass
        total_bytes = sum(os.path.getsize(path) for path in parts)
        from src.social.live_recorder import RecordingResult
        account = str(session.get("account") or "")
        started_at = float(session.get("started_at") or 0)
        ended_at = float(session.get("ended_at") or time.time())
        return RecordingResult(
            session_key=session["session_key"], account=account,
            display_name=self.display_name(account),
            room_id=str(session.get("room_id") or ""),
            title=str(session.get("title") or ""),
            live_url=LIVE_URL.format(account=account),
            started_at=started_at, ended_at=ended_at,
            output_dir=os.path.abspath(output_dir) if output_dir else "",
            parts=parts, total_bytes=total_bytes,
            total_duration=max(0.0, ended_at - started_at),
            container="", note="重试此前未送达的录制完成通知",
        )

    def _retry_pending_completion_notifications(self) -> None:
        """重试本进程或此前进程未成功送达的录制完成通知。"""
        if not self._on_recording_finished:
            return
        for session in self._store.pending_finished_sessions(self.platform_name):
            result = self._result_from_session(session)
            log.info("[tiktok_live] 🔁 重试录制完成通知：%s", result.session_key)
            self._deliver_recording_finished(result)

    def _deliver_recording_finished(self, result) -> None:
        """调用完成通知回调，且只在已确认成功时标记会话。"""
        if not self._on_recording_finished:
            return
        session = self._store.get_live_session(result.session_key) or {}
        attempts = int(session.get("delivery_attempts") or 0)
        if attempts >= self._MAX_COMPLETION_DELIVERY_ATTEMPTS:
            log.warning(
                "[tiktok_live] completion delivery exhausted after %s attempts: %s",
                attempts, result.session_key,
            )
            return
        attempt = attempts + 1
        self._store.update_live_session(
            result.session_key, delivery_attempts=attempt,
        )
        try:
            self._on_recording_finished(result)
        except Exception as e:
            log.error("[tiktok_live] 录制完成通知失败，将在后续轮询重试: %s", e)
            return
        if getattr(result, "delivery_succeeded", False):
            self._store.update_live_session(result.session_key, notified_end=1)
        else:
            if attempt >= self._MAX_COMPLETION_DELIVERY_ATTEMPTS:
                log.warning(
                    "[tiktok_live] completion delivery failed %s/%s; stop retrying: %s",
                    attempt, self._MAX_COMPLETION_DELIVERY_ATTEMPTS,
                    result.session_key,
                )
                return
            log.warning("[tiktok_live] 录制完成通知未确认送达，将在后续轮询重试")

    def _handle_recording_finished(self, result) -> None:
        """LiveRecorder 的完成回调 —— 转交给 SyncManager 推送 QQ。"""
        log.info("[tiktok_live] ✅ %s 录制完成：%s 段 / %s / %s",
                 result.display_name, len(result.parts),
                 result.duration_str, result.size_str)
        self._deliver_recording_finished(result)

    # ── 覆写：直播不需要「首次运行跳过」与常规 mark_synced 语义 ──

    def mark_synced(self, synced_posts: list[Post]) -> None:
        for p in synced_posts:
            if p.platform != self.platform_name:
                continue
            self._store.mark_sent(self.platform_name, p.post_id,
                                  account=p.extra.get("account", ""),
                                  kind=p.extra.get("kind", ""))
            # 开播提醒确认送达后才落 notified_start，保证发送失败能补发
            if (p.extra.get("kind") == "live_start"
                    and p.extra.get("session_key")):
                self._store.update_live_session(p.extra["session_key"],
                                                notified_start=1)

    def stop_all(self, reason: str = "手动停止") -> None:
        """停止本进程内所有录制（供外部优雅关闭调用）。"""
        for r in list(self._recorders.values()):
            try:
                r.stop(reason=reason)
            except Exception:
                pass
