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
from src.social.service import SocialService
from src.social.store import SocialStore

_store: SocialStore | None = None
_downloader: MediaDownloader | None = None
_forwarder: SocialForwarder | None = None
_service: SocialService | None = None
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
                log_all(f"⚠️ {msg}", is_warning=True)
            elif record.levelno >= logging.INFO:
                log_all(f"{msg}")
            else:
                log_all(f"{msg}", is_debug=True)
        except Exception:  # nosec B110
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
    global _store, _downloader, _forwarder, _service, _scheduler, _shared_config

    with _lock:
        if _scheduler is not None:
            return _scheduler

        _setup_social_logger()
        _shared_config = config
        _store = SocialStore()
        _downloader = MediaDownloader(config)
        _forwarder = SocialForwarder(config, _downloader, _store)
        _service = SocialService(
            config,
            downloader=_downloader,
            forwarder=_forwarder,
            store=_store,
        )

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
            outcome_counts: dict[str, int] = {}
            for p in posts:
                try:
                    operation = _service.process_post(
                        p,
                        translate=True,
                        # Fetcher 通常已经完成媒体下载；下载器会跳过已有文件，
                        # 这里只补齐异常情况下仍缺失的媒体，
                        # 避免监控与手动入口各自维护一套翻译/投递编排。
                        download=True,
                        archive=True,
                    )
                    completed = operation.completed
                    result = operation.delivery
                    outcome = result.outcome if result is not None else (
                        "success" if completed else "error"
                    )
                    outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
                    if completed:
                        succeeded.append(p)
                    elif result is None:
                        log_all(
                            f"⚠️ [{platform}] 动态 {p.post_id} 未投递成功，将在下轮重试",
                            is_error=True,
                        )
                except Exception as ex:
                    outcome_counts["error"] = outcome_counts.get("error", 0) + 1
                    log_all(f"⚠️ [{platform}] 动态 {p.post_id} 推送失败: {type(ex).__name__}", is_error=True)

            if posts:
                labels = (
                    ("success", "完整成功"),
                    ("partial", "部分成功"),
                    ("failed", "全部失败"),
                    ("no_route", "无匹配路由"),
                    ("already_delivered", "已投递"),
                    ("error", "异常"),
                )
                summary = " · ".join(
                    f"{label} {outcome_counts[key]}"
                    for key, label in labels
                    if outcome_counts.get(key, 0)
                )
                log_all(
                    f"📊 [{platform}] 推送汇总 | 待处理 {len(posts)} | "
                    f"完成 {len(succeeded)} | {summary or '无结果'}",
                )
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
    global _scheduler, _shared_config, _service
    with _lock:
        if _scheduler:
            _scheduler.stop()
            _scheduler.join(timeout=5)
            _scheduler = None
            _service = None
            _shared_config = None
            log_all("🛑 社交媒体监控服务已停止")
