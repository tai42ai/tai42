"""RQ schedule import/export tools: round trips, upsert/skip modes, and the
malformed/disabled/relative-interval error reporting.
"""

from __future__ import annotations

import pytest

from tai42_backend_rq import tools

from .conftest import (
    FakeSyncRedis,
    _make_stateful_scheduler,
    _patch_redis,
    _StatefulScheduledJob,
)


async def test_import_export_round_trip_interval(monkeypatch):
    """An interval schedule survives import then export unchanged, id == name."""
    store: dict[str, _StatefulScheduledJob] = {}
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    entry = {
        "name": "sched-interval",
        "args": [],
        "kwargs": {"tool_name": "do_thing"},
        "schedule": {"__type__": "interval", "every": 60, "relative": False},
        "enabled": True,
    }

    imported = await tools.backend_import_schedules([entry])
    assert imported == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}
    # Job id is the schedule name.
    assert "sched-interval" in store

    exported = await tools.backend_export_schedules()
    assert len(exported) == 1
    record = exported[0]
    assert record["name"] == "sched-interval"
    # The tool dispatch kwargs survive the round trip.
    assert record["kwargs"] == {"tool_name": "do_thing"}
    # Schedule reconstructs to canonical interval form (seconds coerced to float).
    assert record["schedule"] == {"__type__": "interval", "every": 60.0, "relative": False}
    assert record["enabled"] is True


async def test_import_export_round_trip_cron(monkeypatch):
    """A crontab schedule survives import then export unchanged, id == name."""
    store: dict[str, _StatefulScheduledJob] = {}
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    entry = {
        "name": "sched-cron",
        "args": [],
        "kwargs": {"tool_name": "report"},
        "schedule": {
            "__type__": "crontab",
            "minute": "0",
            "hour": "9",
            "day_of_month": "*",
            "month_of_year": "*",
            "day_of_week": "1",
        },
        "enabled": True,
    }

    imported = await tools.backend_import_schedules([entry])
    assert imported == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}
    assert "sched-cron" in store
    # The cron string was mirrored into the job meta by the shared create logic.
    assert store["sched-cron"].meta == {"cron_string": "0 9 * * 1"}

    exported = await tools.backend_export_schedules()
    assert len(exported) == 1
    record = exported[0]
    assert record["name"] == "sched-cron"
    assert record["kwargs"] == {"tool_name": "report"}
    assert record["schedule"] == entry["schedule"]
    assert record["enabled"] is True


async def test_import_upsert_overwrites_existing(monkeypatch):
    """Re-importing an existing name under overwrite replaces it in place, reported as
    updated."""
    store: dict[str, _StatefulScheduledJob] = {}
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    first = {
        "name": "sched-1",
        "args": [],
        "kwargs": {"tool_name": "do_thing"},
        "schedule": {"__type__": "interval", "every": 60, "relative": False},
        "enabled": True,
    }
    await tools.backend_import_schedules([first])

    second = {
        "name": "sched-1",
        "args": [],
        "kwargs": {"tool_name": "do_thing"},
        "schedule": {"__type__": "interval", "every": 300, "relative": False},
        "enabled": True,
    }
    result = await tools.backend_import_schedules([second], "overwrite")
    assert result == {"created": 0, "updated": 1, "skipped": 0, "skipped_existing": 0, "errors": []}

    # A single job remains, carrying the new interval.
    assert list(store.keys()) == ["sched-1"]
    assert store["sched-1"].meta == {"interval": 300.0}


async def test_import_skip_leaves_existing_schedule(monkeypatch):
    """Under skip (the default) an existing name is left in place — not re-applied —
    and counted as a clean skip."""
    store: dict[str, _StatefulScheduledJob] = {}
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    first = {
        "name": "sched-1",
        "args": [],
        "kwargs": {"tool_name": "do_thing"},
        "schedule": {"__type__": "interval", "every": 60, "relative": False},
        "enabled": True,
    }
    await tools.backend_import_schedules([first])

    second = {
        "name": "sched-1",
        "args": [],
        "kwargs": {"tool_name": "do_thing"},
        "schedule": {"__type__": "interval", "every": 300, "relative": False},
        "enabled": True,
    }
    result = await tools.backend_import_schedules([second])
    assert result == {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 1, "errors": []}

    # The original schedule is untouched — the 300s re-import never applied.
    assert list(store.keys()) == ["sched-1"]
    assert store["sched-1"].meta == {"interval": 60.0}


async def test_import_malformed_record_is_reported_not_silent(monkeypatch):
    """A record with a bad schedule type lands in errors and does not abort the batch."""
    store: dict[str, _StatefulScheduledJob] = {}
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    good = {
        "name": "sched-ok",
        "args": [],
        "kwargs": {"tool_name": "do_thing"},
        "schedule": {"__type__": "interval", "every": 60, "relative": False},
        "enabled": True,
    }
    bad = {
        "name": "sched-bad",
        "args": [],
        "kwargs": {},
        "schedule": {"__type__": "nonsense"},
        "enabled": True,
    }

    result = await tools.backend_import_schedules([good, bad])
    assert result["created"] == 1
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 1
    assert result["errors"][0]["name"] == "sched-bad"
    assert "interval" in result["errors"][0]["error"] or "crontab" in result["errors"][0]["error"]
    # The good record still applied despite the malformed sibling.
    assert "sched-ok" in store
    assert "sched-bad" not in store


async def test_export_raises_when_meta_lacks_schedule(monkeypatch):
    """A stored job with no interval or cron string in meta raises loudly on export."""
    store = {
        "sched-broken": _StatefulScheduledJob("sched-broken", "tool_execution", [], {"tool_name": "x"}, meta={}),
    }
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    with pytest.raises(ValueError, match="cannot reconstruct its schedule"):
        await tools.backend_export_schedules()


async def test_import_disabled_schedule_is_skipped_and_reported(monkeypatch):
    """An ``enabled=False`` record is skipped and surfaced, never enqueued as a live job."""
    store: dict[str, _StatefulScheduledJob] = {}
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    entry = {
        "name": "sched-disabled",
        "args": [],
        "kwargs": {"tool_name": "do_thing"},
        "schedule": {"__type__": "interval", "every": 60, "relative": False},
        "enabled": False,
    }

    result = await tools.backend_import_schedules([entry])
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
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    entry = {
        "name": "sched-fractional",
        "args": [],
        "kwargs": {"tool_name": "do_thing"},
        "schedule": {"__type__": "interval", "every": 0.5, "relative": False},
        "enabled": True,
    }

    result = await tools.backend_import_schedules([entry])
    assert result["created"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["name"] == "sched-fractional"
    assert "whole seconds" in result["errors"][0]["error"]
    assert "sched-fractional" not in store


async def test_import_relative_interval_is_surfaced(monkeypatch):
    """A ``relative=True`` interval is created but the dropped flag is surfaced, not silent."""
    store: dict[str, _StatefulScheduledJob] = {}
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    entry = {
        "name": "sched-rel",
        "args": [],
        "kwargs": {"tool_name": "do_thing"},
        "schedule": {"__type__": "interval", "every": 60, "relative": True},
        "enabled": True,
    }

    result = await tools.backend_import_schedules([entry])
    # Still created (the interval is applied), with a visible note about the dropped flag.
    assert result["created"] == 1
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 0
    assert result["errors"][0]["name"] == "sched-rel"
    assert "relative" in result["errors"][0]["error"]
    assert "sched-rel" in store


async def test_import_apply_failure_preserves_existing_schedule(monkeypatch):
    """A failure mid-apply on an existing name (under overwrite) records the error and
    leaves the prior schedule intact — no pre-cancel destroys it."""
    store = {
        "sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {"tool_name": "orig"}, {"interval": 60.0}),
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

    monkeypatch.setattr(tools, "Scheduler", FailingScheduler)
    _patch_redis(monkeypatch, FakeSyncRedis())

    entry = {
        "name": "sched-1",
        "args": [],
        "kwargs": {"tool_name": "new"},
        "schedule": {"__type__": "interval", "every": 300, "relative": False},
        "enabled": True,
    }

    result = await tools.backend_import_schedules([entry], "overwrite")
    assert result["created"] == 0
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1
    assert "apply boom" in result["errors"][0]["error"]
    # The prior schedule survived — it was never cancelled before the failing apply.
    assert "sched-1" in store
    assert store["sched-1"].kwargs == {"tool_name": "orig"}
    assert store["sched-1"].meta == {"interval": 60.0}
