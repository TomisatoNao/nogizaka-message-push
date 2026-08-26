"""Abstract base class for all platform fetchers."""
import random
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta

from src.social.models import Post

_CST = timezone(timedelta(hours=8))
_NIGHT_START = 0   # 0:00 CST
_NIGHT_END = 6     # 6:00 CST


def _activity_multiplier(hour: int) -> float:
    """时间-活跃度映射：模拟真人使用手机的 Pareto 分布模式

    高峰时段（通勤/午休/晚间）→ 短间隔（频繁轮询）
    普通时段（工作时间内）→ 正常间隔
    低峰时段（深夜/凌晨）→ 长间隔（很少轮询）
    """
    if hour in (7, 8, 12, 13, 19, 20, 21):
        return 0.5   # 早高峰 / 午休 / 晚间黄金时段 — 高频
    elif hour in (9, 10, 11, 14, 15, 16, 17, 18, 22):
        return 1.0   # 白天工作时段 / 晚间 — 正常
    else:
        return 2.5   # 0-6（静默时段外）或 23 — 低频


class BaseFetcher(ABC):
    platform_name: str = ""

    def __init__(self, config: dict):
        self._config = config

    @property
    def is_enabled(self) -> bool:
        cfg = self._config.get("platforms", {}).get(self.platform_name, {})
        return cfg.get("enabled", False)

    @property
    def interval_seconds(self) -> int:
        cfg = self._config.get("platforms", {}).get(self.platform_name, {})
        return cfg.get("interval_seconds", 300)

    @property
    def night_interval_seconds(self) -> int:
        cfg = self._config.get("platforms", {}).get(self.platform_name, {})
        return cfg.get("night_interval_seconds", self.interval_seconds)

    def get_interval(self) -> int:
        cfg = self._config.get("platforms", {}).get(self.platform_name, {})
        hour = datetime.now(_CST).hour
        if _NIGHT_START <= hour < _NIGHT_END:
            base = self.night_interval_seconds
        else:
            base = self.interval_seconds
        # Pareto 活跃度加权（替代均匀分布）
        activity = _activity_multiplier(hour)
        jitter_min = cfg.get("interval_jitter_min", 0.8)
        jitter_max = cfg.get("interval_jitter_max", 1.2)
        return int(base * activity * random.uniform(jitter_min, jitter_max))

    @abstractmethod
    def fetch(self) -> list[Post]:
        """Fetch new posts. Exceptions propagate to the caller for error tracking."""
        ...

    def mark_fetched(self):
        """[已废弃] 请使用 mark_synced(synced_posts)。保留以兼容旧 fetcher。"""

    def mark_synced(self, synced_posts: list[Post]) -> None:
        """已成功转发后回调。子类覆写以持久化游标等状态。

        默认实现回退到 mark_fetched()，保证旧 fetcher（如 YouTube）兼容。
        :param synced_posts: 已确认成功转发到 QQ 的 Post 列表
        """
        self.mark_fetched()

    def _safe_fetch(self) -> list[Post]:
        """Wrapper that isolates errors per-fetcher."""
        try:
            return self.fetch()
        except Exception as e:
            from src.logger import log_all
            log_all(f"⚠️ [{self.platform_name}] 抓取异常: {e}", is_error=True)
            return []
