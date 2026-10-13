"""RQ schedule import/export tools: ``ScheduleRecord`` round trips, upsert/skip modes, and the
malformed/disabled/relative-interval error reporting.

The backend's tool-name key is set to a NON-default value here, so the round trip proves the
backend splits the fired tool out of its own key on export and injects it back on import.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tai42_backend_rq import tools

from .conftest import (
    FakeSyncRedis,
    _make_stateful_scheduler,
    _patch_redis,
    _StatefulScheduledJob,
    scheduler_factory,
)

_TOOL_ARG = "custom_target"
_INTERVAL_60 = {"kind": "interval", "every": 60.0, "relative": False}
_CRON = {
    "kind": "crontab",
    "minute": "0",
    "hour": "9",
    "day_of_month": "*",
    "month_of_year": "*",
    "day_of_week": "1",
}


@pytest.fixture(autouse=True)
def _custom_tool_name_arg(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("RQ_TOOL_NAME_ARG", _TOOL_ARG)
    reset_all_settings()
    yield
    monkeypatch.delenv("RQ_TOOL_NAME_ARG", raising=False)
    reset_all_settings()


def _record(name: str, *, tool: str = "do_thing", **overrides: Any) -> dict[str, Any]:
    return {
        "name": name,
        "tool": tool,
        "args": [],
        "kwargs": {"payload": "x"},
        "cadence": _INTERVAL_60,
        "enabled": True,
        **overrides,
    }


def _bind(monkeypatch: pytest.MonkeyPatch, store: dict[str, _StatefulScheduledJob]) -> None:
    monkeypatch.setattr(tools, "scheduler_for", scheduler_factory(_make_stateful_scheduler(store)))
    _patch_redis(monkeypatch, FakeSyncRedis())


_CLEAN = {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}


async def test_import_export_round_trip_interval(monkeypatch):
    """An interval schedule survives import then export unchanged, id == name."""
    store: dict[str, _StatefulScheduledJob] = {}
    _bind(monkeypatch, store)
    entry = _record("sched-interval")

    assert await tools.backend_import_schedules([entry]) == _CLEAN
    # Job id is the schedule name; the job carries the tool under the backend's own key.
    assert store["sched-interval"].kwargs == {"payload": "x", _TOOL_ARG: "do_thing"}

    assert await tools.backend_export_schedules() == [entry]


async def test_import_export_round_trip_cron(monkeypatch):
    """A crontab schedule survives import then export unchanged, id == name."""
    store: dict[str, _StatefulScheduledJob] = {}
    _bind(monkeypatch, store)
    entry = _record("sched-cron", tool="report", cadence=_CRON)

    assert await tools.backend_import_schedules([entry]) == _CLEAN
    # The cron string was mirrored into the job meta by the shared create logic.
    assert store["sched-cron"].meta == {"cron_string": "0 9 * * 1"}

    assert await tools.backend_export_schedules() == [entry]


async def test_import_upsert_overwrites_existing(monkeypatch):
    """Re-importing an existing name under overwrite replaces it in place, reported as updated."""
    store: dict[str, _StatefulScheduledJob] = {}
    _bind(monkeypatch, store)
    await tools.backend_import_schedules([_record("sched-1")])

    result = await tools.backend_import_schedules(
        [_record("sched-1", cadence={"kind": "interval", "every": 300})], "overwrite"
    )
    assert result == {"created": 0, "updated": 1, "skipped": 0, "skipped_existing": 0, "errors": []}

    # A single job remains, carrying the new interval.
    assert list(store.keys()) == ["sched-1"]
    assert store["sched-1"].meta == {"interval": 300}


async def test_import_skip_leaves_existing_schedule(monkeypatch):
    """Under skip (the default) an existing name is left in place — not re-applied —
    and counted as a clean skip."""
    store: dict[str, _StatefulScheduledJob] = {}
    _bind(monkeypatch, store)
    await tools.backend_import_schedules([_record("sched-1")])

    result = await tools.backend_import_schedules([_record("sched-1", cadence={"kind": "interval", "every": 300})])
    assert result == {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 1, "errors": []}

    # The original schedule is untouched — the 300s re-import never applied.
    assert list(store.keys()) == ["sched-1"]
    assert store["sched-1"].meta == {"interval": 60}


async def test_import_an_old_format_record_is_reported_not_silent(monkeypatch):
    """A row in the old ``schedule``/no-``tool`` shape fails ``ScheduleRecord``, lands in errors,
    and does not abort the batch."""
    store: dict[str, _StatefulScheduledJob] = {}
    _bind(monkeypatch, store)
    old = {
        "name": "sched-bad",
        "args": [],
        "kwargs": {_TOOL_ARG: "do_thing"},
        "schedule": {"__type__": "interval", "every": 60, "relative": False},
        "enabled": True,
    }

    result = await tools.backend_import_schedules([_record("sched-ok"), old])
    assert result["created"] == 1
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 1
    assert result["errors"][0]["name"] == "sched-bad"
    # The good record still applied despite the malformed sibling.
    assert "sched-ok" in store
    assert "sched-bad" not in store


async def test_export_raises_when_meta_lacks_schedule(monkeypatch):
    """A stored job with no interval or cron string in meta raises loudly on export."""
    store = {
        "sched-broken": _StatefulScheduledJob("sched-broken", "tool_execution", [], {_TOOL_ARG: "x"}, meta={}),
    }
    _bind(monkeypatch, store)

    with pytest.raises(ValueError, match="cannot reconstruct its schedule"):
        await tools.backend_export_schedules()


async def test_export_raises_when_the_job_carries_no_tool_key(monkeypatch):
    store = {
        "sched-orphan": _StatefulScheduledJob("sched-orphan", "tool_execution", [], {}, meta={"interval": 60}),
    }
    _bind(monkeypatch, store)

    with pytest.raises(ValueError, match="schedule 'sched-orphan' carries no 'custom_target'"):
        await tools.backend_export_schedules()


async def test_import_disabled_schedule_is_skipped_and_reported(monkeypatch):
    """An ``enabled=False`` record is skipped and surfaced, never enqueued as a live job."""
    store: dict[str, _StatefulScheduledJob] = {}
    _bind(monkeypatch, store)

    result = await tools.backend_import_schedules([_record("sched-disabled", enabled=False)])
    assert result["created"] == 0
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 0
    assert result["errors"][0]["name"] == "sched-disabled"
    assert "enabled=False" in result["errors"][0]["error"]
    # No active job was created for the disabled schedule.
    assert "sched-disabled" not in store


async def test_import_fractional_interval_is_reported_not_silent(monkeypatch):
    """A sub-second interval would silently become a one-shot in the RQ
    scheduler; the record lands in errors instead of being applied."""
    store: dict[str, _StatefulScheduledJob] = {}
    _bind(monkeypatch, store)

    result = await tools.backend_import_schedules(
        [_record("sched-fractional", cadence={"kind": "interval", "every": 0.5})]
    )
    assert result["created"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["name"] == "sched-fractional"
    assert "whole seconds" in result["errors"][0]["error"]
    assert "sched-fractional" not in store


async def test_import_relative_interval_is_refused_per_row(monkeypatch):
    """A ``relative=True`` interval cannot be honoured: it is a per-row error and nothing is created."""
    store: dict[str, _StatefulScheduledJob] = {}
    _bind(monkeypatch, store)

    result = await tools.backend_import_schedules(
        [_record("sched-rel", cadence={"kind": "interval", "every": 60, "relative": True})]
    )
    assert result["created"] == 0
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 0
    assert result["errors"][0]["name"] == "sched-rel"
    assert "the rq backend does not support relative interval schedules (relative=True)" in result["errors"][0]["error"]
    assert "sched-rel" not in store


async def test_import_apply_failure_preserves_existing_schedule(monkeypatch):
    """A failure mid-apply on an existing name (under overwrite) records the error and
    leaves the prior schedule intact — no pre-cancel destroys it."""
    store = {
        "sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {_TOOL_ARG: "orig"}, {"interval": 60.0}),
    }

    class FailingScheduler:
        """Applies fail, but existence/cancel behave normally, so we can prove the old survives."""

        def __init__(self, queue_name=None, connection=None):
            self.connection = connection

        def __contains__(self, name):
            return name in store

        def cancel(self, name):
            store.pop(name, None)

        def get_jobs(self, with_times=False):
            return list(store.values())

        def schedule(self, **kwargs):
            raise RuntimeError("apply boom")

        def cron(self, *args, **kwargs):
            raise RuntimeError("apply boom")

    monkeypatch.setattr(tools, "scheduler_for", scheduler_factory(FailingScheduler))
    _patch_redis(monkeypatch, FakeSyncRedis())

    result = await tools.backend_import_schedules(
        [_record("sched-1", tool="new", cadence={"kind": "interval", "every": 300})], "overwrite"
    )
    assert result["created"] == 0
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1
    assert "apply boom" in result["errors"][0]["error"]
    # The prior schedule survived — it was never cancelled before the failing apply.
    assert "sched-1" in store
    assert store["sched-1"].kwargs == {_TOOL_ARG: "orig"}
    assert store["sched-1"].meta == {"interval": 60.0}
