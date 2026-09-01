"""消息巡查成功路径的低噪声日志回归测试。"""

from __future__ import annotations

import pytest

from src import fetcher
from src.app import _MemberCycleResult, _message_cycle_summary


def test_message_cycle_summary_contains_counts_and_elapsed_time():
    results = [
        _MemberCycleResult("甲", fetch_ok=True, new_count=0, push_ok=True),
        _MemberCycleResult("乙", fetch_ok=True, new_count=2, push_ok=True),
        _MemberCycleResult("丙", skipped=True),
        _MemberCycleResult("丁"),
    ]

    summary, has_errors = _message_cycle_summary(results, 4.26)

    assert has_errors is True
    assert "成员 4" in summary
    assert "请求成功 2" in summary
    assert "新增 2" in summary
    assert "处理完成 2" in summary
    assert "异常 1" in summary
    assert "跳过 1" in summary
    assert "耗时 4.3s" in summary
    assert "异常成员: 丁" in summary


@pytest.mark.asyncio
async def test_push_member_messages_does_not_repeat_no_new_message_log(monkeypatch):
    logs: list[str] = []
    monkeypatch.setattr(fetcher, "log_all", lambda content, **_kwargs: logs.append(str(content)))
    monkeypatch.setattr(fetcher, "_handle_message", _successful_message)
    monkeypatch.setattr(fetcher.archive, "set_timeline_watermark", lambda *_args: None)
    monkeypatch.setattr(fetcher, "write_time_record", _successful_write)

    member = {"m_name": "测试成员", "group_type": "nogizaka46", "m_id": "1"}
    result = await fetcher._push_member_messages(
        member,
        [{"id": "already-seen", "updated_at": "2026-09-01T00:00:00Z"}],
        [],
        {"already-seen"},
        ["2026-09-01T00:00:00Z"],
        "",
        None,
    )

    assert result is True
    assert not any("无新消息" in line for line in logs)


async def _successful_message(*_args, **_kwargs):
    return True


async def _successful_write(*_args, **_kwargs):
    return None
