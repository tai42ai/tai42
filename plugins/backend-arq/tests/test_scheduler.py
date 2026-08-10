"""Scheduler behavior: transitions, abort semantics, the self-rescheduling
``task_scheduler``, and the startup watchdog."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import orjson
import pytest
from arq.jobs import JobStatus

from tai42_backend_arq import scheduler
from tai42_backend_arq.settings import TaskFailedError
from tests.conftest import DeleteOnLockRedis


def _job_type(**results: Any) -> type:
    """A fake ``Job`` class whose per-instance behavior is keyed by job id.

    A list status value is consumed one entry per call; the last entry repeats.
    """

    class _Job:
        def __init__(self, job_id: str, *args: Any, **kwargs: Any) -> None:
            self.job_id = job_id

        async def status(self) -> JobStatus:
            value = results.get(f"status:{self.job_id}", JobStatus.deferred)
            if isinstance(value, list):
                value = value.pop(0) if len(value) > 1 else value[0]
            if isinstance(value, Exception):
                raise value
            return value

        async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
            value = results.get(f"abort:{self.job_id}", True)
            if isinstance(value, Exception):
                raise value
            return value

    return _Job


def _seed(redis: Any, name: str, **overrides: bytes) -> str:
    key = f"arq:schedule:{name}"
    mapping: dict[bytes, bytes] = {
        b"target": b"tool_execution",
        b"args": orjson.dumps(["a"]),
        b"kwargs": orjson.dumps({"backend_tool_name": "t"}),
        b"schedule": orjson.dumps({"__type__": "interval", "every": 60.0, "relative": False}),
        b"cron_or_interval": b"60",
        b"enabled": b"true",
        b"job_id": b"pending-1",
        b"last_scheduled_ts": str(datetime.now(UTC).timestamp()).encode(),
        b"aborted": b"",
    }
    mapping.update({field.encode(): value for field, value in overrides.items()})
    redis._store[key] = mapping
    return key


# -- wait_job_result ---------------------------------------------------------------


async def test_wait_job_result_returns_result() -> None:
    class _Job:
        async def result(self, timeout: float | None = None) -> Any:
            return {"ok": True}

    assert await scheduler.wait_job_result(_Job()) == {"ok": True}  # type: ignore[arg-type]


async def test_wait_job_result_converts_replayed_abort_to_failure() -> None:
    """A stored abort replays out of ``Job.result`` as its revived
    ``CancelledError``; it re-raises as ``TaskFailedError`` carrying the stored
    detail so it can never read as a cancellation of the calling task."""

    class _Job:
        async def result(self, timeout: float | None = None) -> Any:
            raise asyncio.CancelledError("CancelledError()")

    with pytest.raises(TaskFailedError, match=r"CancelledError\(\)") as excinfo:
        await scheduler.wait_job_result(_Job())  # type: ignore[arg-type]
    assert excinfo.value.error_type == "CancelledError"
    assert isinstance(excinfo.value.__cause__, asyncio.CancelledError)


async def test_wait_job_result_reraises_own_cancellation() -> None:
    """A cancellation of the task CALLING ``wait_job_result`` surfaces from the
    same ``await`` as a replayed stored abort. It must propagate as the
    cancellation it is, never convert into a ``TaskFailedError``."""
    entered = asyncio.Event()

    class _HangingJob:
        async def result(self, timeout: float | None = None) -> Any:
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.get_running_loop().create_task(scheduler.wait_job_result(_HangingJob()))  # type: ignore[arg-type]
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# -- request_job_abort ---------------------------------------------------------------


async def test_abort_job_reraises_own_cancellation() -> None:
    """arq's ``Job.abort`` catches a CancelledError raised while it polls the
    result key and returns ``True`` -- including a cancellation of the task
    CALLING it. ``abort_job`` re-raises that swallowed cancellation instead of
    handing back a false confirmed-abort verdict."""
    entered = asyncio.Event()

    class _SwallowingJob:
        async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Exactly what arq's abort() does with the poll's CancelledError.
                return True
            raise AssertionError("unreachable")

    task = asyncio.get_running_loop().create_task(scheduler.abort_job(_SwallowingJob(), timeout=0))  # type: ignore[arg-type]
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_request_job_abort_confirmed() -> None:
    job = _job_type()("j1")
    assert await scheduler.request_job_abort(job) is True


async def test_request_job_abort_pending_confirmation_returns_false(caplog) -> None:
    job = _job_type(**{"abort:j1": TimeoutError()})("j1")
    with caplog.at_level("INFO", logger="tai42_backend_arq.scheduler"):
        assert await scheduler.request_job_abort(job) is False
    assert any("abort of job j1 requested" in record.getMessage() for record in caplog.records)


async def test_request_job_abort_real_error_propagates() -> None:
    job = _job_type(**{"abort:j1": ConnectionError("redis gone")})("j1")
    with pytest.raises(ConnectionError, match="redis gone"):
        await scheduler.request_job_abort(job)


@pytest.mark.parametrize("status", [JobStatus.complete, JobStatus.not_found])
async def test_request_job_abort_finished_or_missing_job_needs_no_abort(status) -> None:
    """A finished or missing job is guaranteed not to run, so no abort request
    is issued -- ``Job.abort`` on a finished job would replay its stored
    outcome, re-raising a failed job's own exception as an abort failure."""
    job = _job_type(**{"status:j1": status, "abort:j1": RuntimeError("stored outcome must not be replayed")})("j1")
    assert await scheduler.request_job_abort(job) is True


@pytest.mark.parametrize("final_status", [JobStatus.complete, JobStatus.not_found])
async def test_request_job_abort_job_finishing_during_request_reads_as_done(final_status) -> None:
    """``Job.abort`` polls the result key while it waits, so a job finishing on
    its own mid-request replays its stored outcome out of the abort call. The
    job is finished -- guaranteed not to run -- so that replay must translate to
    ``True``, never surface as an abort failure."""
    job = _job_type(
        **{
            "status:j1": [JobStatus.in_progress, final_status],
            "abort:j1": ValueError("the job's own stored failure"),
        }
    )("j1")
    assert await scheduler.request_job_abort(job) is True


@pytest.mark.parametrize("final_status", [JobStatus.complete, JobStatus.not_found])
async def test_request_job_abort_success_replay_reads_as_done(final_status) -> None:
    """A job SUCCEEDING (or vanishing without a retained result) while the
    abort request is in flight makes ``Job.abort`` return a plain ``False``
    instead of raising -- the other replay shape. The job is equally finished
    and guaranteed not to run, so this must also translate to ``True``, never
    read as a still-pending abort request."""
    job = _job_type(**{"status:j1": [JobStatus.in_progress, final_status], "abort:j1": False})("j1")
    assert await scheduler.request_job_abort(job) is True


async def test_request_job_abort_unconfirmed_false_with_live_job_stays_false(caplog) -> None:
    """An abort call reporting ``False`` while the job still reads as live has
    no finished outcome to translate: the request is recorded but unconfirmed,
    reported exactly like the elapsed-wait path."""
    job = _job_type(**{"status:j1": JobStatus.in_progress, "abort:j1": False})("j1")
    with caplog.at_level("INFO", logger="tai42_backend_arq.scheduler"):
        assert await scheduler.request_job_abort(job) is False
    assert any("abort of job j1 requested" in record.getMessage() for record in caplog.records)


# -- safe_schedule_transition -----------------------------------------------------------


async def test_transition_replaces_pending_job_and_writes_mapping(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type())
    key = _seed(fake_redis, "s1")

    job = await scheduler.safe_schedule_transition(
        fake_redis, "s1", defer_by=60, last_scheduled_ts=123.0, mapping_updates={"enabled": "false"}
    )

    stored = fake_redis._store[key]
    assert stored[b"job_id"] == job.job_id.encode()
    assert stored[b"last_scheduled_ts"] == b"123.0"
    assert stored[b"enabled"] == b"false"
    # The replacement went through the scheduler function.
    assert fake_redis.enqueued[0][0] == "task_scheduler"


async def test_transition_skipped_when_preempted(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type())
    key = _seed(fake_redis, "s1", job_id=b"pending-1")

    result = await scheduler.safe_schedule_transition(
        fake_redis, "s1", defer_by=60, last_scheduled_ts=123.0, enforce_job_id="someone-else"
    )

    assert result is None
    assert fake_redis.enqueued == []
    assert fake_redis._store[key][b"job_id"] == b"pending-1"


async def test_transition_over_finished_pending_job_proceeds(fake_redis, monkeypatch) -> None:
    """Replacing a schedule whose recorded pending job already finished (e.g.
    it failed without rescheduling) must proceed: there is nothing to abort,
    and the old job's stored failure must not resurface as a transition error."""
    monkeypatch.setattr(
        scheduler,
        "Job",
        _job_type(
            **{
                "status:pending-1": JobStatus.complete,
                "abort:pending-1": RuntimeError("stored outcome must not be replayed"),
            }
        ),
    )
    key = _seed(fake_redis, "s1")

    job = await scheduler.safe_schedule_transition(fake_redis, "s1", defer_by=60, last_scheduled_ts=123.0)

    assert fake_redis._store[key][b"job_id"] == job.job_id.encode()
    assert fake_redis.enqueued[0][0] == "task_scheduler"


async def test_transition_never_resurrects_deleted_schedule(fake_redis, monkeypatch) -> None:
    """A transition whose mapping is not a full schedule definition (the
    update/recovery paths) that finds the hash gone lost a race with a delete:
    it must skip -- writing anyway would recreate the schedule as a partial
    hash with no target or schedule, unrunnable and unexportable."""
    monkeypatch.setattr(scheduler, "Job", _job_type())

    result = await scheduler.safe_schedule_transition(
        fake_redis, "gone", defer_by=60, last_scheduled_ts=123.0, mapping_updates={"enabled": "true"}
    )

    assert result is None
    assert fake_redis.enqueued == []
    assert "arq:schedule:gone" not in fake_redis._store


async def test_transition_with_full_definition_creates_missing_schedule(fake_redis, monkeypatch) -> None:
    """The create/import paths carry the full durable definition, so they may
    (and must) write a hash that does not exist yet."""
    monkeypatch.setattr(scheduler, "Job", _job_type())
    mapping = {
        "target": "tool_execution",
        "args": orjson.dumps([]),
        "kwargs": orjson.dumps({"backend_tool_name": "t"}),
        "schedule": orjson.dumps({"__type__": "interval", "every": 60.0, "relative": False}),
        "cron_or_interval": "60",
        "enabled": "true",
    }

    job = await scheduler.safe_schedule_transition(
        fake_redis, "fresh", defer_by=60, last_scheduled_ts=123.0, mapping_updates=mapping
    )

    assert job is not None
    assert fake_redis._store["arq:schedule:fresh"][b"target"] == b"tool_execution"
    assert fake_redis.enqueued[0][0] == "task_scheduler"


async def test_transition_abort_failure_stops_before_enqueue(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type(**{"abort:pending-1": RuntimeError("abort failed")}))
    key = _seed(fake_redis, "s1")

    with pytest.raises(RuntimeError, match="abort failed"):
        await scheduler.safe_schedule_transition(fake_redis, "s1", defer_by=60, last_scheduled_ts=123.0)

    assert fake_redis.enqueued == []
    assert fake_redis._store[key][b"job_id"] == b"pending-1"


# -- abort_schedule_task ---------------------------------------------------------------


async def test_abort_schedule_task_marks_and_requests_abort(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "Job", _job_type())
    key = _seed(fake_redis, "s1")

    await scheduler.abort_schedule_task(key)

    assert fake_redis._store[key][b"aborted"] == b"pending-1"


async def test_abort_schedule_task_noop_without_job_or_key(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "Job", _job_type(**{"abort:pending-1": RuntimeError("must not be called")}))

    # Missing key: nothing happens.
    await scheduler.abort_schedule_task("arq:schedule:absent")

    # Present key without a pending job id: nothing to abort.
    key = _seed(fake_redis, "s1", job_id=b"")
    await scheduler.abort_schedule_task(key)
    assert fake_redis._store[key][b"aborted"] == b""


async def test_abort_schedule_task_error_propagates(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "Job", _job_type(**{"abort:pending-1": ConnectionError("redis gone")}))
    key = _seed(fake_redis, "s1")

    with pytest.raises(ConnectionError, match="redis gone"):
        await scheduler.abort_schedule_task(key)


# -- task_scheduler ---------------------------------------------------------------------


class _EnqueueRecordingRedis:
    """Extends the fake with target-job results so ``task_scheduler`` can await
    the enqueued target's result."""

    def __init__(self, base: Any, result: Any = "ran") -> None:
        self._base = base
        self._result = result
        self.targets: list[tuple[Any, ...]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any:
        if function == "task_scheduler":
            return await self._base.enqueue_job(function, *args, **kwargs)
        self.targets.append((function, args, kwargs))
        result = self._result

        class _TargetJob:
            job_id = "target-1"

            async def result(self, timeout: float | None = None) -> Any:
                if isinstance(result, BaseException):
                    raise result
                return result

        return _TargetJob()


async def test_task_scheduler_runs_target_and_reschedules(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type())
    key = _seed(fake_redis, "s1")
    redis = _EnqueueRecordingRedis(fake_redis)

    out = await scheduler.task_scheduler({"redis": redis, "job_id": "pending-1"}, "s1")

    assert out == "ran"
    assert redis.targets == [("tool_execution", ("a",), {"backend_tool_name": "t"})]
    # Rescheduled: the hash now points at the replacement scheduler job.
    assert fake_redis._store[key][b"job_id"] != b"pending-1"
    assert float(fake_redis._store[key][b"last_scheduled_ts"]) > datetime.now(UTC).timestamp() - 5


async def test_task_scheduler_aborted_target_records_failure_and_reschedules(fake_redis, monkeypatch) -> None:
    """A target aborted mid-run replays its stored abort as a revived
    ``CancelledError``. It surfaces as ``TaskFailedError`` (a raw
    CancelledError would read to the worker as a cancellation of the scheduler
    job and trigger a retry) and the schedule still reschedules itself."""
    monkeypatch.setattr(scheduler, "Job", _job_type())
    key = _seed(fake_redis, "s1")
    redis = _EnqueueRecordingRedis(fake_redis, result=asyncio.CancelledError("CancelledError()"))

    with pytest.raises(TaskFailedError, match=r"CancelledError\(\)"):
        await scheduler.task_scheduler({"redis": redis, "job_id": "pending-1"}, "s1")

    assert fake_redis._store[key][b"job_id"] != b"pending-1"


async def test_task_scheduler_disabled_skips_target_but_reschedules(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type())
    key = _seed(fake_redis, "s1", enabled=b"false")
    redis = _EnqueueRecordingRedis(fake_redis)

    out = await scheduler.task_scheduler({"redis": redis, "job_id": "pending-1"}, "s1")

    assert out is None
    assert redis.targets == []
    assert fake_redis._store[key][b"job_id"] != b"pending-1"


async def test_task_scheduler_missing_key_fast_exits(fake_redis) -> None:
    out = await scheduler.task_scheduler({"redis": fake_redis, "job_id": "j"}, "absent")
    assert out is None
    assert fake_redis.enqueued == []


async def test_task_scheduler_stale_job_skips_without_duplicate_run(fake_redis, monkeypatch) -> None:
    """A task_scheduler job that is no longer the schedule's recorded pending
    job (an arq retry after its own transition, or a replaced job whose abort
    was never processed) must not fire the target again."""
    monkeypatch.setattr(scheduler, "Job", _job_type())
    key = _seed(fake_redis, "s1", job_id=b"replacement-2")
    redis = _EnqueueRecordingRedis(fake_redis)

    out = await scheduler.task_scheduler({"redis": redis, "job_id": "pending-1"}, "s1")

    assert out is None
    assert redis.targets == []
    assert fake_redis.enqueued == []
    assert fake_redis._store[key][b"job_id"] == b"replacement-2"


async def test_task_scheduler_aborted_marker_stops_run(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type())
    _seed(fake_redis, "s1", aborted=b"pending-1")
    redis = _EnqueueRecordingRedis(fake_redis)

    out = await scheduler.task_scheduler({"redis": redis, "job_id": "pending-1"}, "s1")

    assert out is None
    assert redis.targets == []
    assert fake_redis.enqueued == []


async def test_task_scheduler_crontab_realigns_past_slot(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type())
    key = _seed(
        fake_redis,
        "s1",
        cron_or_interval=b"0 9 * * 1",
        last_scheduled_ts=b"1000.0",  # far in the past: slot after it already elapsed
    )
    redis = _EnqueueRecordingRedis(fake_redis)

    await scheduler.task_scheduler({"redis": redis, "job_id": "pending-1"}, "s1")

    assert float(fake_redis._store[key][b"last_scheduled_ts"]) > datetime.now(UTC).timestamp()


async def test_task_scheduler_interval_realigns_when_far_behind(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type())
    behind = datetime.now(UTC).timestamp() - 3600
    key = _seed(fake_redis, "s1", last_scheduled_ts=str(behind).encode())
    redis = _EnqueueRecordingRedis(fake_redis)

    await scheduler.task_scheduler({"redis": redis, "job_id": "pending-1"}, "s1")

    # Realigned to now + 60, not the stale base + 60.
    assert float(fake_redis._store[key][b"last_scheduled_ts"]) > datetime.now(UTC).timestamp() + 30


async def test_task_scheduler_bad_last_scheduled_ts_treated_as_unset(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type())
    key = _seed(fake_redis, "s1", last_scheduled_ts=b"not-a-number")
    redis = _EnqueueRecordingRedis(fake_redis)

    await scheduler.task_scheduler({"redis": redis, "job_id": "pending-1"}, "s1")

    assert float(fake_redis._store[key][b"last_scheduled_ts"]) > datetime.now(UTC).timestamp() + 30


# -- recover_stalled_schedules --------------------------------------------------------------


async def test_watchdog_restarts_lost_and_completed_jobs(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(
        scheduler,
        "Job",
        _job_type(
            **{
                "status:lost": JobStatus.not_found,
                "status:done": JobStatus.complete,
                "status:live": JobStatus.deferred,
            }
        ),
    )
    lost = _seed(fake_redis, "lost", job_id=b"lost")
    done = _seed(fake_redis, "done", job_id=b"done")
    live = _seed(fake_redis, "live", job_id=b"live")
    disabled = _seed(fake_redis, "disabled", enabled=b"false", job_id=b"lost")
    empty = _seed(fake_redis, "empty", job_id=b"")

    await scheduler.recover_stalled_schedules({"redis": fake_redis})

    assert fake_redis._store[lost][b"job_id"] != b"lost"
    assert fake_redis._store[done][b"job_id"] != b"done"
    assert fake_redis._store[live][b"job_id"] == b"live"
    assert fake_redis._store[disabled][b"job_id"] == b"lost"
    assert fake_redis._store[empty][b"job_id"] != b""


async def test_watchdog_recovery_lock_prevents_double_restart(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "Job", _job_type(**{"status:lost": JobStatus.not_found}))
    key = _seed(fake_redis, "lost", job_id=b"lost")

    await scheduler.recover_stalled_schedules({"redis": fake_redis})
    restarted_job = fake_redis._store[key][b"job_id"]

    # The nx recovery lock is still held: a second pass must not restart again.
    await scheduler.recover_stalled_schedules({"redis": fake_redis})
    assert fake_redis._store[key][b"job_id"] == restarted_job


async def test_watchdog_does_not_resurrect_schedule_deleted_mid_recovery(fake_redis, monkeypatch) -> None:
    """A schedule deleted between the watchdog's stall check and its transition
    must stay deleted -- restarting it would recreate a partial hash."""
    monkeypatch.setattr(scheduler, "Job", _job_type(**{"status:lost": JobStatus.not_found}))
    key = _seed(fake_redis, "lost", job_id=b"lost")
    redis = DeleteOnLockRedis(fake_redis, key)

    await scheduler.recover_stalled_schedules({"redis": redis})

    assert key not in fake_redis._store
    assert fake_redis.enqueued == []


async def test_watchdog_error_on_one_schedule_does_not_stop_others(fake_redis, monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        scheduler,
        "Job",
        _job_type(**{"status:boom": ConnectionError("probe failed"), "status:lost": JobStatus.not_found}),
    )
    broken = _seed(fake_redis, "a_broken", job_id=b"boom")
    lost = _seed(fake_redis, "z_lost", job_id=b"lost")

    with caplog.at_level("ERROR", logger="tai42_backend_arq.scheduler"):
        await scheduler.recover_stalled_schedules({"redis": fake_redis})

    assert any("Watchdog error checking schedule" in record.message for record in caplog.records)
    assert fake_redis._store[broken][b"job_id"] == b"boom"
    assert fake_redis._store[lost][b"job_id"] != b"lost"
