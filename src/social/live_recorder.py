"""
social/live_recorder.py — 直播录制引擎

职责（每场直播一个 LiveRecorder 线程）：
  * 用 yt-dlp 解析最高画质直播流地址（保留原始音轨）
  * 调 ffmpeg 以 -c copy 无损录制，默认输出 MP4
  * 长直播自动分段（默认 30 分钟一段，可配置）
  * 网络异常 / 接口异常 / 主播短暂断流 → 自动重连续录（不新建会话）
  * 超过「结束等待时间」仍未恢复 → 判定直播结束，停止录制
  * MP4 不可用时自动回退容器：mp4 → flv → ts（保存原始直播流）
  * 结束后用 ffprobe 检查每段完整性，汇总时长 / 体积
  * 通过 SocialStore 持久化会话状态，实现「同一场直播只录一次」与重启恢复

进程退出时（atexit）会向 ffmpeg 发送 'q' 优雅停止，保证 MP4 索引写完整。
"""

import atexit
import logging
import os
import signal
import subprocess
import threading
import time

from src.social.downloader import MediaDownloader
from src.social.formatter import fmt_duration, fmt_size, fmt_ts
from src.social.store import SocialStore

log = logging.getLogger("collink")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 容器回退链：首选 mp4，不行退 flv（原始直播流容器），最后退 ts
_CONTAINER_CHAIN = {
    "mp4": ["mp4", "flv", "ts"],
    "flv": ["flv", "ts", "mp4"],
    "ts":  ["ts", "flv", "mp4"],
}

# 全部活跃录制器（供 atexit 统一优雅停止）
_ACTIVE: set["LiveRecorder"] = set()
_ACTIVE_LOCK = threading.Lock()

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
# ffmpeg 必须能收到 SIGINT 才会写完 MP4 索引（moov atom）再退出。
# Windows 上要先把它放进独立进程组，才能定向发送 CTRL_BREAK_EVENT，
# 否则只能 terminate() 强杀 —— 那样产出的 MP4 没有 moov，完全无法播放。
_NEW_GROUP = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
              if os.name == "nt" else 0)


def _shutdown_all() -> None:
    """进程退出钩子：优雅停止所有录制，避免留下损坏的 MP4。"""
    with _ACTIVE_LOCK:
        recorders = list(_ACTIVE)
    for r in recorders:
        try:
            r.stop(reason="进程退出")
        except Exception:
            pass
    for r in recorders:
        try:
            r.join(timeout=20)
        except Exception:
            pass


atexit.register(_shutdown_all)


def safe_name(s: str) -> str:
    """清洗成安全的文件名片段（Windows 兼容）。"""
    out = []
    for ch in str(s):
        out.append("_" if ch in '\\/:*?"<>|\r\n\t' else ch)
    return "".join(out).strip(" .") or "unknown"


class RecordingResult:
    """一场直播的录制结果（传给通知回调）。"""

    def __init__(self, *, session_key: str, account: str, display_name: str,
                 room_id: str, title: str, live_url: str,
                 started_at: float, ended_at: float,
                 output_dir: str, parts: list[str],
                 total_bytes: int, total_duration: float,
                 container: str, note: str = ""):
        self.session_key = session_key
        self.account = account
        self.display_name = display_name
        self.room_id = room_id
        self.title = title
        self.live_url = live_url
        self.started_at = started_at
        self.ended_at = ended_at
        self.output_dir = output_dir
        self.parts = parts
        self.total_bytes = total_bytes
        self.total_duration = total_duration
        self.container = container
        self.note = note
        # Completion delivery is confirmed by SocialForwarder only after every
        # required QQ message/file succeeds; the fetcher persists that outcome.
        self.delivery_succeeded = False

    # ── 便于格式化的派生属性 ─────────────────────────────

    @property
    def start_str(self) -> str:
        return fmt_ts(self.started_at)

    @property
    def end_str(self) -> str:
        return fmt_ts(self.ended_at)

    @property
    def duration_str(self) -> str:
        # 优先用 ffprobe 实测时长，拿不到就用挂钟时间兜底
        secs = self.total_duration or (self.ended_at - self.started_at)
        return fmt_duration(secs)

    @property
    def size_str(self) -> str:
        return fmt_size(self.total_bytes)


class LiveRecorder(threading.Thread):
    """单场直播的录制线程。"""

    def __init__(self, *, platform: str, account: str, display_name: str,
                 room_id: str, live_url: str, title: str,
                 config: dict, platform_cfg: dict,
                 downloader: MediaDownloader, store: SocialStore,
                 session_key: str, session: dict,
                 on_finish=None, initial_stream_url: str = "",
                 stream_resolver=None):
        """
        :param initial_stream_url: 检测阶段已拿到的拉流地址 —— 首段录制直接用它，
            省掉一次 yt-dlp 解析（约 1 秒），做到「发现即开录」
        :param stream_resolver: 可选的轻量取流回调（断流重连时优先调用）；
            返回 None 表示已下播或接口异常，届时回退 yt-dlp
        """
        super().__init__(name=f"rec-{account}", daemon=True)
        self._platform = platform
        self._account = account
        self._display_name = display_name or account
        self._room_id = str(room_id or "")
        self._live_url = live_url
        self._title = title or ""
        self._config = config
        self._platform_cfg = platform_cfg
        self._dl = downloader
        self._store = store
        self._session_key = session_key
        self._session = session
        self._on_finish = on_finish
        self._initial_stream_url = initial_stream_url or ""
        self._stream_resolver = stream_resolver

        self._stop_evt = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._container = str(platform_cfg.get("format", "mp4") or "mp4").lower()
        self._started_at = float(session.get("started_at") or time.time())
        self._session_dir = session.get("output_dir") or self._make_session_dir()
        self._stop_reason = ""

    # ── 目录 ─────────────────────────────────────────────

    def _make_session_dir(self) -> str:
        root = self._platform_cfg.get("output_dir") or "recordings/tiktok_live"
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self._started_at))
        rid = safe_name(self._room_id) if self._room_id else "room"
        d = os.path.join(root, safe_name(self._account), f"{stamp}_{rid}")
        os.makedirs(d, exist_ok=True)
        return d

    # ── 生命周期 ──────────────────────────────────────────

    def stop(self, reason: str = "") -> None:
        """请求停止录制（优雅结束 ffmpeg，保证文件可播）。"""
        if reason and not self._stop_reason:
            self._stop_reason = reason
        self._stop_evt.set()
        self._graceful_stop_ffmpeg()

    @property
    def session_key(self) -> str:
        return self._session_key

    @property
    def session_dir(self) -> str:
        return self._session_dir

    def run(self) -> None:
        with _ACTIVE_LOCK:
            _ACTIVE.add(self)
        os.makedirs(self._session_dir, exist_ok=True)
        self._store.update_live_session(
            self._session_key, output_dir=self._session_dir, status="recording",
            pid=os.getpid(),
        )
        log.info("[live:rec] 🔴 开始直播录制 %s → %s",
                 self._display_name, self._session_dir)

        grace = max(10, int(self._platform_cfg.get("end_grace_seconds", 90)))
        reconnect_delay = max(1, int(self._platform_cfg.get("reconnect_delay_seconds", 5)))
        containers = _CONTAINER_CHAIN.get(self._container, _CONTAINER_CHAIN["mp4"])
        container_idx = 0
        offline_since: float | None = None

        try:
            while not self._stop_evt.is_set():
                stream_url = self._resolve_stream_url()

                if not stream_url:
                    # 断流 / 接口异常 —— 在宽限期内持续重试（短暂断流后自动续录）
                    if offline_since is None:
                        offline_since = time.time()
                        log.info("[live:rec] ⚠️ %s 取流失败或已断流，%ss 内等待恢复...",
                                 self._display_name, grace)
                    elif time.time() - offline_since >= grace:
                        log.info("[live:rec] ⏹️ %s 断流超过 %ss，判定直播结束",
                                 self._display_name, grace)
                        break
                    if self._stop_evt.wait(reconnect_delay):
                        break
                    continue

                if offline_since is not None:
                    log.info("[live:rec] 🔄 %s 直播已恢复，继续录制", self._display_name)
                    offline_since = None

                container = containers[min(container_idx, len(containers) - 1)]
                start_no = self._next_part_index(container)
                created = self._run_ffmpeg(stream_url, start_no, container)

                if created == 0:
                    # 该容器一个文件都没写出来 → 换下一种容器（保存原始流）
                    if container_idx < len(containers) - 1:
                        container_idx += 1
                        log.warning("[live:rec] %s 容器 %s 录制失败，回退为 %s",
                                    self._display_name, container,
                                    containers[container_idx])
                        continue
                    if offline_since is None:
                        offline_since = time.time()
                    if self._stop_evt.wait(reconnect_delay):
                        break
                    continue

                log.info("[live:rec] 📹 %s 本段录制结束（新增 %s 个文件），检查是否仍在直播...",
                         self._display_name, created)
                if self._stop_evt.wait(2):
                    break
        except Exception as e:
            log.error("[live:rec] ❌ %s 录制线程异常: %s", self._display_name, e)
            if not self._stop_reason:
                self._stop_reason = f"录制异常: {e}"
        finally:
            self._graceful_stop_ffmpeg()
            with _ACTIVE_LOCK:
                _ACTIVE.discard(self)
            try:
                self._finalize()
            except Exception as e:
                log.error("[live:rec] ❌ %s 收尾失败: %s", self._display_name, e)

    # ── 取流 ─────────────────────────────────────────────

    def _resolve_stream_url(self) -> str | None:
        """取当前最高画质直播流地址；未开播 / 异常返回 None。

        顺序：检测阶段带来的地址（仅首次） → 轻量回调 → yt-dlp。
        """
        # 首段：直接用检测时拿到的地址，做到「发现即开录」
        if self._initial_stream_url:
            url, self._initial_stream_url = self._initial_stream_url, ""
            log.debug("[live:rec] 使用检测阶段获得的拉流地址，跳过二次解析")
            return url

        # 断流重连：优先用轻量接口（比 yt-dlp 快一个数量级）
        if self._stream_resolver:
            try:
                url = self._stream_resolver()
                if url:
                    return url
            except Exception as e:
                log.debug("[live:rec] 轻量取流回调异常: %s", str(e)[:120])

        try:
            info = self._dl.extract_info_strict(
                self._live_url, platform_cfg=self._platform_cfg,
            )
        except Exception as e:
            msg = str(e)
            if "not currently live" in msg.lower() or "UserNotLive" in msg:
                log.debug("[live:rec] %s 当前未开播", self._display_name)
            else:
                log.debug("[live:rec] %s 取流异常: %s", self._display_name,
                          msg.replace("\n", " ")[:160])
            return None
        if not info:
            return None
        if info.get("is_live") is False:
            return None
        if info.get("title") and not self._title:
            self._title = str(info["title"])[:200]
        return _pick_best_stream(info)

    # ── ffmpeg ───────────────────────────────────────────

    def _next_part_index(self, container: str) -> int:
        """已有分段数量 → 下一段起始编号（重启续录时不覆盖旧文件）。"""
        try:
            existing = [f for f in os.listdir(self._session_dir)
                        if f.startswith("part_")]
        except OSError:
            return 0
        max_idx = -1
        for f in existing:
            stem = os.path.splitext(f)[0]
            try:
                max_idx = max(max_idx, int(stem.split("_")[1]))
            except (IndexError, ValueError):
                continue
        return max_idx + 1

    def _run_ffmpeg(self, stream_url: str, start_no: int, container: str) -> int:
        """启动一次 ffmpeg 录制，阻塞直到退出。返回本次新写出的文件数。"""
        ffmpeg = self._dl.ffmpeg_path()
        if not ffmpeg:
            log.error("[live:rec] 未找到 ffmpeg，无法录制直播"
                      "（请安装 ffmpeg 或在 config.json → media.ffmpeg_path 指定路径）")
            return 0

        before = set(self._list_parts())
        seg_enabled = bool(self._platform_cfg.get("segment_enabled", True))
        # 下限 10s：既防止配置成 0 导致 ffmpeg 疯狂切片，也允许自检脚本用短分段
        seg_sec = max(10, int(float(self._platform_cfg.get("segment_minutes", 30)) * 60))

        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
            "-user_agent", _UA,
            "-headers", "Referer: https://www.tiktok.com/\r\n",
            # HTTP 层自动重连（网络抖动不会立刻中断录制）
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-rw_timeout", "20000000",       # 20s 无数据则报错退出，交由外层重连
            # 直播流开头的探测窗口给足，否则可能因拿不到视频编码参数
            # 而只录下音轨（"Could not find codec parameters"）
            "-analyzeduration", "10000000",  # 10s
            "-probesize", "10000000",        # 10 MB
            "-i", stream_url,
            "-map", "0",                     # 显式录制全部流（视频 + 音频）
            "-c", "copy",                    # 无损：保留原始画质与音轨
            # 直播流的时间戳从「开播那一刻」累计（可能已经几小时），
            # 直接 copy 会让录像的 duration 变成离谱的值（实测 5s 录成 1h56m），
            # 且部分播放器/上传接口会因此判定文件非法 —— 归零修正。
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts",
            "-bsf:a", "aac_adtstoasc",
        ]
        # ── 为什么 MP4 要先录成 TS ─────────────────────────
        # MP4 的索引（moov atom）只在 ffmpeg **正常退出**时才写入。直播录制里
        # ffmpeg 随时可能被中止（主播下播、程序退出、断电），而 Windows 上
        # 既无法通过 stdin 'q' 也无法通过 CTRL_BREAK 可靠地让它优雅收尾
        # （CREATE_NO_WINDOW 的子进程没有控制台，收不到 console 事件）。
        # 实测结果就是产出 `moov atom not found` 的废文件，QQ 也拒收。
        #
        # MPEG-TS 是流式容器，不需要文件尾索引 —— 无论 ffmpeg 怎么死，
        # 已写入的部分永远是完整可播的。因此这里先录 TS，
        # 结束后再无损 remux 成标准 MP4（见 _remux_to_mp4）。
        rec_ext = "ts" if container == "mp4" else container
        seg_fmt = "mpegts" if rec_ext == "ts" else rec_ext

        if seg_enabled:
            cmd += [
                "-f", "segment",
                "-segment_time", str(seg_sec),
                "-segment_format", seg_fmt,
                "-reset_timestamps", "1",
                "-segment_start_number", str(start_no),
                os.path.join(self._session_dir, f"part_%03d.{rec_ext}"),
            ]
        else:
            cmd += [os.path.join(self._session_dir,
                                 f"part_{start_no:03d}.{rec_ext}")]
        # aac_adtstoasc 只在写 MP4/MOV 时需要；录 TS/FLV 时必须去掉
        i = cmd.index("-bsf:a")
        del cmd[i:i + 2]

        log.debug("[live:rec] ffmpeg cmd: %s", " ".join(cmd))
        try:
            with self._proc_lock:
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    creationflags=_NO_WINDOW | _NEW_GROUP,
                )
            proc = self._proc
            _, stderr = proc.communicate()
            rc = proc.returncode
        except Exception as e:
            log.error("[live:rec] ffmpeg 启动失败: %s", e)
            return 0
        finally:
            with self._proc_lock:
                self._proc = None

        after = set(self._list_parts())
        created = len(after - before)
        if rc not in (0, 255) and created == 0:
            tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
            log.warning("[live:rec] ffmpeg 退出码 %s: %s", rc, " | ".join(tail))
        return created

    def _graceful_stop_ffmpeg(self) -> None:
        """优雅停止 ffmpeg，确保 MP4 索引（moov atom）写完整。

        三级递进：
          1. 向 stdin 写 'q'（ffmpeg 交互指令；管道场景下 Windows 上不可靠）
          2. 发送 SIGINT —— Windows 用 CTRL_BREAK_EVENT（需独立进程组），
             这是让 ffmpeg「停止录制并写完文件尾」的标准方式
          3. 仍不退出才 terminate/kill（此时文件可能损坏，属于最后手段）
        """
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return

        # 1) stdin 'q'（POSIX 上通常有效）
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write(b"q\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass

        # 2) SIGINT / CTRL_BREAK
        # 注意：Windows 上子进程以 CREATE_NO_WINDOW 启动时没有控制台，
        # 收不到 console 事件，这一步大概率无效 —— 所以录制容器用 TS，
        # 不依赖 ffmpeg 优雅退出也能得到完整文件。
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        except Exception as e:
            log.debug("[live:rec] 发送中断信号失败: %s", e)
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass

        # 3) 直接结束进程。TS 是流式容器，此时文件依然完整可播，
        #    因此不必为了「优雅退出」再等下去（等待期间还会多录一段）。
        log.debug("[live:rec] ffmpeg 未响应中断信号，直接结束进程"
                  "（TS 容器不受影响）")
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ── 收尾 ─────────────────────────────────────────────

    def _list_parts(self, exts: tuple[str, ...] | None = None) -> list[str]:
        out = []
        try:
            for name in sorted(os.listdir(self._session_dir)):
                if not name.startswith("part_"):
                    continue
                if exts and not name.lower().endswith(exts):
                    continue
                fp = os.path.join(self._session_dir, name)
                if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                    out.append(os.path.abspath(fp))
        except OSError:
            pass
        return out

    def _remux_to_mp4(self, parts: list[str]) -> list[str]:
        """把录制期间产生的 TS 分段无损转成标准 MP4。

        录制用 TS 是为了「怎么死都不会坏」，但 TS 兼容性差（QQ 上传直接拒收），
        所以收尾时统一 remux 成带 faststart 的普通 MP4。
        `-c copy` 不重新编码，画质与音轨完全无损。

        任一分段 remux 失败就保留原始 TS —— 对应需求里的
        「如果无法保存 MP4，则保存原始直播流」。
        """
        ffmpeg = self._dl.ffmpeg_path()
        if not ffmpeg:
            return parts

        out: list[str] = []
        converted = 0
        for src in parts:
            if not src.lower().endswith(".ts"):
                out.append(src)
                continue
            dst = os.path.splitext(src)[0] + ".mp4"
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                out.append(dst)
                continue
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-i", src, "-map", "0", "-c", "copy",
                   "-bsf:a", "aac_adtstoasc",
                   "-movflags", "+faststart", dst]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=3600,
                                   creationflags=_NO_WINDOW)
            except Exception as e:
                log.warning("[live:rec] remux 失败（保留 TS）: %s", e)
                out.append(src)
                continue
            if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
                out.append(dst)
                converted += 1
                try:
                    os.unlink(src)     # MP4 已就绪，删掉中间 TS 省空间
                except OSError:
                    pass
            else:
                tail = (r.stderr or b"").decode("utf-8", "replace").strip()
                log.warning("[live:rec] %s remux 为 MP4 失败，保留原始流: %s",
                            os.path.basename(src), tail[-160:])
                out.append(src)
        if converted:
            log.info("[live:rec] 已将 %s 个分段无损转为 MP4", converted)
        return out

    def _finalize(self) -> None:
        """检查完整性、写入会话状态、回调通知。"""
        ended_at = time.time()
        parts = self._list_parts()

        # TS → MP4 无损转封装（可通过 remux_to_mp4=false 关闭）。
        # 关闭或失败时直接保留 TS —— TS 本身就是完整可播的原始直播流。
        if (self._container == "mp4"
                and self._platform_cfg.get("remux_to_mp4", True)):
            parts = self._remux_to_mp4(parts)

        total_bytes = sum(os.path.getsize(p) for p in parts if os.path.exists(p))

        total_duration = 0.0
        notes: list[str] = []
        if self._stop_reason:
            notes.append(self._stop_reason)

        if self._platform_cfg.get("verify_recording", True) and parts:
            total_duration, bad, no_video = self._verify_parts(parts)
            if bad:
                notes.append(f"{len(bad)} 个分段完整性异常")
                log.warning("[live:rec] ⚠️ %s 有 %s 个分段无法解析: %s",
                            self._display_name, len(bad),
                            ", ".join(os.path.basename(b) for b in bad[:3]))
            if no_video:
                notes.append(f"{len(no_video)} 个分段缺少视频轨")
                log.warning("[live:rec] ⚠️ %s 有 %s 个分段只有音频、缺少视频轨: %s",
                            self._display_name, len(no_video),
                            ", ".join(os.path.basename(b) for b in no_video[:3]))
            if not bad and not no_video:
                log.info("[live:rec] ✅ %s 录像完整性检查通过（%s 段 / %s / %s，"
                         "音视频轨齐全）",
                         self._display_name, len(parts),
                         fmt_duration(total_duration), fmt_size(total_bytes))

        if not parts:
            notes.append("未产生任何录像文件")
            log.warning("[live:rec] ⚠️ %s 直播结束但没有录到任何文件", self._display_name)

        self._store.update_live_session(
            self._session_key, status="finished", ended_at=ended_at, pid=0,
            title=self._title,
        )
        log.info("[live:rec] ⏹️ %s 直播录制结束，保存完成: %s",
                 self._display_name, self._session_dir)

        if self._on_finish:
            result = RecordingResult(
                session_key=self._session_key,
                account=self._account,
                display_name=self._display_name,
                room_id=self._room_id,
                title=self._title,
                live_url=self._live_url,
                started_at=self._started_at,
                ended_at=ended_at,
                output_dir=os.path.abspath(self._session_dir),
                parts=parts,
                total_bytes=total_bytes,
                total_duration=total_duration,
                container=self._container,
                note="；".join(notes),
            )
            try:
                self._on_finish(result)
            except Exception as e:
                log.error("[live:rec] 录制完成回调失败: %s", e)

    def _verify_parts(self, parts: list[str]) -> tuple[float, list[str], list[str]]:
        """用 ffprobe 检查每段完整性。

        既校验时长可解析，也校验视频轨存在 —— 直播取流若在开头丢了
        视频编码参数，会录成「只有声音」的文件，只看时长是发现不了的。

        :return: (总时长, 无法解析的文件, 缺少视频轨的文件)
        """
        ffprobe = self._dl.ffprobe_path()
        if not ffprobe:
            log.debug("[live:rec] 未找到 ffprobe，跳过完整性检查")
            return 0.0, [], []
        total = 0.0
        bad: list[str] = []
        no_video: list[str] = []
        for p in parts:
            try:
                out = subprocess.run(
                    [ffprobe, "-v", "error",
                     "-show_entries", "format=duration:stream=codec_type",
                     "-of", "default=noprint_wrappers=1:nokey=1", p],
                    capture_output=True, text=True, timeout=60,
                    # Windows 默认按 GBK 解码 ffprobe 输出，遇到非 GBK 字节
                    # 会在读取线程里抛 UnicodeDecodeError，导致完整性检查误判
                    encoding="utf-8", errors="replace",
                    creationflags=_NO_WINDOW,
                ).stdout
                lines = [item.strip() for item in out.splitlines() if item.strip()]
                durations = [item for item in lines if _is_float(item)]
                types = [item for item in lines if not _is_float(item)]
                d = float(durations[0]) if durations else 0.0
                if d <= 0:
                    raise ValueError("时长为 0")
                total += d
                if "video" not in types:
                    no_video.append(p)
            except Exception:
                bad.append(p)
        return total, bad, no_video


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _pick_best_stream(info: dict) -> str | None:
    """从 yt-dlp info 中选出最高画质的直播流地址。

    优先级：分辨率 → 码率 → 是否为 flv/m3u8 直链。
    yt-dlp 对 TikTok 直播会给出 flv 与 hls 两类 format，两者 ffmpeg 都能 -c copy。
    """
    formats = info.get("formats") or []
    best, best_key = None, (-1, -1.0)
    for f in formats:
        url = f.get("url") or ""
        if not url:
            continue
        proto = (f.get("protocol") or "").lower()
        if proto.startswith("mhtml") or f.get("vcodec") == "none":
            continue
        key = (int(f.get("height") or 0), float(f.get("tbr") or 0))
        if key > best_key:
            best_key, best = key, url
    if best:
        return best
    return info.get("url") or None
