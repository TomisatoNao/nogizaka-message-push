"""统一内容监控时间策略契约测试。"""

from datetime import datetime, timezone, timedelta

from src.monitor_schedule import JST, MonitorSchedule


def _jst(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 10, hour, minute, tzinfo=JST)


def test_schedule_uses_one_jst_phase_definition_with_sleep_precedence():
    schedule = MonitorSchedule(
        day_start_hour=7,
        night_start_hour=23,
        sleep_start_hour=2,
        sleep_end_hour=7,
    )

    assert schedule.phase(_jst(12)) == "day"
    assert schedule.phase(_jst(23)) == "night"
    assert schedule.phase(_jst(1)) == "night"
    assert schedule.phase(_jst(2)) == "sleep"
    assert schedule.phase(_jst(6, 59)) == "sleep"
    assert schedule.phase(_jst(7)) == "day"


def test_schedule_supports_cross_midnight_sleep_window():
    schedule = MonitorSchedule(
        day_start_hour=8,
        night_start_hour=22,
        sleep_start_hour=23,
        sleep_end_hour=6,
    )

    assert schedule.is_sleeping(_jst(23))
    assert schedule.is_sleeping(_jst(5))
    assert not schedule.is_sleeping(_jst(6))
    assert schedule.seconds_until_wake(_jst(23, 30)) == 6 * 3600 + 30 * 60


def test_schedule_from_nested_config_takes_precedence_over_legacy_keys():
    schedule = MonitorSchedule.from_config(
        {
            "day_start_hour": 8,
            "night_start_hour": 0,
            "sleep_hours": [1, 5],
            "monitor_schedule": {
                "day_start_hour": 7,
                "night_start_hour": 23,
                "sleep_hours": [2, 7],
                "pause_content_monitors": False,
            },
        }
    )

    assert schedule.day_start_hour == 7
    assert schedule.night_start_hour == 23
    assert schedule.sleep_start_hour == 2
    assert schedule.sleep_end_hour == 7
    assert schedule.pause_content_monitors is False
    assert schedule.phase(_jst(3)) == "night"


def test_nested_sleep_start_end_outrank_legacy_sleep_pair():
    schedule = MonitorSchedule.from_config(
        {
            "sleep_hours": [1, 5],
            "monitor_schedule": {
                "sleep_start_hour": 3,
                "sleep_end_hour": 6,
            },
        }
    )

    assert (schedule.sleep_start_hour, schedule.sleep_end_hour) == (3, 6)


def test_schedule_from_legacy_facade_values_remains_compatible():
    class LegacyConfig:
        DAY_START_HOUR = 7
        NIGHT_START_HOUR = 0
        SLEEP_START_HOUR = 2
        SLEEP_END_HOUR = 7

    schedule = MonitorSchedule.from_config(LegacyConfig())

    assert schedule.day_start_hour == 7
    assert schedule.night_start_hour == 0
    assert schedule.sleep_start_hour == 2
    assert schedule.sleep_end_hour == 7
    assert schedule.phase(_jst(1)) == "night"
    assert schedule.phase(_jst(3)) == "sleep"


def test_schedule_converts_aware_non_jst_time_to_jst():
    schedule = MonitorSchedule(day_start_hour=7, night_start_hour=23)
    cst = datetime(2026, 9, 9, 22, 0, tzinfo=timezone(timedelta(hours=8)))
    assert schedule.phase(cst) == "night"


def test_schedule_window_summary_is_explicit_about_all_windows():
    schedule = MonitorSchedule(
        day_start_hour=7,
        night_start_hour=23,
        sleep_start_hour=2,
        sleep_end_hour=7,
    )
    text = schedule.window_summary()
    assert "日间 07:00" in text
    assert "深夜 23:00" in text
    assert "休眠 02:00" in text


def test_schedule_invalid_equal_day_night_boundary_falls_back_safely():
    schedule = MonitorSchedule.from_config({
        "monitor_schedule": {
            "day_start_hour": 9,
            "night_start_hour": 9,
            "sleep_hours": [2, 7],
        }
    })
    assert (schedule.day_start_hour, schedule.night_start_hour) == (7, 0)
