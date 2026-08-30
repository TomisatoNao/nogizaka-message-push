"""
social/scheduler.py — 社交平台异步轮询调度器

每个社交平台一个独立守护线程，各自按自己的 interval 轮询：

  * 平台之间完全隔离 —— 任一平台抛异常只会让它自己退避，
    不影响其它平台，也绝不会让主程序退出
  * 支持热更新 —— 每轮循环都重新读取 config（enabled / interval 即时生效）；
    平台被关闭时线程转为空转（idle_sleep_seconds），重新打开立即恢复
  * 转发串行化 —— 所有线程共用一把锁调用 forward 回调，
    因为 SyncState / QQBot / Translator 都不是线程安全的
  * 指数退避 —— 连续失败按 error_backoff_seconds 递增，上限 error_backoff_max

既有 melink / showroom / youtube 仍在 SyncManager.watch() 主循环里跑，
本调度器不接管它们，因此既有行为零变化。
"""

import logging
import random
import threading

from src.social.fetchers.base import BaseFetcher
from src.social.models import Post
from src.social.settings import social_settings

log = logging.getLogger("collink")


class SocialScheduler:
    """社交平台线程池调度器（每平台一线程）。"""

    def __init__(self, config: dict, fetchers: list[BaseFetcher],
                 forward_cb, forward_lock: threading.Lock,
                 alert_cb=None):
        """
        :param config: 共享 config dict（热更新时原地替换内容）
        :param fetchers: 社交 fetcher 列表（无论 enabled 与否都传进来，
                         线程内每轮自行判断，从而支持热开关）
        :param forward_cb: forward_cb(platform, posts) -> list[Post]（成功列表）
        :param forward_lock: 与主循环共用的转发锁
        :param alert_cb: alert_cb(platform, failures) 连续失败告警
        """
        self._config = config
        self._fetchers = list(fetchers)
        self._forward = forward_cb
        self._lock = forward_lock
        self._alert = alert_cb
        self._stop_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._failures: dict[str, int] = {}

    # ── 生命周期 ──────────────────────────────────────────

    @property
    def _cfg(self) -> dict:
        return social_settings(self._config)

    def start(self) -> None:
        if self._threads:
            return
        for f in self._fetchers:
            t = threading.Thread(target=self._worker, args=(f,),
                                 name=f"social-{f.platform_name}", daemon=True)
            t.start()
            self._threads.append(t)
        if self._threads:
            log.info("[social] 🧵 已启动 %s 个平台监控线程: %s",
                     len(self._threads),
                     " · ".join(f.platform_name for f in self._fetchers))

    def stop(self) -> None:
        self._stop_evt.set()
        for f in self._fetchers:
            if hasattr(f, "stop_all"):
                try:
                    f.stop_all()
                except Exception:  # nosec B110
                    pass

    def join(self, timeout: float = 5) -> None:
        for t in self._threads:
            t.join(timeout=timeout)

    # ── 工作线程 ──────────────────────────────────────────

    def _worker(self, fetcher: BaseFetcher) -> None:
        pname = fetcher.platform_name
        # 各平台启动时间错开，避免同时打多个站点
        if self._stop_evt.wait(random.uniform(0, 3)):  # nosec B311
            return

        while not self._stop_evt.is_set():
            cfg = self._cfg
            try:
                if not fetcher.is_enabled:
                    # 平台未启用 → 空转等待（支持配置热开启）
                    if self._stop_evt.wait(int(cfg.get("idle_sleep_seconds", 30))):
                        return
                    continue

                self._poll_once(fetcher)
                self._failures[pname] = 0
                sleep_for = fetcher.get_interval()
            except Exception as e:
                n = self._failures.get(pname, 0) + 1
                self._failures[pname] = n
                log.warning("[%s] 异常（连续失败 %s 次）: %s", pname, n,
                            str(e).replace("\n", " ")[:200])
                if self._alert:
                    try:
                        self._alert(pname, n)
                    except Exception:  # nosec B110
                        pass
                base = int(cfg.get("error_backoff_seconds", 60))
                cap = int(cfg.get("error_backoff_max", 900))
                sleep_for = min(cap, base * (2 ** min(n - 1, 5)))
                log.info("[%s] ⏳ 异常恢复：%ss 后重试", pname, sleep_for)

            if self._stop_evt.wait(max(5, int(sleep_for))):
                return

    def _poll_once(self, fetcher: BaseFetcher) -> None:
        """一次轮询：抓取 → 转发（加锁）→ 回写游标。"""
        pname = fetcher.platform_name
        posts: list[Post] = fetcher.fetch()

        succeeded: list[Post] = []
        if posts:
            succeeded = self._forward(pname, posts) or []
            try:
                fetcher.mark_synced(succeeded)
            except Exception as e:
                log.warning("[%s] 回写同步状态失败: %s", pname, e)
        else:
            log.debug("[%s] 本轮无新内容", pname)
