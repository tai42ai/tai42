"""Behavior of the ``backend_*`` tool surface against fake Redis/scheduler
doubles: worker/task reads, the schedule CRUD paths, and the export/import
backup round-trip."""

from __future__ import annotations

import base64
import pickle
import zlib
from datetime import UTC, datetime
from typing import Any

import pytest
from rq.exceptions import NoSuchJobError
from rq.job import JobStatus

from tai42_backend_rq import tools
from tai42_backend_rq.settings import rq_settings

from .conftest import FakeAsyncRedis, make_client_ctx


def _patch_redis(monkeypatch, redis) -> None:
    """Make ``client_ctx(...)`` inside the tools module yield the fake."""
    monkeypatch.setattr(tools, "client_ctx", make_client_ctx(redis))


class FakeSyncRedis:
    """Sync stand-in for the paths the scheduler-based tools run off-loop."""

    def __init__(self, kv: dict[str, str] | None = None, zsets: dict[str, dict[str, float]] | None = None) -> None:
        self.kv = kv or {}
        self.zsets = zsets or {}

    def exists(self, key: str) -> int:
        return int(key in self.kv)

    def delete(self, key: str) -> int:
        return int(self.kv.pop(key, None) is not None)

    def zscore(self, key: str, member: str) -> float | None:
        return self.zsets.get(key, {}).get(member)


class FakeJob:
    def __init__(self, meta: dict[str, Any] | None = None, args: list | None = None, kwargs: dict | None = None):
        self.meta = {"interval": 60} if meta is None else meta
        self.args = args or []
        self.kwargs = kwargs or {}
        self.enqueued_at = None


def _make_scheduler(fetch_behavior, contains: bool = True):
    """Build a fake Scheduler class whose ``fetch`` runs ``fetch_behavior``."""

    class FakeJobClass:
        @staticmethod
        def fetch(name, connection=None):
            return fetch_behavior()

    class FakeScheduler:
        job_class = FakeJobClass

        def __init__(self, connection=None):
            self.connection = connection

        def __contains__(self, item):
            return contains

    return FakeScheduler


# --- worker / task reads over raw structures -------------------------------


def _fresh_heartbeat() -> str:
    return datetime.now(UTC).isoformat()


async def test_ping_worker_specific_and_all(monkeypatch):
    settings = rq_settings()
    # The registry set stores full worker KEYS; the hashes live under them.
    redis = FakeAsyncRedis(
        hashes={
            settings.rq_worker_key("w1"): {"last_heartbeat": _fresh_heartbeat().encode()},
            settings.rq_worker_key("w2"): {"last_heartbeat": b"2000-01-01T00:00:00Z"},
        },
        sets={settings.rq_workers_key: {b"rq:worker:w1", b"rq:worker:w2"}},
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_ping_worker("w1") == {"w1": "pong"}
    # Stale heartbeat -> not alive.
    all_ping = await tools.backend_ping_worker(None)
    assert all_ping == {"w1": "pong"}
    assert await tools.backend_list_active_workers() == ["w1"]


async def test_task_status(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(hashes={settings.rq_job_key("t1"): {"status": b"queued"}})
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_task_status("t1") == "queued"
    assert await tools.backend_task_status("missing") == "unknown"


def _finished_result_redis(task_id: str, return_value: Any) -> FakeAsyncRedis:
    settings = rq_settings()
    encoded = base64.b64encode(pickle.dumps(return_value))
    return FakeAsyncRedis(
        hashes={settings.rq_job_key(task_id): {"status": JobStatus.FINISHED.encode()}},
        streams={settings.rq_result_key(task_id): [(b"0-1", {b"return_value": encoded})]},
    )


async def test_task_result_returns_decoded_value(monkeypatch):
    """A well-formed stored result decodes to its original value."""
    _patch_redis(monkeypatch, _finished_result_redis("task-ok", {"answer": 42}))
    assert await tools.backend_task_result("task-ok") == {"answer": 42}


async def test_task_result_returns_the_latest_run(monkeypatch):
    """A recurring job re-uses its id, appending one stream entry per run; the
    NEWEST entry is the task's result."""
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key("t-multi"): {"status": JobStatus.FINISHED.encode()}},
        streams={
            settings.rq_result_key("t-multi"): [
                (b"0-1", {b"return_value": base64.b64encode(pickle.dumps("old"))}),
                (b"0-2", {b"return_value": base64.b64encode(pickle.dumps("new"))}),
            ]
        },
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_task_result("t-multi") == "new"


async def test_task_result_raises_on_corrupt_result(monkeypatch):
    """A corrupt stored result raises loudly instead of returning a string."""
    settings = rq_settings()
    task_id = "task-corrupt"
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key(task_id): {"status": JobStatus.FINISHED.encode()}},
        streams={settings.rq_result_key(task_id): [(b"0-1", {b"return_value": b"!!!not-base64-or-pickle!!!"})]},
    )
    _patch_redis(monkeypatch, redis)

    with pytest.raises(ValueError, match=f"Failed to decode result for task {task_id}"):
        await tools.backend_task_result(task_id)


async def test_task_result_states(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={
            settings.rq_job_key("t-stopped"): {"status": JobStatus.STOPPED.encode()},
            settings.rq_job_key("t-started"): {"status": JobStatus.STARTED.encode()},
            settings.rq_job_key("t-empty"): {"status": JobStatus.FINISHED.encode()},
        },
    )
    _patch_redis(monkeypatch, redis)

    assert "not found" in await tools.backend_task_result("missing")
    assert "did not complete" in await tools.backend_task_result("t-stopped")
    # No timeout: snapshot without waiting.
    assert "is not ready" in await tools.backend_task_result("t-started")
    # Finished but no stream entry.
    assert "No result found" in await tools.backend_task_result("t-empty")


async def test_task_result_failed_raises_the_stored_failure(monkeypatch):
    """A FAILED task raises the persisted traceback text — never an error
    string return."""
    settings = rq_settings()
    exc_string = base64.b64encode(zlib.compress(b"Traceback ...\nValueError: boom"))
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key("t-failed"): {"status": JobStatus.FAILED.encode()}},
        streams={settings.rq_result_key("t-failed"): [(b"0-1", {b"exc_string": exc_string})]},
    )
    _patch_redis(monkeypatch, redis)

    with pytest.raises(RuntimeError, match="ValueError: boom"):
        await tools.backend_task_result("t-failed")


async def test_task_result_failed_without_details_still_raises(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key("t-failed"): {"status": JobStatus.FAILED.encode()}},
    )
    _patch_redis(monkeypatch, redis)

    with pytest.raises(RuntimeError, match="no failure details retained"):
        await tools.backend_task_result("t-failed")


async def test_task_result_finished_with_empty_payload_is_none(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key("t-none"): {"status": JobStatus.FINISHED.encode()}},
        streams={settings.rq_result_key("t-none"): [(b"0-1", {})]},
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_task_result("t-none") is None


async def test_task_result_polls_until_finished(monkeypatch):
    """With a timeout, a running task is polled until it finishes."""
    settings = rq_settings()
    redis = _finished_result_redis("t-poll", "done")
    statuses = iter([JobStatus.STARTED.encode(), JobStatus.FINISHED.encode()])
    real_hget = redis.hget

    async def hget(key: str, field: str) -> Any:
        if key == settings.rq_job_key("t-poll") and field == "status":
            return next(statuses)
        return await real_hget(key, field)

    redis.hget = hget  # type: ignore[method-assign]
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_task_result("t-poll", timeout=5) == "done"


async def test_task_result_timeout_elapses(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(hashes={settings.rq_job_key("t-slow"): {"status": JobStatus.STARTED.encode()}})
    _patch_redis(monkeypatch, redis)

    assert "is not ready" in await tools.backend_task_result("t-slow", timeout=0.3)


async def test_active_reserved_scheduled_stats_queues(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={
            settings.rq_worker_key("w1"): {
                "current_job": b"j1",
                "queues": b'["default","high"]',
                "state": b"busy",
            },
            settings.rq_worker_key("w2"): {"queues": b"default,low"},
            settings.rq_job_key("j1"): {b"status": b"started"},
        },
        sets={
            # Both registry sets store full KEYS ("rq:worker:<name>" /
            # "rq:queue:<name>"), never bare names.
            settings.rq_workers_key: {b"rq:worker:w1", b"rq:worker:w2"},
            settings.rq_queues_key: {b"rq:queue:default"},
        },
        lists={settings.rq_queue_key("default"): [b"j2", b"j3"]},
        zsets={
            # Recurring schedules and ETA/countdown jobs live in separate
            # zsets; the scheduled-tasks read merges both.
            settings.rq_scheduler_zset: {"sched-1": 123.0},
            settings.rq_scheduled_registry_key("default"): {"eta-1": 456.0},
        },
    )
    _patch_redis(monkeypatch, redis)

    active = await tools.backend_active_tasks("w1")
    assert active == {"w1": [{"id": "j1", "data": {"status": "started"}}]}
    # All-workers path: w2 has no current job.
    assert set(await tools.backend_active_tasks()) == {"w1"}

    assert await tools.backend_reserved_tasks() == {"default": ["j2", "j3"]}
    assert await tools.backend_scheduled_tasks() == {
        "sched-1": {"next_run_at_ts": 123.0},
        "eta-1": {"next_run_at_ts": 456.0},
    }

    queues = await tools.backend_worker_queues()
    assert queues == {"w1": ["default", "high"], "w2": ["default", "low"]}
    assert await tools.backend_worker_queues("w2") == {"w2": ["default", "low"]}


async def test_worker_stats(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={settings.rq_worker_key("w1"): {b"state": b"busy", b"successful_job_count": b"7"}},
        sets={settings.rq_workers_key: {b"rq:worker:w1"}},
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_worker_stats() == {"w1": {"state": "busy", "successful_job_count": "7"}}
    assert await tools.backend_worker_stats("w1") == {"w1": {"state": "busy", "successful_job_count": "7"}}


# --- cancel ----------------------------------------------------------------


async def test_cancel_task_scheduled_branch(monkeypatch):
    canceled: list[str] = []

    class FakeScheduler:
        def __init__(self, connection=None):
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


# --- schedule reads ---------------------------------------------------------


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
        def __init__(self, connection=None):
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


# --- run now / enable -------------------------------------------------------


def _run_now_scheduler(store: dict[str, FakeJob], enqueued: list[FakeJob]):
    class FakeJobClass:
        @staticmethod
        def fetch(name, connection=None):
            return store[name]

    class FakeScheduler:
        job_class = FakeJobClass

        def __init__(self, connection=None):
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


# --- update -----------------------------------------------------------------


class _StatefulScheduledJob:
    """A stored scheduled job carrying the fields the export tool reads."""

    def __init__(self, job_id, func_name, args, kwargs, meta):
        self.id = job_id
        self.func_name = func_name
        self.args = args
        self.kwargs = kwargs
        self.meta = meta
        self.enqueued_at = None


# Fixed stand-in for "the next time the cron fires" (the fake cannot evaluate
# a cron expression).
_FAKE_CRON_NEXT_TS = datetime(2032, 1, 1, tzinfo=UTC).timestamp()


def _make_stateful_scheduler(store: dict[str, _StatefulScheduledJob], zset: dict[str, float] | None = None):
    """Build a fake Scheduler class backed by ``store`` (name -> job).

    ``schedule`` and ``cron`` mirror the real scheduler's create signatures,
    storing the interval seconds or cron string in the job ``meta`` exactly as
    the create path does, so an export reads back what an import wrote. When
    ``zset`` is given, the create/cancel/change-time calls maintain it like the
    real scheduler maintains its scheduled-jobs zset.
    """
    times = zset if zset is not None else {}

    class FakeJobClass:
        @staticmethod
        def fetch(name, connection=None):
            if name not in store:
                raise NoSuchJobError(name)
            return store[name]

    class FakeScheduler:
        job_class = FakeJobClass

        def __init__(self, connection=None):
            self.connection = connection

        def __contains__(self, name):
            return name in store

        def cancel(self, name):
            store.pop(name, None)
            times.pop(name, None)

        def get_jobs(self, with_times=False):
            if with_times:
                return [(job, datetime(2030, 1, 1)) for job in store.values()]
            return list(store.values())

        def schedule(self, *, scheduled_time, func, args, kwargs, interval, id, meta, **rest):
            store[id] = _StatefulScheduledJob(
                id,
                getattr(func, "__name__", "tool_execution"),
                list(args),
                dict(kwargs),
                dict(meta),
            )
            times[id] = scheduled_time.timestamp()

        def cron(self, cron_string, *, func, args, kwargs, id, meta, **rest):
            store[id] = _StatefulScheduledJob(
                id,
                getattr(func, "__name__", "tool_execution"),
                list(args),
                dict(kwargs),
                dict(meta),
            )
            times[id] = _FAKE_CRON_NEXT_TS

        def change_execution_time(self, job, date_time):
            if job.id not in times:
                raise ValueError("Job not in scheduled jobs queue")
            times[job.id] = date_time.timestamp()

    return FakeScheduler


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


# --- export / import round-trip ---------------------------------------------


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
    assert imported == {"created": 1, "updated": 0, "skipped": 0, "errors": []}
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
    assert imported == {"created": 1, "updated": 0, "skipped": 0, "errors": []}
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
    """Re-importing an existing name overwrites it in place, reported as updated."""
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
    assert result == {"created": 0, "updated": 1, "skipped": 0, "errors": []}

    # A single job remains, carrying the new interval.
    assert list(store.keys()) == ["sched-1"]
    assert store["sched-1"].meta == {"interval": 300.0}


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
    """A failure mid-apply on an existing name records the error and leaves the prior schedule intact."""
    store = {
        "sched-1": _StatefulScheduledJob("sched-1", "tool_execution", [], {"tool_name": "orig"}, {"interval": 60.0}),
    }

    class FailingScheduler:
        """Applies fail, but existence/cancel behave normally, so we can prove the old survives."""

        def __init__(self, connection=None):
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

    result = await tools.backend_import_schedules([entry])
    assert result["created"] == 0
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1
    assert "apply boom" in result["errors"][0]["error"]
    # The prior schedule survived — it was never cancelled before the failing apply.
    assert "sched-1" in store
    assert store["sched-1"].kwargs == {"tool_name": "orig"}
    assert store["sched-1"].meta == {"interval": 60.0}
