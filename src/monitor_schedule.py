"""统一内容监控调度时间策略。

Message、官方博客和社交媒体监控都使用同一套 JST 时间窗口：

* ``day_start_hour`` 到 ``night_start_hour``：日间；
* ``night_start_hour`` 到 ``day_start_hour``：深夜（跨午夜）；
* ``sleep_hours``：休眠，优先级高于深夜。

该模块只负责“现在属于哪个阶段”和“何时结束休眠”，不负责具体平台的
轮询、投递或健康检查。这样内容监控可以共享策略，同时保持各自的频率和
错误隔离。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

from src.utils import in_hour_range

JST = timezone(timedelta(hours=9), name="JST")
MonitorPhase = Literal["day", "night", "sleep"]


def _hour_label(hour: int, *, end: bool = False) -> str:
    """格式化时段边界；结束边界为 0 时显示为 24:00，避免区间看起来倒置。"""
    if end and hour == 0:
        return "24:00"
    return f"{hour:02d}:00"


def _as_hour(value: Any, default: int) -> int:
    """读取 0–23 的小时值；配置异常时回退默认值。"""
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return default
    return hour if 0 <= hour <= 23 else default


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no", "none"}
    return bool(value)


def _as_sleep_hours(value: Any, start: int, end: int) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return _as_hour(value[0], start), _as_hour(value[1], end)
    return start, end


def _mapping_value(source: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """读取新配置块中的键，同时兼容 ``sleep_hours`` 的数组形式。"""
    if key in source:
        return source[key]
    if key == "sleep_start_hour" and "sleep_hours" in source:
        value = source.get("sleep_hours")
        return value[0] if isinstance(value, (list, tuple)) and value else default
    if key == "sleep_end_hour" and "sleep_hours" in source:
        value = source.get("sleep_hours")
        return value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else default
    return default


@dataclass(frozen=True, slots=True)
class MonitorSchedule:
    """内容监控共用的时间窗口。

    ``night_start_hour`` 可以大于 ``day_start_hour``，表示深夜跨越午夜。
    ``sleep_start_hour`` / ``sleep_end_hour`` 同样支持跨午夜。休眠窗口相同
    起止时视为关闭，避免误把整天判定为休眠。
    """

    timezone_name: str = "Asia/Tokyo"
    day_start_hour: int = 7
    night_start_hour: int = 0
    sleep_start_hour: int = 2
    sleep_end_hour: int = 7
    pause_content_monitors: bool = True

    @classmethod
    def from_config(cls, raw_config: Any = None) -> "MonitorSchedule":
        """从 JSON 配置字典或 ``config.config`` facade 构造调度对象。

        新格式优先级高于旧格式：``monitor_schedule`` > 顶层旧键 > 默认值。
        旧版配置可以不做迁移直接使用。
        """
        defaults = cls()
        if isinstance(raw_config, Mapping):
            nested_raw = raw_config.get("monitor_schedule")
            nested = nested_raw if isinstance(nested_raw, Mapping) else {}
            value = lambda key, default: _mapping_value(  # noqa: E731
                nested,
                key,
                _mapping_value(raw_config, key, default),
            )
            sleep_start = value("sleep_start_hour", defaults.sleep_start_hour)
            sleep_end = value("sleep_end_hour", defaults.sleep_end_hour)
            # Nested values always outrank legacy top-level values.  In
            # particular, an explicit nested start/end pair must not be
            # overwritten by a stale top-level ``sleep_hours`` alias.
            if "sleep_hours" in nested:
                sleep_pair = nested["sleep_hours"]
            elif "sleep_start_hour" in nested or "sleep_end_hour" in nested:
                sleep_pair = None
            else:
                sleep_pair = raw_config.get("sleep_hours")
            sleep_start, sleep_end = _as_sleep_hours(
                sleep_pair, _as_hour(sleep_start, defaults.sleep_start_hour),
                _as_hour(sleep_end, defaults.sleep_end_hour),
            )
            day_start = _as_hour(value("day_start_hour", defaults.day_start_hour), defaults.day_start_hour)
            night_start = _as_hour(value("night_start_hour", defaults.night_start_hour), defaults.night_start_hour)
            if day_start == night_start:
                day_start, night_start = defaults.day_start_hour, defaults.night_start_hour
            return cls(
                timezone_name=str(value("timezone", defaults.timezone_name) or defaults.timezone_name),
                day_start_hour=day_start,
                night_start_hour=night_start,
                sleep_start_hour=sleep_start,
                sleep_end_hour=sleep_end,
                pause_content_monitors=_as_bool(
                    value("pause_content_monitors", defaults.pause_content_monitors),
                    defaults.pause_content_monitors,
                ),
            )

        nested_raw = getattr(raw_config, "MONITOR_SCHEDULE", None)
        nested = nested_raw if isinstance(nested_raw, Mapping) else {}

        def attr_value(key: str, default: Any) -> Any:
            if key in nested:
                return nested[key]
            env_name = {
                "timezone": "MONITOR_TIMEZONE",
                "day_start_hour": "DAY_START_HOUR",
                "night_start_hour": "NIGHT_START_HOUR",
                "sleep_start_hour": "SLEEP_START_HOUR",
                "sleep_end_hour": "SLEEP_END_HOUR",
                "pause_content_monitors": "PAUSE_CONTENT_MONITORS",
            }.get(key, "")
            return getattr(raw_config, env_name, default) if env_name else default

        sleep_start = _as_hour(
            attr_value("sleep_start_hour", defaults.sleep_start_hour),
            defaults.sleep_start_hour,
        )
        sleep_end = _as_hour(
            attr_value("sleep_end_hour", defaults.sleep_end_hour),
            defaults.sleep_end_hour,
        )
        sleep_pair = nested.get("sleep_hours")
        sleep_start, sleep_end = _as_sleep_hours(sleep_pair, sleep_start, sleep_end)
        day_start = _as_hour(attr_value("day_start_hour", defaults.day_start_hour), defaults.day_start_hour)
        night_start = _as_hour(attr_value("night_start_hour", defaults.night_start_hour), defaults.night_start_hour)
        if day_start == night_start:
            day_start, night_start = defaults.day_start_hour, defaults.night_start_hour
        return cls(
            timezone_name=str(attr_value("timezone", defaults.timezone_name) or defaults.timezone_name),
            day_start_hour=day_start,
            night_start_hour=night_start,
            sleep_start_hour=sleep_start,
            sleep_end_hour=sleep_end,
            pause_content_monitors=_as_bool(
                attr_value("pause_content_monitors", defaults.pause_content_monitors),
                defaults.pause_content_monitors,
            ),
        )

    @staticmethod
    def _as_jst(now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(JST)
        if now.tzinfo is None:
            return now.replace(tzinfo=JST)
        return now.astimezone(JST)

    @property
    def sleep_enabled(self) -> bool:
        return self.pause_content_monitors and self.sleep_start_hour != self.sleep_end_hour

    def is_sleeping(self, now: datetime | None = None) -> bool:
        """是否处于全局内容监控休眠时段。"""
        if not self.sleep_enabled:
            return False
        current = self._as_jst(now)
        return in_hour_range(current.hour, self.sleep_start_hour, self.sleep_end_hour)

    def phase(self, now: datetime | None = None) -> MonitorPhase:
        """返回 ``day``、``night`` 或 ``sleep``；休眠优先级最高。"""
        current = self._as_jst(now)
        if self.is_sleeping(current):
            return "sleep"
        if in_hour_range(current.hour, self.night_start_hour, self.day_start_hour):
            return "night"
        return "day"

    def interval_phase(self, now: datetime | None = None) -> Literal["day", "night"]:
        """返回轮询间隔应使用的昼夜档位。

        休眠本身不属于任何一个间隔档位；调用方应先用 :meth:`is_sleeping`
        暂停内容轮询。为避免各调度器自行猜测，这里在休眠时返回 ``night``
        作为安全的低频兜底。
        """
        return "night" if self.phase(now) != "day" else "day"

    def seconds_until_wake(self, now: datetime | None = None) -> int:
        """当前处于休眠时返回到休眠结束的秒数，否则返回 0。"""
        current = self._as_jst(now)
        if not self.is_sleeping(current):
            return 0
        wake = current.replace(
            hour=self.sleep_end_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if wake <= current:
            wake += timedelta(days=1)
        return max(1, int((wake - current).total_seconds()))

    def next_transition(self, now: datetime | None = None) -> datetime:
        """返回下一个小时边界，用于状态页和调度日志。"""
        current = self._as_jst(now)
        candidates: list[datetime] = []
        for hour in {
            self.day_start_hour,
            self.night_start_hour,
            self.sleep_start_hour,
            self.sleep_end_hour,
        }:
            candidate = current.replace(hour=hour, minute=0, second=0, microsecond=0)
            if candidate <= current:
                candidate += timedelta(days=1)
            candidates.append(candidate)
        return min(candidates)

    def phase_label(self, now: datetime | None = None) -> str:
        return {
            "day": "☀️ 日间",
            "night": "🌙 深夜低速",
            "sleep": "😴 全局休眠",
        }[self.phase(now)]

    def window_summary(self) -> str:
        """给管理端/启动日志使用的紧凑说明。"""
        return (
            f"JST 日间 {_hour_label(self.day_start_hour)}–"
            f"{_hour_label(self.night_start_hour, end=True)} · "
            f"深夜 {_hour_label(self.night_start_hour)}–"
            f"{_hour_label(self.sleep_start_hour, end=True)} · "
            f"休眠 {_hour_label(self.sleep_start_hour)}–"
            f"{_hour_label(self.sleep_end_hour, end=True)}"
        )


__all__ = ["JST", "MonitorPhase", "MonitorSchedule"]
