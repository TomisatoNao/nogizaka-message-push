"""监控项目频率来源与全局休眠门闩契约。"""

import asyncio
from datetime import datetime

from src.app_modules import blog_worker, message_worker
from src.monitor_schedule import JST, MonitorSchedule
from src.social.scheduler import SocialScheduler
from src.social.settings import platform_settings


def test_each_monitor_keeps_its_own_frequency_source():
    config = {
        "day_interval": [120, 180],
        "night_interval": [300, 360],
        "blog_monitor": {
            "day_interval": [60, 90],
            "night_interval": [600, 900],
        },
        "platforms": {
            "x": {"interval_seconds": 61, "night_interval_seconds": 301},
            "instagram": {
                "interval_range_seconds": [1800, 2400],
                "night_interval_range_seconds": [5400, 7200],
            },
            "tiktok": {"interval_seconds": 121},
            "tiktok_live": {"interval_seconds": 8},
        },
    }

    assert config["day_interval"] == [120, 180]
    assert config["blog_monitor"]["day_interval"] == [60, 90]
    assert platform_settings(config, "x")["interval_seconds"] == 61
    assert platform_settings(config, "instagram")["interval_range_seconds"] == [1800, 2400]
    assert platform_settings(config, "tiktok")["interval_seconds"] == 121
    assert platform_settings(config, "tiktok_live")["interval_seconds"] == 8


def test_instagram_legacy_single_values_are_not_hidden_by_default_ranges():
    config = {
        "platforms": {
            "instagram": {
                "interval_seconds": 777,
                "night_interval_seconds": 1777,
            }
        }
    }
    settings = platform_settings(config, "instagram")
    assert settings["interval_seconds"] == 777
    assert settings["night_interval_seconds"] == 1777
    assert "interval_range_seconds" not in settings
    assert "night_interval_range_seconds" not in settings


def test_blog_worker_uses_blog_intervals_independently(monkeypatch):
    monkeypatch.setattr(
        blog_worker,
        "_get_jst_now",
        lambda: datetime(2026, 9, 10, 12, tzinfo=JST),
    )
    worker = blog_worker.BlogWorker(
        None,
        None,
        lambda: {},
        __import__("asyncio").Event(),
    )
    value, label = worker._interval(
        {
            "monitor_schedule": {
                "day_start_hour": 7,
                "night_start_hour": 23,
                "sleep_hours": [2, 7],
            },
            "blog_monitor": {
                "day_interval": [60, 60],
                "night_interval": [900, 900],
            },
        }
    )
    assert value == 60
    assert "日间" in label


def test_schedule_hot_reload_changes_sleep_boundary_without_restart():
    raw = {
        "monitor_schedule": {
            "day_start_hour": 7,
            "night_start_hour": 23,
            "sleep_hours": [2, 7],
        }
    }
    now = datetime(2026, 9, 10, 3, tzinfo=JST)
    assert MonitorSchedule.from_config(raw).is_sleeping(now)

    raw["monitor_schedule"]["sleep_hours"] = [4, 8]
    updated = MonitorSchedule.from_config(raw)
    assert not updated.is_sleeping(now)
    assert updated.phase(now) == "night"


def test_blog_worker_wakes_when_hot_reload_ends_sleep_window(monkeypatch):
    """博客等待休眠时也要读取新边界，不能被旧窗口卡住。"""
    now = datetime(2026, 9, 10, 3, tzinfo=JST)
    monkeypatch.setattr(blog_worker, "_get_jst_now", lambda: now)
    raw = {
        "monitor_schedule": {
            "day_start_hour": 7,
            "night_start_hour": 23,
            "sleep_hours": [2, 7],
        }
    }
    worker = blog_worker.BlogWorker(
        None,
        None,
        lambda: raw,
        asyncio.Event(),
    )
    waits = []

    async def fake_wait(seconds):
        waits.append(seconds)
        # 模拟管理端热重载：当前 03:00 已不再处于新的休眠窗口。
        raw["monitor_schedule"]["sleep_hours"] = [1, 3]
        return False

    worker._wait = fake_wait
    result = asyncio.run(
        worker._wait_for_schedule(MonitorSchedule.from_config(raw))
    )

    assert result is False
    assert waits == [30]


def test_manual_message_trigger_is_consumed_during_wait():
    async def scenario():
        event = asyncio.Event()
        event.set()
        triggered = await message_worker._wait_or_trigger(event, 60)
        return triggered, event.is_set()

    triggered, still_set = asyncio.run(scenario())
    assert triggered is True
    assert still_set is False


def test_social_scheduler_exposes_global_sleep_gate(monkeypatch):
    class SleepingSchedule:
        sleep_start_hour = 2
        sleep_end_hour = 7

        def is_sleeping(self, now=None):
            return True

    monkeypatch.setattr(
        "src.social.scheduler.MonitorSchedule.from_config",
        lambda _config: SleepingSchedule(),
    )
    scheduler = SocialScheduler({}, [], lambda *_: [], __import__("threading").Lock())
    assert scheduler._is_content_sleeping() is True


def test_blog_worker_sleep_gate_skips_content_cycle(monkeypatch):
    calls = []

    async def fake_cycle(*_args):
        calls.append(True)
        return []

    class SleepingSchedule:
        sleep_start_hour = 2
        sleep_end_hour = 7

        def is_sleeping(self, now=None):
            return True

    async def scenario():
        stop = asyncio.Event()
        worker = blog_worker.BlogWorker(
            None,
            None,
            lambda: {
                "blog_monitor": {"enabled": True},
                "blog_records": {},
            },
            stop,
            cycle_runner=fake_cycle,
        )
        worker._wait_for_schedule = lambda _schedule: asyncio.sleep(0, result=True)
        await worker.run()

    monkeypatch.setattr(blog_worker.MonitorSchedule, "from_config", lambda _config: SleepingSchedule())
    asyncio.run(scenario())
    assert calls == []
