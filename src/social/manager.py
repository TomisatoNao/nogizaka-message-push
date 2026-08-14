"""
social/manager.py — 社交媒体监控服务管理器
负责协调 Fetchers、SocialScheduler、SocialForwarder 的生命周期。
"""

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
_lock = threading.Lock()
_forward_lock = threading.Lock()


def start_social_service(config: dict) -> SocialScheduler | None:
    """初始化并启动社交媒体监控守护调度器。"""
    global _store, _downloader, _forwarder, _scheduler

    with _lock:
        if _scheduler is not None:
            return _scheduler

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


def stop_social_service():
    """优雅停止社媒监控服务。"""
    global _scheduler
    with _lock:
        if _scheduler:
            _scheduler.stop()
            _scheduler.join(timeout=5)
            _scheduler = None
            log_all("🛑 社交媒体监控服务已停止")
