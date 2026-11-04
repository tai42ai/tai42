"""The rq schedule store: a record written as an rq-scheduler job firing ``tool_execution``."""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.backend import IntervalCadence, ScheduleRecord

from tai42_backend_rq import schedule_store
from tai42_backend_rq import schedules as schedules_module
from tai42_backend_rq.queue import TaskBudgetQueue
from tai42_backend_rq.settings import RqSettings, rq_settings
from tai42_backend_rq.tasks import tool_execution

from .conftest import _make_stateful_scheduler, make_client_ctx, scheduler_factory

_HOURLY = IntervalCadence(every=3600)


def _record(*, enabled: bool = True) -> ScheduleRecord:
    return ScheduleRecord(
        name="nightly", tool="sample_tool", args=[1], kwargs={"x": 7}, cadence=_HOURLY, enabled=enabled
    )


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def fake_apply(scheduler: Any, norm: Any, func: Any, args: Any, kwargs: Any, name: Any) -> None:
        calls.append((norm, func, args, kwargs, name))

    monkeypatch.setattr(schedule_store, "apply_normalized_schedule", fake_apply)
    monkeypatch.setattr(schedule_store, "client_ctx", make_client_ctx(object()))
    return calls


def test_the_store_holds_no_relative_interval() -> None:
    assert schedule_store.RqScheduleStore().relative_intervals is False


async def test_write_schedules_tool_execution_naming_the_tool(
    applied: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schedule_store, "scheduler_for", scheduler_factory(lambda queue_name=None, connection=None: object())
    )
    await schedule_store.RqScheduleStore().write(_record())

    [(norm, func, args, kwargs, name)] = applied
    assert norm == _HOURLY
    assert func is tool_execution
    assert args == [1]
    assert name == "nightly"
    # The fired job dispatches back onto the record's tool by name.
    assert kwargs == {"x": 7, rq_settings().tool_name_arg: "sample_tool"}


async def test_write_refuses_a_disabled_record(applied: list[tuple[Any, ...]]) -> None:
    with pytest.raises(ValueError, match="'nightly' has enabled=False; the rq backend holds no disabled schedule"):
        await schedule_store.RqScheduleStore().write(_record(enabled=False))
    assert applied == []


async def test_write_binds_the_settings_queue_name(
    applied: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler enqueues due jobs onto the ``queue_name`` setting's queue, so a schedule
    fires onto the same per-stack queue the worker consumes."""
    captured: dict[str, Any] = {}

    def fake_scheduler(queue_name: Any = None, connection: Any = None, queue_class: Any = None) -> Any:
        captured["queue_name"] = queue_name
        captured["queue_class"] = queue_class
        return object()

    settings = RqSettings(queue_name="tai42_e2e_abc123:default")
    monkeypatch.setattr(schedules_module, "rq_settings", lambda: settings)
    monkeypatch.setattr(schedules_module, "Scheduler", fake_scheduler)

    await schedule_store.RqScheduleStore().write(_record())

    assert captured == {"queue_name": "tai42_e2e_abc123:default", "queue_class": TaskBudgetQueue}


async def test_an_update_of_a_schedule_gone_meanwhile_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheduler no longer holds the job: the update answers ``False`` and writes nothing."""
    monkeypatch.setattr(schedule_store, "scheduler_for", scheduler_factory(_make_stateful_scheduler({})))
    monkeypatch.setattr(schedule_store, "client_ctx", make_client_ctx(object()))

    assert await schedule_store.RqScheduleStore().update("gone", cadence=_HOURLY, next_run_at_ts=None) is False
