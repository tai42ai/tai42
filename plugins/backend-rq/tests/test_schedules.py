"""ScheduleRecord validation and the shared schedule-apply path."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_backend_rq.schedules import ScheduleRecord, apply_normalized_schedule, crontab_string, interval_seconds


def test_schedule_record_accepts_canonical_interval():
    record = ScheduleRecord(
        name="s1",
        schedule={"__type__": "interval", "every": 60.0, "relative": False},
        enabled=True,
    )
    assert record.args == []
    assert record.kwargs == {}


def test_schedule_record_accepts_canonical_crontab():
    record = ScheduleRecord(
        name="s1",
        schedule={
            "__type__": "crontab",
            "minute": "0",
            "hour": "9",
            "day_of_month": "*",
            "month_of_year": "*",
            "day_of_week": "1",
        },
        enabled=False,
    )
    assert record.enabled is False


@pytest.mark.parametrize(
    "schedule",
    [
        {"__type__": "nonsense"},
        {"__type__": "interval", "every": "sixty"},
        {"__type__": "interval", "every": True},
        {"__type__": "interval", "every": 60, "relative": "yes"},
        {"__type__": "crontab", "minute": "0"},
    ],
)
def test_schedule_record_rejects_malformed_schedules(schedule: dict[str, Any]):
    with pytest.raises(ValueError):  # noqa: PT011 - each case has its own message
        ScheduleRecord(name="s1", schedule=schedule, enabled=True)


def test_interval_seconds_returns_whole_seconds():
    assert interval_seconds({"__type__": "interval", "every": 60.0}) == 60
    assert interval_seconds({"__type__": "interval", "every": 1}) == 1


@pytest.mark.parametrize("every", [0.5, 90.5])
def test_interval_seconds_rejects_unrepresentable_values(every: float):
    """The RQ scheduler recurs on ``int(interval)`` seconds: a fractional
    interval would silently drift, and a sub-second one would truncate to 0
    and never recur — both must raise instead."""
    with pytest.raises(ValueError, match="whole seconds"):
        interval_seconds({"__type__": "interval", "every": every})


def test_crontab_string_builds_five_fields():
    norm = {
        "__type__": "crontab",
        "minute": "0",
        "hour": "9",
        "day_of_month": "*",
        "month_of_year": "*",
        "day_of_week": "1",
    }
    assert crontab_string(norm) == "0 9 * * 1"


class RecordingScheduler:
    def __init__(self) -> None:
        self.schedule_calls: list[dict[str, Any]] = []
        self.cron_calls: list[tuple[str, dict[str, Any]]] = []

    def schedule(self, **kwargs: Any) -> None:
        self.schedule_calls.append(kwargs)

    def cron(self, cron_string: str, **kwargs: Any) -> None:
        self.cron_calls.append((cron_string, kwargs))


def _func() -> None:
    pass


async def test_apply_interval_mirrors_meta():
    scheduler = RecordingScheduler()
    norm = {"__type__": "interval", "every": 60.0, "relative": False}
    await apply_normalized_schedule(scheduler, norm, _func, [1], {"a": 2}, "s1")

    [call] = scheduler.schedule_calls
    # Whole seconds as an int — the exact value the RQ scheduler itself keeps
    # in the job meta, so the mirror reads back identically.
    assert call["interval"] == 60
    assert isinstance(call["interval"], int)
    assert call["meta"] == {"interval": 60}
    assert call["id"] == "s1"
    assert call["args"] == [1]
    assert call["kwargs"] == {"a": 2}


async def test_apply_interval_rejects_fractional_seconds():
    scheduler = RecordingScheduler()
    norm = {"__type__": "interval", "every": 0.5, "relative": False}
    with pytest.raises(ValueError, match="whole seconds"):
        await apply_normalized_schedule(scheduler, norm, _func, [], {}, "s1")
    # Nothing was written for the rejected schedule.
    assert scheduler.schedule_calls == []


async def test_apply_crontab_mirrors_meta():
    scheduler = RecordingScheduler()
    norm = {
        "__type__": "crontab",
        "minute": "30",
        "hour": "6",
        "day_of_month": "*",
        "month_of_year": "*",
        "day_of_week": "*",
    }
    await apply_normalized_schedule(scheduler, norm, _func, [], {}, "s2")

    [(cron, call)] = scheduler.cron_calls
    assert cron == "30 6 * * *"
    assert call["meta"] == {"cron_string": "30 6 * * *"}
    assert call["id"] == "s2"


async def test_apply_unknown_type_raises():
    with pytest.raises(ValueError, match="Unsupported schedule type"):
        await apply_normalized_schedule(RecordingScheduler(), {"__type__": "weird"}, _func, [], {}, "s3")
