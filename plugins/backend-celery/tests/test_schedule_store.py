"""The Celery schedule store: a record saved as the RedBeat entry of its name."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from celery.schedules import crontab, schedule
from tai42_contract.backend import CrontabCadence, IntervalCadence, ScheduleRecord

from tai42_backend_celery.core import schedule_store
from tai42_backend_celery.core.schedule_store import CeleryScheduleStore


class _FakeEntry:
    instances: ClassVar[list[_FakeEntry]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.saved = False
        type(self).instances.append(self)

    def save(self) -> None:
        self.saved = True


@pytest.fixture
def fake_entry(monkeypatch: pytest.MonkeyPatch) -> type[_FakeEntry]:
    _FakeEntry.instances = []
    monkeypatch.setattr(schedule_store, "RedBeatSchedulerEntry", _FakeEntry)
    return _FakeEntry


def _record(cadence: Any, *, enabled: bool = True) -> ScheduleRecord:
    return ScheduleRecord(
        name="nightly", tool="sample_tool", args=[1], kwargs={"b": "x"}, cadence=cadence, enabled=enabled
    )


def test_the_store_honours_a_relative_interval() -> None:
    assert CeleryScheduleStore().relative_intervals is True


async def test_write_saves_an_interval_entry_naming_the_tool(fake_entry: type[_FakeEntry]) -> None:
    await CeleryScheduleStore().write(_record(IntervalCadence(every=30)))

    (entry,) = fake_entry.instances
    assert entry.saved
    assert entry.kwargs["name"] == "nightly"
    assert entry.kwargs["task"] == "celery.tool_execution"
    assert isinstance(entry.kwargs["schedule"], schedule)
    assert entry.kwargs["schedule"].run_every.total_seconds() == 30.0
    assert entry.kwargs["args"] == [1]
    assert entry.kwargs["kwargs"] == {"b": "x", "backend_tool_name": "sample_tool"}
    assert entry.kwargs["enabled"] is True


async def test_write_saves_a_crontab_entry(fake_entry: type[_FakeEntry]) -> None:
    await CeleryScheduleStore().write(_record(CrontabCadence(minute="0", hour="9", day_of_week="1")))

    (entry,) = fake_entry.instances
    assert isinstance(entry.kwargs["schedule"], crontab)
    assert entry.kwargs["schedule"]._orig_day_of_week == "1"


async def test_write_keeps_a_relative_interval(fake_entry: type[_FakeEntry]) -> None:
    await CeleryScheduleStore().write(_record(IntervalCadence(every=30, relative=True)))

    (entry,) = fake_entry.instances
    assert entry.kwargs["schedule"].relative is True


async def test_write_saves_a_disabled_record_as_a_disabled_entry(fake_entry: type[_FakeEntry]) -> None:
    await CeleryScheduleStore().write(_record(IntervalCadence(every=30), enabled=False))

    (entry,) = fake_entry.instances
    assert entry.kwargs["enabled"] is False
