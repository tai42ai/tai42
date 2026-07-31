"""arq-native task tools (active/reserved/scheduled/failed/cancel) and the
schedule CRUD tools over the in-memory fake."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import orjson
import pytest
from arq.jobs import JobDef, JobResult, JobStatus, SerializationError
from arq.utils import timestamp_ms

from tai42_backend_arq import scheduler, tools
from tai42_backend_arq.settings import TaskFailedError
from tests.conftest import DeleteOnLockRedis


def _job_def(job_id: str, score: int) -> JobDef:
    return JobDef(
        function="tool_execution",
        args=(),
        kwargs={},
        job_try=1,
        enqueue_time=datetime.now(UTC),
        score=score,
        job_id=job_id,
    )


def _job_result(job_id: str, success: bool, result: Any = "x") -> JobResult:
    now = datetime.now(UTC)
    return JobResult(
        function="tool_execution",
        args=(),
        kwargs={},
        job_try=1,
        enqueue_time=now,
        score=None,
        job_id=job_id,
        success=success,
        result=result,
        start_time=now,
        finish_time=now,
        queue_name="arq:queue",
    )


class _StatusJob:
    """Factory of fake ``Job`` instances keyed by job id. A list status value
    is consumed one entry per call (the last entry repeats); an exception
    ``abort:`` value is raised instead of returned; a ``result:`` value is the
    ``result_info`` outcome (default ``None`` — no retained result)."""

    def __init__(self, statuses: dict[str, Any]) -> None:
        self._statuses = statuses
        self.abort_calls: list[float | None] = []

    def __call__(self, job_id: str, *args: Any, **kwargs: Any) -> Any:
        outer = self

        class _Job:
            async def status(self) -> JobStatus:
                value = outer._statuses[job_id]
                if isinstance(value, list):
                    value = value.pop(0) if len(value) > 1 else value[0]
                return value

            async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
                outer.abort_calls.append(timeout)
                value = outer._statuses.get(f"abort:{job_id}", True)
                if isinstance(value, Exception):
                    raise value
                return value

            async def result_info(self) -> JobResult | None:
                return outer._statuses.get(f"result:{job_id}")

        return _Job()


# -- task tools -------------------------------------------------------------------


async def test_active_tasks_lists_only_in_progress(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    # in-progress marker keys live in the hash store the fake scan() walks.
    fake_redis._store["arq:in-progress:job-a"] = {}
    fake_redis._store["arq:in-progress:job-b"] = {}
    monkeypatch.setattr(tools, "Job", _StatusJob({"job-a": JobStatus.in_progress, "job-b": JobStatus.complete}))

    assert await tools.backend_active_tasks() == {"job-a": {"status": "in_progress"}}


async def test_reserved_and_scheduled_tasks_split_by_due_time(monkeypatch, bind_pool) -> None:
    now_ms = timestamp_ms()
    due = _job_def("due-1", now_ms - 1000)
    future = _job_def("future-1", now_ms + 60_000)
    fake = AsyncMock()
    fake.queued_jobs = AsyncMock(return_value=[due, future])
    bind_pool(fake)

    assert await tools.backend_reserved_tasks() == ["due-1"]
    scheduled = await tools.backend_scheduled_tasks()
    assert scheduled == {"future-1": float(now_ms + 60_000)}


async def test_list_failed_tasks_rows_carry_stored_failure_detail(bind_pool) -> None:
    """Each failed row names the task and its stored failure detail: the
    revived failure's stored repr, a revived abort's ``CancelledError``
    message, or the repr of whatever else was stored."""
    fake = AsyncMock()
    fake.all_job_results = AsyncMock(
        return_value=[
            _job_result("ok-1", True),
            _job_result("bad-1", False, result=TaskFailedError("ValueError", "ValueError('boom')", None)),
            _job_result("bad-2", False, result=asyncio.CancelledError("CancelledError()")),
            _job_result("bad-3", False, result="unable to serialize result"),
        ]
    )
    bind_pool(fake)

    assert await tools.backend_list_failed_tasks() == [
        {"task_id": "bad-1", "error": "ValueError('boom')"},
        {"task_id": "bad-2", "error": "CancelledError()"},
        {"task_id": "bad-3", "error": "'unable to serialize result'"},
    ]


async def test_cancel_task_confirmed(monkeypatch, bind_pool) -> None:
    """``Job.abort`` returning ``True`` is arq's confirmed-abort verdict: a
    replayed CancelledError outcome, revived by this backend's deserializer
    from the tagged description a stored abort serializes to."""
    bind_pool(object())
    jobs = _StatusJob({"j1": JobStatus.in_progress})
    monkeypatch.setattr(tools, "Job", jobs)

    assert await tools.backend_cancel_task("j1") == "Task j1 aborted"
    # Bounded by the configured task timeout — never an infinite wait.
    assert jobs.abort_calls == [300]


async def test_cancel_task_unconfirmed_reported(monkeypatch, bind_pool) -> None:
    """An abort call reporting ``False`` while the task still reads as live has
    no finished outcome to report: the request stays recorded, reported as
    requested-but-unconfirmed."""
    bind_pool(object())
    jobs = _StatusJob({"j1": JobStatus.queued, "abort:j1": False})
    monkeypatch.setattr(tools, "Job", jobs)

    assert await tools.backend_cancel_task("j1") == "Task j1 abort requested but not confirmed"


@pytest.mark.parametrize("status", [JobStatus.not_found, JobStatus.complete])
async def test_cancel_task_not_cancellable(monkeypatch, bind_pool, status) -> None:
    bind_pool(object())
    monkeypatch.setattr(tools, "Job", _StatusJob({"j1": status}))

    out = await tools.backend_cancel_task("j1")
    assert out == f"Task j1 cannot be canceled (status: {status})"


async def test_cancel_task_wait_elapsed_reports_unconfirmed(monkeypatch, bind_pool) -> None:
    """arq's ``Job.abort`` raises TimeoutError when the confirmation wait
    elapses; the tool reports requested-but-unconfirmed instead of raising --
    the request stays recorded and the worker honors it at pick-up."""
    bind_pool(object())
    jobs = _StatusJob({"j1": JobStatus.in_progress, "abort:j1": TimeoutError()})
    monkeypatch.setattr(tools, "Job", jobs)

    assert await tools.backend_cancel_task("j1") == "Task j1 abort requested but not confirmed"


async def test_cancel_task_revived_failure_replay_reports_failed_on_its_own(monkeypatch, bind_pool) -> None:
    """``Job.abort`` polls the result key while it waits, so a stored FAILURE
    landing mid-wait replays out of the poll as its revived exception. A
    revived ``TaskFailedError`` outcome is the task's own failure — a worker
    abort stores a ``CancelledError``, which replays as a confirmed abort
    instead — so it reports as failed-on-its-own with the stored detail, never
    as "cannot be canceled" and never as a cancel failure."""
    stored = TaskFailedError("ValueError", "ValueError('boom')", None)
    bind_pool(object())
    jobs = _StatusJob(
        {
            "j1": [JobStatus.in_progress, JobStatus.complete],
            "abort:j1": stored,
            "result:j1": _job_result("j1", success=False, result=stored),
        }
    )
    monkeypatch.setattr(tools, "Job", jobs)

    out = await tools.backend_cancel_task("j1")
    assert out == "Task j1 failed on its own before the abort could take effect: ValueError('boom')"


@pytest.mark.parametrize(
    ("replay", "stored", "detail"),
    [
        # arq's last-ditch placeholder: stored when even the tagged failure
        # description could not serialize — no revivable detail, so abort and
        # own-failure stay indistinguishable.
        (
            SerializationError("unable to serialize result"),
            "unable to serialize result",
            "'unable to serialize result'",
        ),
        # A stored failure carried whole as an exception the deserializer did
        # not revive itself.
        (ValueError("boom"), ValueError("boom"), "ValueError('boom')"),
    ],
)
async def test_cancel_task_undetailed_failure_replay_reports_aborted_or_failed(
    monkeypatch, bind_pool, replay, stored, detail
) -> None:
    """A stored failure without the revived ``TaskFailedError`` shape leaves
    the worker's abort and the task's own failure indistinguishable -- reported
    as aborted-or-failed with the raw stored value appended."""
    bind_pool(object())
    jobs = _StatusJob(
        {
            "j1": [JobStatus.in_progress, JobStatus.complete],
            "abort:j1": replay,
            "result:j1": _job_result("j1", success=False, result=stored),
        }
    )
    monkeypatch.setattr(tools, "Job", jobs)

    out = await tools.backend_cancel_task("j1")
    assert out == f"Task j1 finished in failure after the abort request (aborted or failed on its own): {detail}"


async def test_cancel_task_exception_replay_without_stored_failure_reports_not_cancelable(
    monkeypatch, bind_pool
) -> None:
    """An exception replay whose stored outcome does not read back as a failure
    (no retained result by the time it is re-read) has no abort to report:
    the task finished, nothing to cancel."""
    bind_pool(object())
    jobs = _StatusJob(
        {
            "j1": [JobStatus.in_progress, JobStatus.complete],
            "abort:j1": SerializationError("unable to serialize result"),
        }
    )
    monkeypatch.setattr(tools, "Job", jobs)

    out = await tools.backend_cancel_task("j1")
    assert out == f"Task j1 cannot be canceled (status: {JobStatus.complete})"


async def test_cancel_task_job_vanishing_during_wait_reports_not_cancelable(monkeypatch, bind_pool) -> None:
    """A task whose stored outcome replays but then vanishes before the status
    re-check (result retention lapsing) is equally finished: gone, nothing to
    cancel."""
    bind_pool(object())
    jobs = _StatusJob(
        {
            "j1": [JobStatus.in_progress, JobStatus.not_found],
            "abort:j1": SerializationError("unable to serialize result"),
        }
    )
    monkeypatch.setattr(tools, "Job", jobs)

    out = await tools.backend_cancel_task("j1")
    assert out == f"Task j1 cannot be canceled (status: {JobStatus.not_found})"


async def test_cancel_task_own_cancellation_propagates_never_reads_as_aborted(monkeypatch, bind_pool) -> None:
    """arq's ``Job.abort`` catches a CancelledError raised while it polls the
    result key and returns ``True`` -- including a cancellation of the task
    CALLING it. A cancelled cancel call must propagate its cancellation, never
    swallow it into a false "aborted" success report."""
    bind_pool(object())
    entered = asyncio.Event()

    class _SwallowingJob:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def status(self) -> JobStatus:
            return JobStatus.in_progress

        async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Exactly what arq's abort() does with the poll's CancelledError.
                return True
            raise AssertionError("unreachable")

    monkeypatch.setattr(tools, "Job", _SwallowingJob)
    task = asyncio.get_running_loop().create_task(tools.backend_cancel_task("j1"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("final_status", [JobStatus.complete, JobStatus.not_found])
async def test_cancel_task_success_replay_reports_not_cancelable(monkeypatch, bind_pool, final_status) -> None:
    """A task SUCCEEDING (or vanishing without a retained result) mid-wait
    makes ``Job.abort`` return a plain ``False`` instead of raising -- the
    other replay shape. The task finished, so this reads as "completed,
    nothing to cancel" -- never as a still-pending abort request."""
    bind_pool(object())
    jobs = _StatusJob({"j1": [JobStatus.in_progress, final_status], "abort:j1": False})
    monkeypatch.setattr(tools, "Job", jobs)

    out = await tools.backend_cancel_task("j1")
    assert out == f"Task j1 cannot be canceled (status: {final_status})"


async def test_cancel_task_real_abort_error_propagates(monkeypatch, bind_pool) -> None:
    bind_pool(object())
    jobs = _StatusJob({"j1": JobStatus.in_progress, "abort:j1": ConnectionError("redis gone")})
    monkeypatch.setattr(tools, "Job", jobs)

    with pytest.raises(ConnectionError, match="redis gone"):
        await tools.backend_cancel_task("j1")


# -- schedule CRUD tools ---------------------------------------------------------------


def _seed_schedule(redis: Any, name: str = "s1", enabled: bool = True) -> str:
    key = f"arq:schedule:{name}"
    redis._store[key] = {
        b"target": b"tool_execution",
        b"args": orjson.dumps([]),
        b"kwargs": orjson.dumps({"backend_tool_name": "t"}),
        b"schedule": orjson.dumps({"__type__": "interval", "every": 60.0, "relative": False}),
        b"cron_or_interval": b"60",
        b"enabled": b"true" if enabled else b"false",
        b"job_id": b"pending-1",
        b"last_scheduled_ts": b"12345.0",
        b"aborted": b"",
    }
    return key


class _OkJob:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def status(self) -> JobStatus:
        return JobStatus.deferred

    async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
        return True


async def test_list_schedules(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    _seed_schedule(fake_redis)

    out = await tools.backend_list_schedules()
    assert out == [
        {
            "name": "s1",
            "enabled": True,
            "next_run_at_ts": 12345.0,
            "next_run_at_iso": datetime.fromtimestamp(12345.0, tz=UTC).isoformat(),
            "schedule": {"__type__": "interval", "every": 60.0, "relative": False},
            "target": "tool_execution",
            "args": [],
            "kwargs": {"backend_tool_name": "t"},
        }
    ]


async def test_list_schedules_without_recorded_next_run(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    key = _seed_schedule(fake_redis)
    del fake_redis._store[key][b"last_scheduled_ts"]

    (row,) = await tools.backend_list_schedules()
    assert row["next_run_at_ts"] is None
    assert row["next_run_at_iso"] is None


async def test_get_schedule_and_exists(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    _seed_schedule(fake_redis)

    assert await tools.backend_schedule_exists("s1") is True
    assert await tools.backend_schedule_exists("nope") is False
    out = await tools.backend_get_schedule("s1")
    assert out["enabled"] is True
    assert out["target"] == "tool_execution"
    assert await tools.backend_get_schedule("nope") == {"status": "not_found"}


async def test_enable_disable_schedule(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    key = _seed_schedule(fake_redis, enabled=False)

    assert await tools.backend_enable_schedule("s1") == {"status": "enabled"}
    assert fake_redis._store[key][b"enabled"] == b"true"
    assert await tools.backend_disable_schedule("s1") == {"status": "disabled"}
    assert fake_redis._store[key][b"enabled"] == b"false"
    assert await tools.backend_enable_schedule("nope") == {"status": "not_found"}
    assert await tools.backend_disable_schedule("nope") == {"status": "not_found"}


@pytest.mark.parametrize("flip", [tools.backend_enable_schedule, tools.backend_disable_schedule])
async def test_enable_disable_deleted_concurrently_not_resurrected(fake_redis, bind_pool, flip) -> None:
    """A delete landing before the flag write acquires the per-schedule lock
    wins: the flag write reports not_found and must not recreate the schedule
    as a partial enabled-only hash (unrunnable, and one such hash fails the
    whole export)."""
    key = _seed_schedule(fake_redis)
    bind_pool(DeleteOnLockRedis(fake_redis, key))

    assert await flip("s1") == {"status": "not_found"}
    assert key not in fake_redis._store


async def test_delete_schedule_aborts_and_deletes(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "Job", _OkJob)
    key = _seed_schedule(fake_redis)

    assert await tools.backend_delete_schedule("s1") == {"status": "deleted", "name": "s1"}
    assert key not in fake_redis._store
    assert await tools.backend_delete_schedule("s1") == {"status": "not_found", "name": "s1"}


async def test_delete_schedule_failed_abort_propagates(fake_redis, monkeypatch, bind_pool) -> None:
    class _BadAbort(_OkJob):
        async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
            raise RuntimeError("abort failed")

    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "Job", _BadAbort)
    key = _seed_schedule(fake_redis)

    with pytest.raises(RuntimeError, match="abort failed"):
        await tools.backend_delete_schedule("s1")
    # The schedule was NOT deleted behind the failed abort.
    assert key in fake_redis._store


async def test_delete_schedule_deleted_concurrently_reports_not_found(fake_redis, monkeypatch, bind_pool) -> None:
    """A delete that loses the race for the per-schedule lock to another delete
    finds the hash gone: it reports not_found without touching any job."""
    monkeypatch.setattr(scheduler, "Job", _StatusJob({"abort:pending-1": RuntimeError("must not be called")}))
    key = _seed_schedule(fake_redis)
    bind_pool(DeleteOnLockRedis(fake_redis, key))

    assert await tools.backend_delete_schedule("s1") == {"status": "not_found", "name": "s1"}
    assert key not in fake_redis._store


async def test_get_schedule_and_run_now_skip_schedule_deleted_mid_read(fake_redis, bind_pool) -> None:
    """A hash that reads back empty was deleted concurrently (an existing Redis
    hash is never empty): get must not fabricate a row and run-now must not
    enqueue an empty-target job."""
    bind_pool(fake_redis)
    fake_redis._store["arq:schedule:vanished"] = {}

    assert await tools.backend_get_schedule("vanished") == {"status": "not_found"}
    assert await tools.backend_run_schedule_now("vanished") == {"status": "not_found"}
    assert fake_redis.enqueued == []


async def test_run_schedule_now_enqueues_target(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    _seed_schedule(fake_redis)

    assert await tools.backend_run_schedule_now("s1") == {"status": "queued"}
    assert fake_redis.enqueued == [("tool_execution",)]
    assert await tools.backend_run_schedule_now("nope") == {"status": "not_found"}


async def test_update_schedule_new_schedule(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "Job", _OkJob)
    key = _seed_schedule(fake_redis)

    out = await tools.backend_update_schedule("s1", new_schedule=120)

    assert out["status"] == "updated"
    assert out["previous_schedule"] == {"__type__": "interval", "every": 60.0, "relative": False}
    assert out["new_schedule"] == {"__type__": "interval", "every": 120.0, "relative": False}
    # The next-run timestamp is reported even when derived from the schedule.
    assert out["next_run_at_ts"] == pytest.approx(datetime.now(UTC).timestamp() + 120, abs=5)
    assert out["next_run_at_iso"]
    assert fake_redis._store[key][b"cron_or_interval"] == b"120"


async def test_update_schedule_next_run_in_ms(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "Job", _OkJob)
    _seed_schedule(fake_redis)

    out = await tools.backend_update_schedule("s1", next_run_in_ms=5000)

    assert out["status"] == "updated"
    assert out["next_run_at_ts"] == pytest.approx(datetime.now(UTC).timestamp() + 5, abs=5)


async def test_update_schedule_crontab_next_run(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "Job", _OkJob)
    key = _seed_schedule(fake_redis)

    out = await tools.backend_update_schedule("s1", new_schedule="0 9 * * 1")

    assert out["status"] == "updated"
    assert fake_redis._store[key][b"cron_or_interval"] == b"0 9 * * 1"
    assert out["next_run_at_ts"] > datetime.now(UTC).timestamp()


async def test_update_schedule_deleted_concurrently_reports_not_found(fake_redis, monkeypatch, bind_pool) -> None:
    """A delete landing between the update's exists-check and its transition
    wins: the update reports not_found and must not resurrect the schedule as
    a partial hash."""
    key = _seed_schedule(fake_redis)
    bind_pool(DeleteOnLockRedis(fake_redis, key))
    monkeypatch.setattr(scheduler, "Job", _OkJob)

    out = await tools.backend_update_schedule("s1", new_schedule=120)

    assert out["status"] == "not_found"
    assert "deleted concurrently" in out["message"]
    assert key not in fake_redis._store
    assert fake_redis.enqueued == []


async def test_update_schedule_skipped_and_not_found(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    _seed_schedule(fake_redis)

    out = await tools.backend_update_schedule("s1")
    assert out["status"] == "skipped"

    out = await tools.backend_update_schedule("nope", new_schedule=60)
    assert out["status"] == "not_found"
