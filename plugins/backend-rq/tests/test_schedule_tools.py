"""RQ schedule CRUD tools: cancel/delete/exists/list/get, run-now and enable, and
the update-schedule edits.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from rq.exceptions import NoSuchJobError

from tai42_backend_rq import tools
from tai42_backend_rq.settings import rq_settings

from .conftest import (
    _FAKE_CRON_NEXT_TS,
    FakeAsyncRedis,
    FakeJob,
    FakeSyncRedis,
    _make_stateful_scheduler,
    _patch_redis,
    _StatefulScheduledJob,
)


def _make_scheduler(fetch_behavior, contains: bool = True):
    """Build a fake Scheduler class whose ``fetch`` runs ``fetch_behavior``."""

    class FakeJobClass:
        @staticmethod
        def fetch(name, connection=None):
            return fetch_behavior()

    class FakeScheduler:
        job_class = FakeJobClass

        def __init__(self, queue_name=None, connection=None):
            self.connection = connection

        def __contains__(self, item):
            return contains

    return FakeScheduler


async def test_cancel_task_scheduled_branch(monkeypatch):
    canceled: list[str] = []

    class FakeScheduler:
        def __init__(self, queue_name=None, connection=None):
            pass

        def __contains__(self, item):
            return True

        def cancel(self, name):
            canceled.append(name)

    monkeypatch.setattr(tools, "Scheduler", FakeScheduler)
    _patch_redis(monkeypatch, FakeSyncRedis())

    assert await tools.backend_cancel_task("sched-1") == "Scheduled Task sched-1 canceled"
    assert canceled == ["sched-1"]


async def test_cancel_task_job_branches(monkeypatch):
    settings = rq_settings()
    monkeypatch.setattr(tools, "Scheduler", _make_scheduler(FakeJob, contains=False))
    redis = FakeSyncRedis(kv={settings.rq_job_key("j1"): "1"})
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_cancel_task("j1") == "Task j1 canceled"
    assert settings.rq_job_key("j1") not in redis.kv
    assert await tools.backend_cancel_task("j1") == "Task j1 not found"


async def test_delete_schedule(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        zsets={settings.rq_scheduler_zset: {"sched-1": 1.0}},
        hashes={settings.rq_job_key("sched-1"): {"status": b"scheduled"}},
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_delete_schedule("sched-1") == {"status": "deleted", "name": "sched-1"}
    assert await tools.backend_delete_schedule("sched-1") == {"status": "not_found", "name": "sched-1"}


async def test_disable_schedule_aliases_delete(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(zsets={settings.rq_scheduler_zset: {"sched-1": 1.0}})
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_disable_schedule("sched-1") == {"status": "deleted", "name": "sched-1"}


async def test_schedule_exists(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(zsets={settings.rq_scheduler_zset: {"sched-1": 1.0}})
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_schedule_exists("sched-1") is True
    assert await tools.backend_schedule_exists("other") is False


async def test_list_schedules_reports_next_run(monkeypatch):
    next_run = datetime(2030, 1, 2, 3, 4, 5)  # naive UTC, as the scheduler returns

    class _NamedJob(FakeJob):
        id = "sched-1"

    class FakeScheduler:
        def __init__(self, queue_name=None, connection=None):
            pass

        def get_jobs(self, with_times=False):
            assert with_times is True
            return [(_NamedJob(meta={"interval": 60.0}), next_run)]

    monkeypatch.setattr(tools, "Scheduler", FakeScheduler)
    _patch_redis(monkeypatch, FakeSyncRedis())

    [row] = await tools.backend_list_schedules()
    assert row["name"] == "sched-1"
    # RQ has no disabled-schedule state, so a listed schedule is always live.
    assert row["enabled"] is True
    assert row["meta"] == {"interval": 60.0}
    assert row["next_run_at_ts"] == next_run.replace(tzinfo=UTC).timestamp()
    assert row["next_run_at_iso"] == next_run.replace(tzinfo=UTC).isoformat()


async def test_get_schedule_not_found_when_absent(monkeypatch):
    monkeypatch.setattr(tools, "Scheduler", _make_scheduler(FakeJob, contains=False))
    _patch_redis(monkeypatch, FakeSyncRedis())

    assert await tools.backend_get_schedule("sched-1") == {"status": "not_found", "name": "sched-1"}


async def test_get_schedule_not_found_when_job_missing(monkeypatch):
    """A genuine ``NoSuchJobError`` maps to a clear ``not_found`` result."""

    def _raise_missing():
        raise NoSuchJobError("gone")

    monkeypatch.setattr(tools, "Scheduler", _make_scheduler(_raise_missing))
    _patch_redis(monkeypatch, FakeSyncRedis())

    assert await tools.backend_get_schedule("sched-1") == {"status": "not_found", "name": "sched-1"}


async def test_get_schedule_propagates_real_fetch_failure(monkeypatch):
    """A non-absence failure during fetch must propagate, not be masked."""

    def _raise_boom():
        raise RuntimeError("redis exploded")

    monkeypatch.setattr(tools, "Scheduler", _make_scheduler(_raise_boom))
    _patch_redis(monkeypatch, FakeSyncRedis())

    with pytest.raises(RuntimeError, match="redis exploded"):
        await tools.backend_get_schedule("sched-1")


async def test_get_schedule_returns_details_on_success(monkeypatch):
    settings = rq_settings()
    monkeypatch.setattr(tools, "Scheduler", _make_scheduler(FakeJob))
    _patch_redis(monkeypatch, FakeSyncRedis(zsets={settings.rq_scheduler_zset: {"sched-1": 500.0}}))

    result = await tools.backend_get_schedule("sched-1")
    assert result["name"] == "sched-1"
    assert result["definition"]["schedule"] == {"__type__": "interval", "every": 60}
    assert result["next_run_at_ts"] == 500.0
    assert result["next_run_at_iso"] == datetime.fromtimestamp(500.0, tz=UTC).isoformat()


async def test_get_schedule_cron_definition(monkeypatch):
    monkeypatch.setattr(tools, "Scheduler", _make_scheduler(lambda: FakeJob(meta={"cron_string": "0 9 * * 1"})))
    _patch_redis(monkeypatch, FakeSyncRedis())

    result = await tools.backend_get_schedule("sched-cron")
    assert result["definition"]["schedule"] == {"__type__": "crontab", "cron_string": "0 9 * * 1"}
    assert result["next_run_at_ts"] is None


def _run_now_scheduler(store: dict[str, FakeJob], enqueued: list[FakeJob]):
    class FakeJobClass:
        @staticmethod
        def fetch(name, connection=None):
            return store[name]

    class FakeScheduler:
        job_class = FakeJobClass

        def __init__(self, queue_name=None, connection=None):
            pass

        def __contains__(self, item):
            return item in store

        def enqueue_job(self, job):
            enqueued.append(job)

    return FakeScheduler


async def test_run_schedule_now_enqueues_via_scheduler(monkeypatch):
    """The fetched job is handed to ``Scheduler.enqueue_job`` (which runs it
    now and re-arms the recurrence)."""
    job = FakeJob()
    enqueued: list[FakeJob] = []
    monkeypatch.setattr(tools, "Scheduler", _run_now_scheduler({"sched-1": job}, enqueued))
    _patch_redis(monkeypatch, FakeSyncRedis())

    assert await tools.backend_run_schedule_now("sched-1") == {"status": "queued", "name": "sched-1"}
    assert enqueued == [job]


async def test_run_schedule_now_not_found(monkeypatch):
    enqueued: list[FakeJob] = []
    monkeypatch.setattr(tools, "Scheduler", _run_now_scheduler({}, enqueued))
    _patch_redis(monkeypatch, FakeSyncRedis())

    assert await tools.backend_run_schedule_now("missing") == {"status": "not_found", "name": "missing"}
    assert enqueued == []


async def test_enable_schedule_aliases_run_now(monkeypatch):
    job = FakeJob()
    enqueued: list[FakeJob] = []
    monkeypatch.setattr(tools, "Scheduler", _run_now_scheduler({"sched-1": job}, enqueued))
    _patch_redis(monkeypatch, FakeSyncRedis())

    assert await tools.backend_enable_schedule("sched-1") == {"status": "queued", "name": "sched-1"}
    assert enqueued == [job]


async def test_update_schedule_not_found(monkeypatch):
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler({}))
    _patch_redis(monkeypatch, FakeSyncRedis())

    assert await tools.backend_update_schedule("missing", new_schedule=30) == {
        "status": "not_found",
        "name": "missing",
    }


async def test_update_schedule_no_changes(monkeypatch):
    store = {"sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {}, {"interval": 60.0})}
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store))
    _patch_redis(monkeypatch, FakeSyncRedis())

    assert await tools.backend_update_schedule("sched-1") == {"status": "skipped", "message": "No changes provided"}


def _update_env(monkeypatch, store: dict[str, _StatefulScheduledJob]) -> dict[str, float]:
    """Wire a stateful scheduler + a sync Redis sharing one scheduled-jobs zset."""
    settings = rq_settings()
    zset: dict[str, float] = dict.fromkeys(store, 1.0)
    monkeypatch.setattr(tools, "Scheduler", _make_stateful_scheduler(store, zset))
    _patch_redis(monkeypatch, FakeSyncRedis(zsets={settings.rq_scheduler_zset: zset}))
    return zset


async def test_update_schedule_new_interval(monkeypatch):
    store = {"sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {"a": 1}, {"interval": 60.0})}
    zset = _update_env(monkeypatch, store)

    result = await tools.backend_update_schedule("sched-1", new_schedule=300)
    assert result["status"] == "updated"
    assert result["new_schedule"] == {"interval": 300.0, "cron": None}
    assert store["sched-1"].meta["interval"] == 300.0
    assert store["sched-1"].kwargs == {"a": 1}
    # The reported next run is read back from the scheduler zset.
    assert result["next_run_at_ts"] == zset["sched-1"]


async def test_update_schedule_switch_to_cron_drops_stale_interval(monkeypatch):
    store = {"sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {}, {"interval": 60.0, "keep": 1})}
    _update_env(monkeypatch, store)

    result = await tools.backend_update_schedule("sched-1", new_schedule="0 9 * * 1")
    assert result["status"] == "updated"
    assert result["new_schedule"] == {"interval": None, "cron": "0 9 * * 1"}
    assert store["sched-1"].meta["cron_string"] == "0 9 * * 1"
    # The replaced kind's meta key is gone (custom meta survives), so the
    # schedule reads back as exactly one kind.
    assert "interval" not in store["sched-1"].meta
    assert store["sched-1"].meta["keep"] == 1
    assert result["next_run_at_ts"] == _FAKE_CRON_NEXT_TS


async def test_update_schedule_next_run_only_moves_time_in_place(monkeypatch):
    store = {"sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {}, {"interval": 60.0})}
    zset = _update_env(monkeypatch, store)
    job_before = store["sched-1"]

    at_ts = datetime(2031, 5, 1, tzinfo=UTC).timestamp()
    result = await tools.backend_update_schedule("sched-1", next_run_at_ts=at_ts)
    assert result["status"] == "updated"
    assert result["next_run_at_ts"] == at_ts
    assert result["next_run_at_iso"] == datetime.fromtimestamp(at_ts, tz=UTC).isoformat()
    # No re-create: the stored job is untouched, only its execution time moved.
    assert store["sched-1"] is job_before
    assert zset["sched-1"] == at_ts

    result = await tools.backend_update_schedule("sched-1", next_run_in_ms=60_000)
    assert result["status"] == "updated"
    assert result["next_run_at_ts"] == zset["sched-1"]


async def test_update_schedule_next_run_applies_to_cron(monkeypatch):
    """A next-run change on a crontab schedule shifts only the next firing —
    in place, without re-creating the job."""
    store = {"sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {}, {"cron_string": "0 9 * * 1"})}
    zset = _update_env(monkeypatch, store)

    at_ts = datetime(2031, 5, 1, tzinfo=UTC).timestamp()
    result = await tools.backend_update_schedule("sched-1", next_run_at_ts=at_ts)
    assert result["status"] == "updated"
    assert zset["sched-1"] == at_ts
    assert store["sched-1"].meta == {"cron_string": "0 9 * * 1"}


async def test_update_schedule_rejects_fractional_interval_keeping_the_old(monkeypatch):
    """An interval the RQ scheduler cannot represent raises BEFORE the old
    entry is cancelled, so the prior schedule survives intact."""
    store = {"sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {}, {"interval": 60})}
    _update_env(monkeypatch, store)

    with pytest.raises(ValueError, match="whole seconds"):
        await tools.backend_update_schedule("sched-1", new_schedule=0.5)
    assert store["sched-1"].meta == {"interval": 60}


async def test_update_schedule_new_interval_with_explicit_next_run(monkeypatch):
    store = {"sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {}, {"interval": 60.0})}
    zset = _update_env(monkeypatch, store)

    at_ts = datetime(2031, 5, 1, tzinfo=UTC).timestamp()
    result = await tools.backend_update_schedule("sched-1", new_schedule=300, next_run_at_ts=at_ts)
    assert result["status"] == "updated"
    # The re-created interval schedule starts at the requested time.
    assert zset["sched-1"] == at_ts
    assert result["next_run_at_ts"] == at_ts
