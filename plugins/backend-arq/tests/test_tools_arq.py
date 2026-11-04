"""arq-native task tools (active/reserved/scheduled/failed/cancel) and the
schedule CRUD tools over the in-memory fake."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import orjson
import pytest
from arq.constants import abort_jobs_ss, in_progress_key_prefix, result_key_prefix
from arq.jobs import JobResult, JobStatus, ResultNotFound, SerializationError
from tai42_kit.backend.job_context import running_job
from tai42_kit.backend.tool_surface import UnsupportedBackendTool

from tai42_backend_arq import inspection, scheduler
from tai42_backend_arq.jobs import register_queue
from tai42_backend_arq.settings import TaskFailedError

from .conftest import SETTINGS_QUEUE, DeleteOnLockRedis, backend_tools, handle_of, submit_job

_REFUSAL = "^backend 'arq' does not support backend_cancel_task without terminate for a running task$"


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
    is consumed one entry per call (the last entry repeats); a ``verdict:``
    value is how the wait on the job's result ends — an exception is raised
    (by default the revived ``CancelledError`` of a stored abort), anything
    else is returned as the stored result; a ``result:`` value is the
    ``result_info`` outcome (default ``None`` — no retained result)."""

    def __init__(self, statuses: dict[str, Any]) -> None:
        self._statuses = statuses
        self.waits: list[float | None] = []

    def __call__(self, job_id: str, *args: Any, **kwargs: Any) -> Any:
        outer = self
        ident = job_id

        class _Job:
            job_id = ident

            async def status(self) -> JobStatus:
                value = outer._statuses[job_id]
                if isinstance(value, list):
                    value = value.pop(0) if len(value) > 1 else value[0]
                return value

            async def result(self, timeout: float | None = None, *, poll_delay: float = 0.5) -> Any:
                outer.waits.append(timeout)
                value = outer._statuses.get(f"verdict:{job_id}", asyncio.CancelledError())
                if isinstance(value, BaseException):
                    raise value
                return value

            async def result_info(self) -> JobResult | None:
                return outer._statuses.get(f"result:{job_id}")

        return _Job()


# -- task tools -------------------------------------------------------------------


async def test_active_tasks_lists_only_in_progress(arq_redis) -> None:
    """A running job on a derived queue is listed; a finished one whose in-progress key lingers is not."""
    running = await submit_job(queue="gpu")
    await arq_redis.set(in_progress_key_prefix + running.job_id, b"1")
    await arq_redis.set(in_progress_key_prefix + "finished", b"1")
    await arq_redis.set(result_key_prefix + "finished", b"1")

    assert await backend_tools().backend_active_tasks() == {running.job_id: {"status": "in_progress"}}


async def test_active_tasks_reads_each_job_in_one_round_trip(arq_redis, monkeypatch) -> None:
    await register_queue(arq_redis, f"{SETTINGS_QUEUE}.gpu")
    for job_id in ("job-a", "job-b", "job-c"):
        await arq_redis.set(in_progress_key_prefix + job_id, b"1")
    trips: list[str] = []
    command, pipeline = arq_redis.execute_command, arq_redis.pipeline

    async def _command(*args: Any, **kwargs: Any) -> Any:
        if args[0] != "SCAN":
            trips.append(args[0])
        return await command(*args, **kwargs)

    def _pipeline(*args: Any, **kwargs: Any) -> Any:
        trips.append("pipeline")
        return pipeline(*args, **kwargs)

    monkeypatch.setattr(arq_redis, "execute_command", _command)
    monkeypatch.setattr(arq_redis, "pipeline", _pipeline)

    assert len(await backend_tools().backend_active_tasks()) == 3
    assert trips == ["pipeline"] * 3


async def test_reserved_and_scheduled_tasks_span_every_known_queue(arq_redis) -> None:
    """Due ids are listed per queue, every known queue keyed (an empty one as ``[]``);
    future ids merge into one id-to-due-time map."""
    due_main = await submit_job()
    future_main = await submit_job(countdown=60)
    due_gpu = await submit_job(queue="gpu")
    future_gpu = await submit_job(queue="gpu", countdown=120)
    await register_queue(arq_redis, f"{SETTINGS_QUEUE}.idle")

    assert await backend_tools().backend_reserved_tasks() == {
        SETTINGS_QUEUE: [due_main.job_id],
        f"{SETTINGS_QUEUE}.gpu": [due_gpu.job_id],
        f"{SETTINGS_QUEUE}.idle": [],
    }
    scheduled = await backend_tools().backend_scheduled_tasks()
    assert set(scheduled) == {future_main.job_id, future_gpu.job_id}
    assert scheduled[future_gpu.job_id] == await arq_redis.zscore(f"{SETTINGS_QUEUE}.gpu", future_gpu.job_id)


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

    assert await backend_tools().backend_list_failed_tasks() == [
        {"task_id": "bad-1", "error": "ValueError('boom')"},
        {"task_id": "bad-2", "error": "CancelledError()"},
        {"task_id": "bad-3", "error": "'unable to serialize result'"},
    ]


def test_the_inspection_terminates() -> None:
    assert inspection.ArqInspection("arq").terminates is True


async def _marked(redis: Any, job_id: str) -> bool:
    return await redis.zscore(abort_jobs_ss, job_id) is not None


async def test_cancel_task_with_terminate_aborts_a_running_job(monkeypatch, arq_redis) -> None:
    """With ``terminate`` a running job is marked for abort; the revived ``CancelledError`` its
    result replays is arq's confirmed-abort verdict."""
    await arq_redis.set(in_progress_key_prefix + "j1", b"1")
    jobs = _StatusJob({"j1": JobStatus.in_progress})
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    assert await backend_tools().backend_cancel_task("j1", terminate=True) == "Task j1 aborted"
    assert await _marked(arq_redis, "j1")
    # Bounded by the configured task timeout — never an infinite wait.
    assert jobs.waits == [300]


@pytest.mark.parametrize("terminate", [False, True])
async def test_cancel_task_aborts_a_queued_job(monkeypatch, arq_redis, terminate) -> None:
    """A job no worker has claimed is marked with or without ``terminate``; the worker honors the
    mark at pick-up and never runs it."""
    job = await submit_job()
    monkeypatch.setattr(inspection, "job_handle", handle_of(_StatusJob({job.job_id: JobStatus.queued})))

    assert await backend_tools().backend_cancel_task(job.job_id, terminate=terminate) == f"Task {job.job_id} aborted"
    assert await _marked(arq_redis, job.job_id)


async def test_cancel_task_brings_a_deferred_job_due_on_its_own_queue(monkeypatch, arq_redis) -> None:
    """A deferred job is re-scored due on the queue holding it, so a worker picks it up and honors
    the mark now instead of at its due time."""
    job = await submit_job(queue="gpu", countdown=60)
    monkeypatch.setattr(inspection, "job_handle", handle_of(_StatusJob({job.job_id: JobStatus.deferred})))

    assert await backend_tools().backend_cancel_task(job.job_id) == f"Task {job.job_id} aborted"
    assert await arq_redis.zscore(f"{SETTINGS_QUEUE}.gpu", job.job_id) == 1
    assert await _marked(arq_redis, job.job_id)


async def test_cancel_task_without_terminate_refuses_a_running_job(arq_redis) -> None:
    job = await submit_job()
    await arq_redis.set(in_progress_key_prefix + job.job_id, b"1")

    with pytest.raises(UnsupportedBackendTool, match=_REFUSAL):
        await backend_tools().backend_cancel_task(job.job_id)
    assert not await _marked(arq_redis, job.job_id)


async def test_cancel_task_without_terminate_never_marks_a_job_a_worker_claims_meanwhile(
    monkeypatch, arq_redis
) -> None:
    """A worker claiming the job between the in-progress check and the mark writes the in-progress
    key the transaction watches, so the mark is never written: the retry sees the claim and refuses."""
    job = await submit_job()
    claim = in_progress_key_prefix + job.job_id
    claims: list[str] = []
    pipeline = arq_redis.pipeline

    def _pipeline(transaction: bool = True, shard_hint: Any = None) -> Any:
        pipe = pipeline(transaction, shard_hint)
        zscore = pipe.zscore

        async def _claim_then_zscore(*args: Any) -> Any:
            claims.append(claim)
            await arq_redis.psetex(claim, 60_000, b"1")
            return await zscore(*args)

        def _zscore(*args: Any) -> Any:
            # Only a read made while watching runs at once; a buffered one is queued as usual.
            if pipe.watching and not pipe.explicit_transaction and not claims:
                return _claim_then_zscore(*args)
            return zscore(*args)

        pipe.zscore = _zscore
        return pipe

    monkeypatch.setattr(arq_redis, "pipeline", _pipeline)

    with pytest.raises(UnsupportedBackendTool, match=_REFUSAL):
        await backend_tools().backend_cancel_task(job.job_id)
    assert claims == [claim]
    assert not await _marked(arq_redis, job.job_id)


async def test_cancel_task_unconfirmed_reported(monkeypatch, arq_redis) -> None:
    """A wait ending without a stored outcome while the task still reads as live has no finished
    outcome to report: the request stays recorded, reported as requested-but-unconfirmed with the
    status the task reached."""
    jobs = _StatusJob({"j1": JobStatus.queued, "verdict:j1": ResultNotFound()})
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    assert (
        await backend_tools().backend_cancel_task("j1") == "Task j1 abort requested but not confirmed (status: queued)"
    )


_IN_JOB_UNCONFIRMED = "Task j1 abort requested but not awaited inside a job, so not confirmed (status: queued)"


@pytest.mark.parametrize(
    ("verdict", "reply"),
    [
        (asyncio.CancelledError(), "Task j1 aborted"),
        (ResultNotFound(), _IN_JOB_UNCONFIRMED),
    ],
)
async def test_cancel_task_inside_a_job_requests_the_abort_without_waiting(
    monkeypatch, arq_redis, verdict, reply
) -> None:
    """Inside a job the cancel requests the abort and returns: a wait for the target's outcome
    could wait on a job queued behind the caller, which never starts while the caller runs."""
    jobs = _StatusJob({"j1": JobStatus.queued, "verdict:j1": verdict})
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    with running_job():
        assert await backend_tools().backend_cancel_task("j1") == reply
    assert jobs.waits == [0]
    assert await _marked(arq_redis, "j1")


async def test_cancel_task_inside_a_job_reports_the_abort_unconfirmed_with_its_status(monkeypatch, arq_redis) -> None:
    jobs = _StatusJob({"j1": JobStatus.queued, "verdict:j1": TimeoutError()})
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    with running_job():
        out = await backend_tools().backend_cancel_task("j1")
    assert out == _IN_JOB_UNCONFIRMED


async def test_cancel_task_inside_a_job_without_terminate_refuses_a_running_job(monkeypatch, arq_redis) -> None:
    await arq_redis.set(in_progress_key_prefix + "j1", b"1")
    monkeypatch.setattr(inspection, "job_handle", handle_of(_StatusJob({"j1": JobStatus.in_progress})))

    with running_job(), pytest.raises(UnsupportedBackendTool, match=_REFUSAL):
        await backend_tools().backend_cancel_task("j1")
    assert not await _marked(arq_redis, "j1")


def _fresh(statuses: dict[str, Any]) -> dict[str, Any]:
    """``statuses`` with each status list copied, so a second fake job replays them from the start."""
    return {key: list(value) if isinstance(value, list) else value for key, value in statuses.items()}


_REVIVED = TaskFailedError("ValueError", "ValueError('boom')", None)


@pytest.mark.parametrize(
    ("statuses", "reply"),
    [
        (
            {"j1": [JobStatus.queued, JobStatus.complete], "verdict:j1": "x"},
            f"Task j1 cannot be canceled (status: {JobStatus.complete.value})",
        ),
        (
            {"j1": [JobStatus.queued, JobStatus.not_found], "verdict:j1": ResultNotFound()},
            "Task j1 not found",
        ),
        (
            {
                "j1": [JobStatus.queued, JobStatus.complete],
                "verdict:j1": _REVIVED,
                "result:j1": _job_result("j1", success=False, result=_REVIVED),
            },
            "Task j1 failed on its own before the abort could take effect: ValueError('boom')",
        ),
        (
            {
                "j1": [JobStatus.queued, JobStatus.complete],
                "verdict:j1": SerializationError("unable to serialize result"),
                "result:j1": _job_result("j1", success=False, result="unable to serialize result"),
            },
            "Task j1 finished in failure after the abort request (aborted or failed on its own): "
            "'unable to serialize result'",
        ),
    ],
)
async def test_cancel_task_inside_a_job_reports_a_task_finishing_during_the_request_as_outside_one(
    monkeypatch, arq_redis, statuses, reply
) -> None:
    """A task that reaches its own outcome between the status check and the abort reads the same inside a job."""
    monkeypatch.setattr(inspection, "job_handle", handle_of(_StatusJob(_fresh(statuses))))
    outside = await backend_tools().backend_cancel_task("j1")

    monkeypatch.setattr(inspection, "job_handle", handle_of(_StatusJob(_fresh(statuses))))
    with running_job():
        inside = await backend_tools().backend_cancel_task("j1")

    assert inside == outside == reply


@pytest.mark.parametrize("terminate", [False, True])
@pytest.mark.parametrize(
    ("status", "reply"),
    [
        (JobStatus.not_found, "Task j1 not found"),
        (JobStatus.complete, f"Task j1 cannot be canceled (status: {JobStatus.complete.value})"),
    ],
)
async def test_cancel_task_not_cancellable(monkeypatch, arq_redis, status, reply, terminate) -> None:
    monkeypatch.setattr(inspection, "job_handle", handle_of(_StatusJob({"j1": status})))

    assert await backend_tools().backend_cancel_task("j1", terminate=terminate) == reply
    assert not await _marked(arq_redis, "j1")


async def test_cancel_task_wait_elapsed_reports_unconfirmed(monkeypatch, arq_redis) -> None:
    """arq's ``Job.result`` raises TimeoutError when the confirmation wait
    elapses; the tool reports requested-but-unconfirmed, with the status the job reached, instead of raising --
    the request stays recorded and the worker honors it."""
    jobs = _StatusJob({"j1": JobStatus.in_progress, "verdict:j1": TimeoutError()})
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    out = await backend_tools().backend_cancel_task("j1", terminate=True)
    assert out == "Task j1 abort requested but not confirmed (status: in_progress)"


async def test_cancel_task_revived_failure_replay_reports_failed_on_its_own(monkeypatch, arq_redis) -> None:
    """The wait polls the result key, so a stored FAILURE landing mid-wait
    replays out of the poll as its revived exception. A revived
    ``TaskFailedError`` outcome is the task's own failure — a worker abort
    stores a ``CancelledError``, which replays as a confirmed abort instead —
    so it reports as failed-on-its-own with the stored detail, never as
    "cannot be canceled" and never as a cancel failure."""
    stored = TaskFailedError("ValueError", "ValueError('boom')", None)
    jobs = _StatusJob(
        {
            "j1": [JobStatus.in_progress, JobStatus.complete],
            "verdict:j1": stored,
            "result:j1": _job_result("j1", success=False, result=stored),
        }
    )
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    out = await backend_tools().backend_cancel_task("j1", terminate=True)
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
    monkeypatch, arq_redis, replay, stored, detail
) -> None:
    """A stored failure without the revived ``TaskFailedError`` shape leaves
    the worker's abort and the task's own failure indistinguishable -- reported
    as aborted-or-failed with the raw stored value appended."""
    jobs = _StatusJob(
        {
            "j1": [JobStatus.in_progress, JobStatus.complete],
            "verdict:j1": replay,
            "result:j1": _job_result("j1", success=False, result=stored),
        }
    )
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    out = await backend_tools().backend_cancel_task("j1", terminate=True)
    assert out == f"Task j1 finished in failure after the abort request (aborted or failed on its own): {detail}"


async def test_cancel_task_exception_replay_without_stored_failure_reports_not_cancelable(
    monkeypatch, arq_redis
) -> None:
    """An exception replay whose stored outcome does not read back as a failure
    (no retained result by the time it is re-read) has no abort to report:
    the task finished, nothing to cancel."""
    jobs = _StatusJob(
        {
            "j1": [JobStatus.in_progress, JobStatus.complete],
            "verdict:j1": SerializationError("unable to serialize result"),
        }
    )
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    out = await backend_tools().backend_cancel_task("j1", terminate=True)
    assert out == f"Task j1 cannot be canceled (status: {JobStatus.complete.value})"


async def test_cancel_task_job_vanishing_during_wait_reports_not_cancelable(monkeypatch, arq_redis) -> None:
    """A task whose stored outcome replays but then vanishes before the status
    re-check (result retention lapsing) is equally finished: gone, nothing to
    cancel."""
    jobs = _StatusJob(
        {
            "j1": [JobStatus.in_progress, JobStatus.not_found],
            "verdict:j1": SerializationError("unable to serialize result"),
        }
    )
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    assert await backend_tools().backend_cancel_task("j1", terminate=True) == "Task j1 not found"


async def test_cancel_task_own_cancellation_propagates_never_reads_as_aborted(monkeypatch, arq_redis) -> None:
    """A stored abort and a cancellation of the task CALLING the cancel both
    surface from the wait as ``CancelledError``. A cancelled cancel call must
    propagate its cancellation, never read it as a false "aborted" success."""
    entered = asyncio.Event()

    class _WaitingJob:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def status(self) -> JobStatus:
            return JobStatus.in_progress

        async def result(self, timeout: float | None = None, *, poll_delay: float = 0.5) -> Any:
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(inspection, "job_handle", handle_of(_WaitingJob))
    task = asyncio.get_running_loop().create_task(backend_tools().backend_cancel_task("j1", terminate=True))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    ("final_status", "reply"),
    [
        (JobStatus.complete, f"Task j1 cannot be canceled (status: {JobStatus.complete.value})"),
        (JobStatus.not_found, "Task j1 not found"),
    ],
)
async def test_cancel_task_success_replay_reports_not_cancelable(monkeypatch, arq_redis, final_status, reply) -> None:
    """A task SUCCEEDING (or vanishing without a retained result) mid-wait
    ends the wait with a stored result (or ``ResultNotFound``) instead of an
    abort. The task finished, so this reads as "completed, nothing to cancel"
    -- never as a still-pending abort request."""
    verdict = "x" if final_status == JobStatus.complete else ResultNotFound()
    jobs = _StatusJob({"j1": [JobStatus.in_progress, final_status], "verdict:j1": verdict})
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    assert await backend_tools().backend_cancel_task("j1", terminate=True) == reply


async def test_cancel_task_real_wait_error_propagates(monkeypatch, arq_redis) -> None:
    jobs = _StatusJob({"j1": JobStatus.in_progress, "verdict:j1": ConnectionError("redis gone")})
    monkeypatch.setattr(inspection, "job_handle", handle_of(jobs))

    with pytest.raises(ConnectionError, match="redis gone"):
        await backend_tools().backend_cancel_task("j1", terminate=True)


# -- schedule CRUD tools ---------------------------------------------------------------


def _seed_schedule(redis: Any, name: str = "s1", enabled: bool = True) -> str:
    key = f"arq:schedule:{name}"
    redis._store[key] = {
        b"target": b"tool_execution",
        b"args": orjson.dumps([]),
        b"kwargs": orjson.dumps({"backend_tool_name": "t"}),
        b"schedule": orjson.dumps({"kind": "interval", "every": 60.0, "relative": False}),
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

    out = await backend_tools().backend_list_schedules()
    # A typed ``ScheduleListRow``: the fired tool and the cadence, never the stored args/kwargs.
    assert out == [
        {
            "name": "s1",
            "tool": "t",
            "cadence": {"kind": "interval", "every": 60.0, "relative": False},
            "enabled": True,
            "next_run_at_ts": 12345.0,
            "next_run_at_iso": datetime.fromtimestamp(12345.0, tz=UTC).isoformat(),
            "error": None,
        }
    ]


_OLD_FORMAT = orjson.dumps({"__type__": "interval", "every": 60.0, "relative": False})


async def test_an_un_reset_store_is_loud_on_list_export_and_get(fake_redis, bind_pool) -> None:
    from tai42_backend_arq.records import ScheduleStoreFormatError

    bind_pool(fake_redis)
    key = _seed_schedule(fake_redis)
    fake_redis._store[key][b"schedule"] = _OLD_FORMAT

    for read in (backend_tools().backend_list_schedules, backend_tools().backend_export_schedules):
        with pytest.raises(ScheduleStoreFormatError, match="delete the keys matching 'arq:schedule:\\*'"):
            await read()
    with pytest.raises(ScheduleStoreFormatError):
        await backend_tools().backend_get_schedule("s1")


async def test_update_refuses_a_relative_interval(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "job_handle", handle_of(_OkJob))
    key = _seed_schedule(fake_redis)
    with pytest.raises(ValueError, match="does not support relative interval schedules"):
        await backend_tools().backend_update_schedule(
            "s1", new_schedule={"kind": "interval", "every": 30, "relative": True}
        )
    assert fake_redis._store[key][b"cron_or_interval"] == b"60"


async def test_list_schedules_without_recorded_next_run(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    key = _seed_schedule(fake_redis)
    del fake_redis._store[key][b"last_scheduled_ts"]

    (row,) = await backend_tools().backend_list_schedules()
    assert row["next_run_at_ts"] is None
    assert row["next_run_at_iso"] is None


async def test_get_schedule_and_exists(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    _seed_schedule(fake_redis)

    assert await backend_tools().backend_schedule_exists("s1") is True
    assert await backend_tools().backend_schedule_exists("nope") is False
    out = await backend_tools().backend_get_schedule("s1")
    assert out["name"] == "s1"
    assert out["enabled"] is True
    assert out["cadence"] == {"kind": "interval", "every": 60.0, "relative": False}
    assert await backend_tools().backend_get_schedule("nope") == {"status": "not_found", "name": "nope"}


async def test_enable_disable_schedule(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    key = _seed_schedule(fake_redis, enabled=False)

    assert await backend_tools().backend_enable_schedule("s1") == {"status": "enabled", "name": "s1"}
    assert fake_redis._store[key][b"enabled"] == b"true"
    assert await backend_tools().backend_disable_schedule("s1") == {"status": "disabled", "name": "s1"}
    assert fake_redis._store[key][b"enabled"] == b"false"
    assert await backend_tools().backend_enable_schedule("nope") == {"status": "not_found", "name": "nope"}
    assert await backend_tools().backend_disable_schedule("nope") == {"status": "not_found", "name": "nope"}


@pytest.mark.parametrize("flip", ["backend_enable_schedule", "backend_disable_schedule"])
async def test_enable_disable_deleted_concurrently_not_resurrected(fake_redis, bind_pool, flip) -> None:
    """A delete landing before the flag write acquires the per-schedule lock
    wins: the flag write reports not_found and must not recreate the schedule
    as a partial enabled-only hash (unrunnable, and one such hash fails the
    whole export)."""
    key = _seed_schedule(fake_redis)
    bind_pool(DeleteOnLockRedis(fake_redis, key))

    assert await getattr(backend_tools(), flip)("s1") == {"status": "not_found", "name": "s1"}
    assert key not in fake_redis._store


async def test_delete_schedule_aborts_and_deletes(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "job_handle", handle_of(_OkJob))
    key = _seed_schedule(fake_redis)

    assert await backend_tools().backend_delete_schedule("s1") == {"status": "deleted", "name": "s1"}
    assert key not in fake_redis._store
    assert await backend_tools().backend_delete_schedule("s1") == {"status": "not_found", "name": "s1"}


async def test_delete_schedule_failed_abort_propagates(fake_redis, monkeypatch, bind_pool) -> None:
    class _BadAbort(_OkJob):
        async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
            raise RuntimeError("abort failed")

    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "job_handle", handle_of(_BadAbort))
    key = _seed_schedule(fake_redis)

    with pytest.raises(RuntimeError, match="abort failed"):
        await backend_tools().backend_delete_schedule("s1")
    # The schedule was NOT deleted behind the failed abort.
    assert key in fake_redis._store


async def test_delete_schedule_deleted_concurrently_reports_not_found(fake_redis, monkeypatch, bind_pool) -> None:
    """A delete that loses the race for the per-schedule lock to another delete
    finds the hash gone: it reports not_found without touching any job."""

    async def _no_job(redis: Any, job_id: str, queue: str | None = None) -> Any:
        raise AssertionError(f"job {job_id} is touched")

    monkeypatch.setattr(scheduler, "job_handle", _no_job)
    key = _seed_schedule(fake_redis)
    bind_pool(DeleteOnLockRedis(fake_redis, key))

    assert await backend_tools().backend_delete_schedule("s1") == {"status": "not_found", "name": "s1"}
    assert key not in fake_redis._store


async def test_get_schedule_and_run_now_skip_schedule_deleted_mid_read(fake_redis, bind_pool) -> None:
    """A hash that reads back empty was deleted concurrently (an existing Redis
    hash is never empty): get must not fabricate a row and run-now must not
    enqueue an empty-target job."""
    bind_pool(fake_redis)
    fake_redis._store["arq:schedule:vanished"] = {}

    assert await backend_tools().backend_get_schedule("vanished") == {"status": "not_found", "name": "vanished"}
    assert await backend_tools().backend_run_schedule_now("vanished") == {"status": "not_found", "name": "vanished"}
    assert fake_redis.enqueued == []


async def test_run_schedule_now_enqueues_target(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    _seed_schedule(fake_redis)

    assert await backend_tools().backend_run_schedule_now("s1") == {"status": "queued", "name": "s1"}
    assert fake_redis.enqueued == [("tool_execution",)]
    assert await backend_tools().backend_run_schedule_now("nope") == {"status": "not_found", "name": "nope"}


async def test_run_schedule_now_enqueues_on_the_settings_queue(arq_redis) -> None:
    arq_redis.default_queue_name = f"{SETTINGS_QUEUE}.gpu"
    await arq_redis.hset("arq:schedule:s1", mapping={"target": "tool_execution", "args": "[]", "kwargs": "{}"})

    assert await backend_tools().backend_run_schedule_now("s1") == {"status": "queued", "name": "s1"}
    assert await arq_redis.zcard(SETTINGS_QUEUE) == 1
    assert await arq_redis.zcard(f"{SETTINGS_QUEUE}.gpu") == 0


async def test_update_schedule_new_schedule(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "job_handle", handle_of(_OkJob))
    key = _seed_schedule(fake_redis)

    out = await backend_tools().backend_update_schedule("s1", new_schedule=120)

    assert out["status"] == "updated"
    assert out["previous_schedule"] == {"kind": "interval", "every": 60.0, "relative": False}
    assert out["new_schedule"] == {"kind": "interval", "every": 120.0, "relative": False}
    # The next-run timestamp is reported even when derived from the schedule.
    assert out["next_run_at_ts"] == pytest.approx(datetime.now(UTC).timestamp() + 120, abs=5)
    assert out["next_run_at_iso"]
    assert fake_redis._store[key][b"cron_or_interval"] == b"120"


async def test_update_schedule_next_run_in_ms(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "job_handle", handle_of(_OkJob))
    _seed_schedule(fake_redis)

    out = await backend_tools().backend_update_schedule("s1", next_run_in_ms=5000)

    assert out["status"] == "updated"
    assert out["next_run_at_ts"] == pytest.approx(datetime.now(UTC).timestamp() + 5, abs=5)


async def test_update_schedule_crontab_next_run(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "job_handle", handle_of(_OkJob))
    key = _seed_schedule(fake_redis)

    out = await backend_tools().backend_update_schedule("s1", new_schedule="0 9 * * 1")

    assert out["status"] == "updated"
    assert fake_redis._store[key][b"cron_or_interval"] == b"0 9 * * 1"
    assert out["next_run_at_ts"] > datetime.now(UTC).timestamp()


async def test_update_schedule_deleted_concurrently_reports_not_found(fake_redis, monkeypatch, bind_pool) -> None:
    """A delete landing between the update's exists-check and its transition
    wins: the update reports not_found and must not resurrect the schedule as
    a partial hash."""
    key = _seed_schedule(fake_redis)
    bind_pool(DeleteOnLockRedis(fake_redis, key))
    monkeypatch.setattr(scheduler, "job_handle", handle_of(_OkJob))

    out = await backend_tools().backend_update_schedule("s1", new_schedule=120)

    assert out == {"status": "not_found", "name": "s1"}
    assert key not in fake_redis._store
    assert fake_redis.enqueued == []


async def test_update_schedule_skipped_and_not_found(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    _seed_schedule(fake_redis)

    out = await backend_tools().backend_update_schedule("s1")
    assert out["status"] == "skipped"

    out = await backend_tools().backend_update_schedule("nope", new_schedule=60)
    assert out["status"] == "not_found"
