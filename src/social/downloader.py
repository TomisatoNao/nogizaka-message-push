"""
social/downloader.py — 统一媒体下载器

两条下载通道，封装成统一接口，未来接新平台只需调用这里：

  1. download_direct()     —— requests 直下（已知直链的图片 / 视频）
  2. download_via_ytdlp()  —— yt-dlp 下载（需要解析的页面 URL）

统一保证：
  * 图片下载原图（各 fetcher 负责把 URL 改写成原图地址）
  * 视频选择最高画质并合并原始音轨（bv*+ba/b + merge_output_format）
  * 失败自动重试（次数与退避可配置），临时文件 + os.replace 原子落地
  * 多媒体并发下载（线程池，线程数可配置）

yt-dlp 是惰性 import 的：未安装时只影响社交平台，既有功能完全不受影响。
"""

import logging
import json
import os
import re
import shutil
import subprocess  # nosec B404
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from src.social.settings import media_settings

log = logging.getLogger("collink")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".ts", ".m4v"}
_AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".wav", ".ogg", ".opus"}

# yt-dlp 下载产物里需要忽略的中间/附属文件
_IGNORED_EXTS = {".part", ".ytdl", ".temp", ".json", ".description",
                 ".vtt", ".srt", ".lrc", ".ass"}


def classify_media(path: str) -> str:
    """按扩展名判定媒体类型，与既有 MediaItem.type 取值保持一致。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "file"


def _is_media_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext not in _IGNORED_EXTS and os.path.isfile(path)


class YtdlpUnavailable(RuntimeError):
    """yt-dlp 未安装 —— 由调用方转成一条警告日志，不影响其它平台。"""


class _YdlLogger:
    """Discard yt-dlp's internal chatter.

    Each caller translates an extraction result into a platform-specific,
    actionable log line.  Keeping raw extractor text (especially Instagram's
    ambiguous "login required") made the admin log look like a traceback and
    obscured the actual outcome.
    """

    @staticmethod
    def debug(_msg):
        return None

    @staticmethod
    def info(_msg):
        return None

    @staticmethod
    def warning(_msg):
        return None

    @staticmethod
    def error(_msg):
        return None


def _sniff_and_fix_media_file(fpath: str) -> tuple[str, str]:
    """根据文件前 32 字节 Magic Number 识别真实类型，必要时自动修正文件扩展名。"""
    if not os.path.exists(fpath) or os.path.getsize(fpath) == 0:
        return classify_media(fpath), fpath
    try:
        with open(fpath, "rb") as f:
            header = f.read(32)
    except Exception:
        return classify_media(fpath), fpath

    real_type = ""
    correct_ext = ""
    if header.startswith(b"\xff\xd8\xff"):
        real_type = "image"
        correct_ext = ".jpg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        real_type = "image"
        correct_ext = ".png"
    elif header.startswith(b"GIF8"):
        real_type = "image"
        correct_ext = ".gif"
    elif header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WEBP":
        real_type = "image"
        correct_ext = ".webp"
    elif (len(header) >= 8 and (b"ftyp" in header[4:12] or header.startswith(b"\x00\x00\x00"))):
        real_type = "video"
        correct_ext = ".mp4"
    elif header.startswith(b"\x1a\x45\xdf\xa3"):
        real_type = "video"
        correct_ext = ".webm"
    elif header.startswith(b"ID3") or header.startswith(b"\xff\xfb") or header.startswith(b"\xff\xf3") or header.startswith(b"\xff\xf2"):
        real_type = "audio"
        correct_ext = ".mp3"
    elif header.startswith(b"OggS"):
        real_type = "audio"
        correct_ext = ".ogg"
    elif header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WAVE":
        real_type = "audio"
        correct_ext = ".wav"

    curr_ext = os.path.splitext(fpath)[1].lower()
    if real_type and correct_ext and curr_ext != correct_ext:
        new_path = os.path.splitext(fpath)[0] + correct_ext
        try:
            if os.path.exists(new_path):
                os.remove(new_path)
            os.rename(fpath, new_path)
            log.info("[downloader] 🔍 自动纠正媒体文件扩展名: %s -> %s (真实类型: %s)",
                     os.path.basename(fpath), os.path.basename(new_path), real_type)
            return real_type, new_path
        except Exception as ex:
            log.warning("[downloader] 重命名媒体文件失败: %s", ex)

    return real_type or classify_media(fpath), fpath


class MediaDownloader:
    """全平台共享的下载器（线程安全）。"""

    def __init__(self, config: dict):
        self._config = config
        self._lock = threading.Lock()
        self._ytdlp_warned = False
        self._compat_warned = False
        self._instagram_cookie_header = ""
        self._instagram_cookie_at = 0.0

    def download(self, post) -> None:
        """为 Post 对象下载所有尚未下载的媒体（填充 local_path）。"""
        from src.social.live_recorder import safe_name
        from src.social.models import MediaItem

        media_root = self._cfg.get("download_dir", "data/social_media")
        acc = post.extra.get("account") or post.extra.get("username") or post.author or "manual"
        dest_dir = os.path.join(media_root, safe_name(post.platform), safe_name(acc), safe_name(post.post_id))
        os.makedirs(dest_dir, exist_ok=True)

        tasks = []
        for idx, m in enumerate(post.media):
            if m.local_path and os.path.exists(m.local_path):
                continue
            if not m.url:
                continue
            ext = ".mp4" if m.type == "video" else ".jpg"
            dest_file = os.path.join(dest_dir, f"{safe_name(post.post_id)}_{idx+1}{ext}")
            tasks.append((m.url, dest_file, m))

        # 优先使用 requests 直链下载（图片和一般直链速度最快）
        if tasks:
            download_tasks = [(url, dest) for url, dest, _ in tasks]
            referer = (
                "https://www.tiktok.com/" if post.platform in ("tiktok", "douyin")
                else "https://www.instagram.com/" if post.platform == "instagram"
                else ""
            )
            self.download_many(download_tasks, referer=referer)
            for _, dest_file, m in tasks:
                if os.path.exists(dest_file) and os.path.getsize(dest_file) > 0:
                    m.local_path = os.path.abspath(dest_file)
                    m.type, m.local_path = _sniff_and_fix_media_file(m.local_path)

        # 若仍有未下载成功的媒体（例如 TikTok / 抖音等需要会话签名的视频直链 403，或直接为视频）
        unresolved = [m for m in post.media if not (m.local_path and os.path.exists(m.local_path))]
        source_url = post.extra.get("source_url") or getattr(post, "url", "")
        if (unresolved or not post.media) and source_url:
            log.info(f"[download] 使用 yt-dlp 兜底下载 {post.platform} 原始链接: {source_url}")
            try:
                outtmpl = f"{safe_name(post.post_id)}%(autonumber)s.%(ext)s"
                downloaded_files = self.download_via_ytdlp(source_url, dest_dir, outtmpl=outtmpl)
                if not downloaded_files:
                    downloaded_files = _list_media(dest_dir)
                if downloaded_files:
                    if not post.media:
                        for fpath in downloaded_files:
                            mtype = classify_media(fpath)
                            post.media.append(MediaItem(type=mtype, url="", local_path=os.path.abspath(fpath)))
                    else:
                        unresolved_media = [
                            m for m in post.media
                            if not (m.local_path and os.path.exists(m.local_path))
                        ]
                        # ``download_via_ytdlp`` may return files already
                        # attached by the direct-download pass when its
                        # output directory had no newly-created files.  Do
                        # not reuse those paths for unresolved media: doing
                        # so silently sends the same photo multiple times.
                        attached_paths = {
                            os.path.normcase(os.path.abspath(m.local_path))
                            for m in post.media
                            if m.local_path and os.path.exists(m.local_path)
                        }
                        fallback_files = [
                            os.path.abspath(fpath)
                            for fpath in downloaded_files
                            if os.path.normcase(os.path.abspath(fpath)) not in attached_paths
                        ]
                        for media_item, fpath in zip(unresolved_media, fallback_files):
                            media_item.local_path = fpath
                        if len(fallback_files) < len(unresolved_media):
                            log.warning(
                                "[download] 回退下载仍缺少 %s 个媒体，保留未下载状态，"
                                "不会重复复用已有文件",
                                len(unresolved_media) - len(fallback_files),
                            )
            except Exception as e:
                log.warning(f"[download] yt-dlp 兜底下载失败: {e}")

        # 移动端兼容性检查与转码
        downloaded_paths = [m.local_path for m in post.media if m.local_path and os.path.exists(m.local_path)]
        if downloaded_paths:
            self.ensure_mobile_video_compatibility(downloaded_paths)

    # ── 配置读取（每次读取，配置热更新即时生效）──────────

    @property
    def _cfg(self) -> dict:
        return media_settings(self._config)

    @property
    def retry_times(self) -> int:
        return max(1, int(self._cfg.get("retry_times", 3)))

    @property
    def threads(self) -> int:
        return max(1, int(self._cfg.get("download_threads", 4)))

    @property
    def timeout(self) -> int:
        return max(5, int(self._cfg.get("timeout_seconds", 60)))

    @property
    def proxy(self) -> str:
        try:
            import config.config as cfg
        except Exception:
            cfg = None
        candidate = (
            self._config.get("proxy")
            or self._config.get("social", {}).get("proxy")
            or (getattr(cfg, "PROXY", "") if cfg else "")
            or (getattr(cfg, "SOCIAL_CONFIG", {}).get("proxy", "") if cfg else "")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("ALL_PROXY")
            or ""
        )
        return str(candidate).strip()

    def _instagram_public_headers(self) -> dict[str, str]:
        """预热匿名 Embed 会话，返回 CDN 直链所需的短期 cookies。

        Instagram CDN 在部分出口 IP 上会拒绝没有站点匿名 cookies 的
        ``requests`` 直链请求（HTTP 403），而同一 URL 在 Embed 浏览器
        上下文中可以正常下载。Cookies 只保存在内存中，不写入配置、日志
        或归档库；缓存十分钟以避免每个媒体重复访问 Instagram 首页。
        """
        now = time.monotonic()
        with self._lock:
            if self._instagram_cookie_header and now - self._instagram_cookie_at < 600:
                return {
                    "Cookie": self._instagram_cookie_header,
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                }

        session = requests.Session()
        session.headers.update({
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        })
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        cookie_header = ""
        try:
            response = session.get(
                "https://www.instagram.com/",
                timeout=min(self.timeout, 15),
                proxies=proxies,
            )
            try:
                if response.status_code == 200:
                    cookie_header = "; ".join(
                        f"{name}={value}" for name, value in session.cookies.get_dict().items()
                        if name and value
                    )
            finally:
                response.close()
        except (OSError, requests.RequestException) as exc:
            log.debug("[download] Instagram 匿名会话预热失败: %s", type(exc).__name__)

        if not cookie_header:
            return {}
        with self._lock:
            self._instagram_cookie_header = cookie_header
            self._instagram_cookie_at = now
        return {
            "Cookie": cookie_header,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }

    def ffmpeg_path(self) -> str | None:
        """ffmpeg 可执行文件路径（配置优先，其次 PATH）。"""
        p = self._cfg.get("ffmpeg_path") or ""
        if p and os.path.exists(p):
            return p
        return shutil.which("ffmpeg")

    def ffprobe_path(self) -> str | None:
        p = self._cfg.get("ffprobe_path") or ""
        if p and os.path.exists(p):
            return p
        found = shutil.which("ffprobe")
        if found:
            return found
        # 常见情况：ffmpeg 与 ffprobe 同目录
        ff = self.ffmpeg_path()
        if ff:
            cand = os.path.join(os.path.dirname(ff),
                                "ffprobe.exe" if os.name == "nt" else "ffprobe")
            if os.path.exists(cand):
                return cand
        return None

    def ensure_mobile_video_compatibility(self, files: list[str]) -> list[str]:
        """把需要兼容的下载视频原地转为 H.264/AAC。

        TikTok 偶尔会交付 HEVC/H.265（hvc1/hev1）MP4；部分手机浏览器
        无法播放该编码。只转换检测到的 HEVC 文件，其他视频不重新编码。
        使用临时文件完成后再原子替换，调用方和归档始终引用原始文件名。
        """
        if not self._cfg.get("mobile_video_transcode", True):
            return files

        ffmpeg = self.ffmpeg_path()
        ffprobe = self.ffprobe_path()
        if not ffmpeg or not ffprobe:
            if not self._compat_warned:
                self._compat_warned = True
                log.warning("[download] 未找到 ffmpeg/ffprobe，跳过移动端视频兼容转码")
            return files

        for path in files:
            if classify_media(path) != "video":
                continue
            codec = self._video_codec(path, ffprobe)
            if codec not in {"hevc", "h265", "hvc1", "hev1"}:
                continue
            self._transcode_h264_in_place(path, ffmpeg)
        return files

    @staticmethod
    def _video_codec(path: str, ffprobe: str) -> str:
        """返回首个视频流的标准编码名；探测失败时返回空字符串。"""
        try:
            result = subprocess.run(  # nosec B603
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,codec_tag_string",
                 "-of", "json", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, check=False,
            )
            data = json.loads(result.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            return str(stream.get("codec_name") or stream.get("codec_tag_string") or "").lower()
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
            log.debug("[download] 无法探测视频编码: %s", os.path.basename(path))
            return ""

    @staticmethod
    def _transcode_h264_in_place(path: str, ffmpeg: str) -> bool:
        """以临时 MP4 转码后原子替换源视频；失败时保留源文件。"""
        root, _ext = os.path.splitext(path)
        temp = root + ".mobile.tmp.mp4"
        try:
            if os.path.exists(temp):
                os.unlink(temp)
            result = subprocess.run(  # nosec B603
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-threads", "1", "-i", path,
                 "-map", "0:v:0?", "-map", "0:a?", "-c:v", "libx264",
                 "-preset", "superfast", "-crf", "23", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", temp],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30 * 60, check=False,
            )
            if result.returncode != 0 or not os.path.exists(temp) or os.path.getsize(temp) == 0:
                log.warning("[download] 移动端转码失败，保留原视频 %s: %s",
                            os.path.basename(path), _short(RuntimeError(result.stderr)))
                return False
            os.replace(temp, path)
            log.info("[download] 📱 已转为 H.264/AAC 以兼容手机浏览器: %s",
                     os.path.basename(path))
            return True
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("[download] 移动端转码异常，保留原视频 %s: %s",
                        os.path.basename(path), _short(e))
            return False
        finally:
            try:
                if os.path.exists(temp):
                    os.unlink(temp)
            except OSError:
                pass

    # ── 通道 1：requests 直下 ────────────────────────────

    def download_direct(self, url: str, dest_path: str, *,
                        referer: str = "", headers: dict | None = None) -> bool:
        """下载单个直链到 dest_path。已存在且非空则直接复用（可安全重跑）。"""
        if not url:
            return False
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return True

        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        hdrs = {"User-Agent": _UA}
        if referer:
            hdrs["Referer"] = referer
        if headers:
            hdrs.update(headers)

        tmp = dest_path + ".part"
        backoff = max(1, int(self._cfg.get("retry_backoff_seconds", 2)))
        cur_url = url
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        # 修正 Twitter 视频封面错误嵌套的 /media/ 路径
        for pfx in ("amplify_video_thumb/", "tweet_video_thumb/", "ext_tw_video_thumb/"):
            if f"pbs.twimg.com/media/{pfx}" in cur_url:
                cur_url = cur_url.replace(f"pbs.twimg.com/media/{pfx}", f"pbs.twimg.com/{pfx}")

        for attempt in range(1, self.retry_times + 1):
            try:
                with requests.get(cur_url, headers=hdrs, timeout=self.timeout,
                                  proxies=proxies, stream=True) as r:
                    if r.status_code == 404 and "pbs.twimg.com" in cur_url:
                        if "name=orig" in cur_url:
                            log.debug("[download] Twitter 图片 name=orig 404，自动降级为 name=large")
                            cur_url = cur_url.replace("name=orig", "name=large")
                        elif "?name=" in cur_url or "&name=" in cur_url:
                            log.debug("[download] Twitter 图片 404，去除 name 参数重试")
                            cur_url = re.sub(r"[?&]name=[^&]+", "", cur_url)
                        else:
                            r.raise_for_status()

                        with requests.get(cur_url, headers=hdrs, timeout=self.timeout,
                                          proxies=proxies, stream=True) as r2:
                            r2.raise_for_status()
                            with open(tmp, "wb") as f:
                                for chunk in r2.iter_content(chunk_size=256 * 1024):
                                    if chunk:
                                        f.write(chunk)
                    else:
                        r.raise_for_status()
                        with open(tmp, "wb") as f:
                            for chunk in r.iter_content(chunk_size=256 * 1024):
                                if chunk:
                                    f.write(chunk)
                if os.path.getsize(tmp) == 0:
                    raise OSError("下载内容为空")
                os.replace(tmp, dest_path)
                log.debug("[download] ✅ 下载成功: %s (%.1f KB)",
                          os.path.basename(dest_path),
                          os.path.getsize(dest_path) / 1024)
                return True
            except Exception as e:
                log.warning("[download] ❌ 下载失败 (%s/%s) %s: %s",
                            attempt, self.retry_times,
                            os.path.basename(dest_path), e)
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except OSError:
                    pass
                if attempt < self.retry_times:
                    time.sleep(backoff * attempt)
        return False

    def download_many(self, tasks: list[tuple[str, str]], *,
                      referer: str = "") -> list[str]:
        """并发下载多个直链。

        :param tasks: [(url, dest_path), ...]
        :return: 下载成功的本地路径列表（保持输入顺序）
        """
        if not tasks:
            return []
        results: list[str | None] = [None] * len(tasks)
        extra_headers: dict[str, str] = {}
        if referer.startswith("https://www.instagram.com/"):
            # The caller already identified this as an Instagram media batch;
            # warm one anonymous site session before the worker fan-out.
            extra_headers = self._instagram_public_headers()

        def _one(idx: int, url: str, dest: str):
            if self.download_direct(url, dest, referer=referer, headers=extra_headers):
                results[idx] = dest

        workers = min(self.threads, len(tasks))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (url, dest) in enumerate(tasks):
                pool.submit(_one, i, url, dest)
        return [p for p in results if p]

    # ── 通道 2：yt-dlp ──────────────────────────────────

    def _ytdlp_module(self):
        """惰性 import yt-dlp；未安装时抛 YtdlpUnavailable（只警告一次）。"""
        try:
            import yt_dlp  # noqa: PLC0415 — 惰性导入是刻意设计
            return yt_dlp
        except ImportError as e:
            if not self._ytdlp_warned:
                self._ytdlp_warned = True
                log.error("[download] 未安装 yt-dlp，社交平台媒体下载不可用："
                          "请执行 pip install -U yt-dlp（%s）", e)
            raise YtdlpUnavailable(str(e)) from e

    def base_ydl_opts(self, platform_cfg: dict | None = None) -> dict:
        """构建 yt-dlp 通用选项：最高画质 + 原始音轨 + cookies + 代理 + 重试。"""
        cfg = self._cfg
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "consoletitle": False,
            "logger": _YdlLogger(),   # yt-dlp 输出统一降级到 debug 日志
            "ignoreerrors": False,
            "noplaylist": False,
            # 最高画质视频 + 最佳音轨，回退到单一最佳流
            "format": "bv*+ba/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "socket_timeout": int(cfg.get("ytdlp_socket_timeout", 30)),
            "retries": self.retry_times,
            "fragment_retries": self.retry_times,
            "extractor_retries": self.retry_times,
            "http_headers": {
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
            },
            "writethumbnail": False,
            "writeinfojson": False,
            "overwrites": False,
            "continuedl": True,
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        ff = self.ffmpeg_path()
        if ff:
            opts["ffmpeg_location"] = ff

        # cookies —— 永远通过 yt-dlp 的 cookiefile/cookiesfrombrowser 传递。
        # 过去把 SQLite 中的值拼进 ``Cookie`` 请求头，会触发 yt-dlp 的安全
        # 警告，而且在新版中可能改变 cookie 的作用域。临时 Netscape 文件
        # 只在一次调用期间存在，调用方会在 finally 中清理。
        if platform_cfg:
            cf = os.path.expanduser(str(platform_cfg.get("cookies_file") or "").strip())
            platform = str(platform_cfg.get("_platform") or platform_cfg.get("platform") or "").lower()
            if cf and os.path.exists(cf):
                opts["cookiefile"] = cf
            elif platform == "instagram":
                from src.social import ig_session
                cookies = ig_session.resolve_cookies(cf)
                if cookies:
                    temp_cookiefile = ig_session.create_temp_cookie_file(cookies)
                    if temp_cookiefile:
                        opts["cookiefile"] = temp_cookiefile
                        # Private bookkeeping key; stripped before YoutubeDL sees it.
                        opts["_collink_temp_cookiefile"] = temp_cookiefile
            browser = (platform_cfg.get("cookies_from_browser") or "").strip()
            if browser and not opts.get("cookiefile"):
                opts["cookiesfrombrowser"] = (browser,)
        return opts

    @staticmethod
    def _take_temp_cookiefile(opts: dict) -> str:
        """Remove internal cookie bookkeeping before passing options to yt-dlp."""
        return str(opts.pop("_collink_temp_cookiefile", "") or "")

    def extract_info(self, url: str, *, platform_cfg: dict | None = None,
                     extra_opts: dict | None = None) -> dict | None:
        """解析 URL 元信息（不下载）。失败返回 None，异常不外抛。"""
        try:
            yt_dlp = self._ytdlp_module()
        except YtdlpUnavailable:
            return None

        opts = self.base_ydl_opts(platform_cfg)
        opts["skip_download"] = True
        if extra_opts:
            opts.update(extra_opts)
        temp_cookiefile = self._take_temp_cookiefile(opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            log.debug("[download] extract_info 失败 %s: %s", url, _short(e))
            return None
        finally:
            if temp_cookiefile:
                from src.social import ig_session
                ig_session.remove_temp_cookie_file(temp_cookiefile)

    def extract_info_strict(self, url: str, *, platform_cfg: dict | None = None,
                            extra_opts: dict | None = None) -> dict:
        """同 extract_info，但把异常向上抛（直播检测需要区分「未开播」与「网络错误」）。"""
        yt_dlp = self._ytdlp_module()
        opts = self.base_ydl_opts(platform_cfg)
        opts["skip_download"] = True
        if extra_opts:
            opts.update(extra_opts)
        temp_cookiefile = self._take_temp_cookiefile(opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        finally:
            if temp_cookiefile:
                from src.social import ig_session
                ig_session.remove_temp_cookie_file(temp_cookiefile)

    def download_via_ytdlp(self, url: str, dest_dir: str, *,
                           platform_cfg: dict | None = None,
                           extra_opts: dict | None = None,
                           outtmpl: str | None = None) -> list[str]:
        """用 yt-dlp 把 url 的全部媒体下载到 dest_dir。

        返回 dest_dir 下新出现的媒体文件路径（已排序）。
        「扫描目录」而不是解析 yt-dlp 返回值，是为了同时兼容
        单视频 / 多图轮播 / Reel / 图文 Post 等各种形态。
        """
        try:
            yt_dlp = self._ytdlp_module()
        except YtdlpUnavailable:
            return []

        os.makedirs(dest_dir, exist_ok=True)
        before = set(_list_media(dest_dir))

        opts = self.base_ydl_opts(platform_cfg)
        opts["paths"] = {"home": dest_dir}
        tmpl = outtmpl or "%(id)s_%(playlist_index|0)s.%(ext)s"
        if os.path.isabs(tmpl) or dest_dir in tmpl:
            tmpl = os.path.basename(tmpl)
        opts["outtmpl"] = tmpl
        if extra_opts:
            opts.update(extra_opts)
        temp_cookiefile = self._take_temp_cookiefile(opts)

        backoff = max(1, int(self._cfg.get("retry_backoff_seconds", 2)))
        try:
            for attempt in range(1, self.retry_times + 1):
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([url])
                    break
                except Exception as e:
                    log.warning("[download] yt-dlp 下载失败 (%s/%s) %s: %s",
                                attempt, self.retry_times, url, _short(e))
                    if attempt < self.retry_times:
                        time.sleep(backoff * attempt)
        finally:
            if temp_cookiefile:
                from src.social import ig_session
                ig_session.remove_temp_cookie_file(temp_cookiefile)

        after = set(_list_media(dest_dir))
        new_files = sorted(after - before)
        if not new_files:
            # 可能是之前已下载过 —— 复用目录里已有文件
            new_files = sorted(after)
        if new_files:
            log.info("[download] ✅ yt-dlp 取得 %s 个文件 → %s",
                     len(new_files), dest_dir)
        return new_files


def _list_media(d: str) -> list[str]:
    """列出目录内的媒体文件（跳过 .part 等中间产物，支持递归查找）。"""
    out = []
    try:
        for root, _, files in os.walk(d):
            for name in files:
                fp = os.path.join(root, name)
                if _is_media_file(fp) and os.path.getsize(fp) > 0:
                    out.append(os.path.abspath(fp))
    except OSError:
        pass
    return sorted(out)


def _short(e: Exception, limit: int = 200) -> str:
    """截断异常信息，避免 yt-dlp 的长堆栈污染日志。"""
    s = str(e).replace("\n", " ")
    return s[:limit] + ("…" if len(s) > limit else "")
