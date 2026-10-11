"""The shared schedule-apply path, the relative refusal, and the scheduler factory."""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.backend import CrontabCadence, IntervalCadence

from tai42_backend_rq.queue import TaskBudgetQueue
from tai42_backend_rq.schedules import (
    apply_normalized_schedule,
    crontab_string,
    interval_seconds,
    refuse_relative,
    scheduler_for,
)


def test_interval_seconds_returns_whole_seconds():
    assert interval_seconds(IntervalCadence(every=60.0)) == 60
    assert interval_seconds(IntervalCadence(every=1)) == 1


@pytest.mark.parametrize("every", [0.5, 90.5])
def test_interval_seconds_rejects_unrepresentable_values(every: float):
    """The RQ scheduler recurs on ``int(interval)`` seconds: a fractional
    interval would silently drift, and a sub-second one would truncate to 0
    and never recur — both must raise instead."""
    with pytest.raises(ValueError, match="whole seconds"):
        interval_seconds(IntervalCadence(every=every))


def test_crontab_string_builds_five_fields():
    assert crontab_string(CrontabCadence(minute="0", hour="9", day_of_week="1")) == "0 9 * * 1"


def test_refuse_relative():
    refuse_relative(IntervalCadence(every=5))
    refuse_relative(CrontabCadence())
    with pytest.raises(
        ValueError, match=r"the rq backend does not support relative interval schedules \(relative=True\)"
    ):
        refuse_relative(IntervalCadence(every=5, relative=True))


def test_the_scheduler_moves_jobs_onto_the_task_budget_queue():
    scheduler = scheduler_for(object())
    assert scheduler.queue_class is TaskBudgetQueue


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
    await apply_normalized_schedule(scheduler, IntervalCadence(every=60.0), _func, [1], {"a": 2}, "s1")

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
    with pytest.raises(ValueError, match="whole seconds"):
        await apply_normalized_schedule(scheduler, IntervalCadence(every=0.5), _func, [], {}, "s1")
    # Nothing was written for the rejected schedule.
    assert scheduler.schedule_calls == []


async def test_apply_refuses_a_relative_interval():
    scheduler = RecordingScheduler()
    with pytest.raises(ValueError, match="does not support relative interval schedules"):
        await apply_normalized_schedule(scheduler, IntervalCadence(every=60, relative=True), _func, [], {}, "s1")
    assert scheduler.schedule_calls == []


async def test_apply_crontab_mirrors_meta():
    scheduler = RecordingScheduler()
    await apply_normalized_schedule(scheduler, CrontabCadence(minute="30", hour="6"), _func, [], {}, "s2")

    [(cron, call)] = scheduler.cron_calls
    assert cron == "30 6 * * *"
    assert call["meta"] == {"cron_string": "30 6 * * *"}
    assert call["id"] == "s2"


def test_a_task_budget_queue_defaults_its_job_timeout_to_task_timeout(monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("RQ_TASK_TIMEOUT", "77")
    reset_all_settings()
    try:
        queue = TaskBudgetQueue("default", connection=object())
        explicit = TaskBudgetQueue("default", connection=object(), default_timeout=5)
    finally:
        monkeypatch.delenv("RQ_TASK_TIMEOUT", raising=False)
        reset_all_settings()
    assert queue._default_timeout == 77
    assert explicit._default_timeout == 5


def test_a_job_moved_onto_the_queue_without_a_timeout_gets_the_budget(monkeypatch: pytest.MonkeyPatch):
    """rq gives a scheduled job without its own timeout the queue's default when it is moved."""
    from rq.job import Job
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("RQ_TASK_TIMEOUT", "77")
    reset_all_settings()
    try:
        queue = TaskBudgetQueue("default", connection=object())
    finally:
        monkeypatch.delenv("RQ_TASK_TIMEOUT", raising=False)
        reset_all_settings()
    job = Job.__new__(Job)
    job.timeout = None
    monkeypatch.setattr(queue, "get_redis_server_version", lambda: (7, 0, 0))
    queue._prepare_for_queue(job)
    assert job.timeout == 77
