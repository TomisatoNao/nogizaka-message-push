import logging
import threading

from src.logger import log_all
from src.social.downloader import MediaDownloader
from src.social.fetchers.instagram_fetcher import InstagramFetcher
from src.social.fetchers.tiktok_fetcher import TikTokFetcher
from src.social.fetchers.tiktok_live_fetcher import TikTokLiveFetcher
from src.social.fetchers.x_fetcher import XFetcher
from src.social.forwarder import SocialForwarder
from src.social.scheduler import SocialScheduler
from src.social.store import SocialStore

_store: SocialStore | None = None
_downloader: MediaDownloader | None = None
_forwarder: SocialForwarder | None = None
_scheduler: SocialScheduler | None = None
_shared_config: dict | None = None
_lock = threading.Lock()
_forward_lock = threading.Lock()


class _SocialLogBridge(logging.Handler):
    """将社媒监控模块的 logging 日志桥接至系统的统一 log_all 流。"""
    def emit(self, record):
        try:
            msg = self.format(record)
            if record.levelno >= logging.ERROR:
                log_all(f"{msg}", is_error=True)
            elif record.levelno >= logging.WARNING:
                log_all(f"⚠️ {msg}")
            elif record.levelno >= logging.INFO:
                log_all(f"{msg}")
            else:
                log_all(f"{msg}", is_debug=True)
        except Exception:
            pass


def _setup_social_logger() -> None:
    for name in ("collink", "social.forwarder"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(_SocialLogBridge())
        logger.setLevel(logging.INFO)
        logger.propagate = False


def start_social_service(config: dict) -> SocialScheduler | None:
    """初始化并启动社交媒体监控守护调度器。"""
    global _store, _downloader, _forwarder, _scheduler, _shared_config

    with _lock:
        if _scheduler is not None:
            return _scheduler

        _setup_social_logger()
        _shared_config = config
        _store = SocialStore()
        _downloader = MediaDownloader(config)
        _forwarder = SocialForwarder(config, _downloader)

        # 实例化全部 Fetcher
        fetchers = [
            XFetcher(config, _store, _downloader),
            InstagramFetcher(config, _store, _downloader),
            TikTokFetcher(config, _store, _downloader),
        ]
        live_fetcher = TikTokLiveFetcher(config, _store, _downloader)
        live_fetcher.set_recording_callback(_forwarder.send_recording)
        fetchers.append(live_fetcher)

        def _forward_callback(platform: str, posts: list):
            succeeded = []
            for p in posts:
                try:
                    _forwarder.forward_post(p)
                    succeeded.append(p)
                except Exception as ex:
                    log_all(f"⚠️ [{platform}] 动态 {p.post_id} 推送失败: {ex}", is_error=True)
            return succeeded

        _scheduler = SocialScheduler(
            config=config,
            fetchers=fetchers,
            forward_cb=_forward_callback,
            forward_lock=_forward_lock,
        )

        _scheduler.start()
        log_all("🌐 社交媒体监控服务已启动（支持 X / Instagram / TikTok / TikTok Live）")
        return _scheduler


def reload_social_service(config: dict | None = None) -> None:
    """热重载社媒监控配置，使所有 Fetcher 的账号列表与配置即时生效。"""
    global _shared_config
    with _lock:
        if config is None:
            import json5
            from pathlib import Path
            config_path = Path(__file__).resolve().parent.parent.parent / "config" / "config.json"
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json5.load(f)
            except Exception as e:
                log_all(f"⚠️ 读取 config.json 热重载社媒失败: {e}", is_error=True)
                return

        if _shared_config is not None and isinstance(config, dict):
            _shared_config.clear()
            _shared_config.update(config)
            log_all("⚙️ 社交媒体监控配置已热重载（X / Instagram / TikTok 账号列表即时生效）", is_debug=True)


def stop_social_service():
    """优雅停止社媒监控服务。"""
    global _scheduler, _shared_config
    with _lock:
        if _scheduler:
            _scheduler.stop()
            _scheduler.join(timeout=5)
            _scheduler = None
            _shared_config = None
            log_all("🛑 社交媒体监控服务已停止")
