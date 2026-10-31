"""A dispatched job's body runs at most once: arq's second pickup of a job is refused, never run.

arq picks a job up again in two ways, and both are driven here through a real
:class:`arq.Worker` the runtime builds, over an in-process fake Redis server:

- a hard worker death: arq's ``run_job`` counts the pickup (``INCR arq:retry:<id>``)
  and the dying worker leaves the job in its queue; once the in-progress key
  expires, any worker picks it up again. The test leaves exactly that state in
  Redis: the counted try, the queued job, no in-progress key.
- a cold stop: the running job is cancelled, and arq leaves a cancelled job in its
  queue for the next worker to pick up.

Either way the second pickup ends the job as a failure the caller reads back, and
the tool body is never entered again. The schedule driver is the one job function
that takes later pickups: they only move its schedule on, never fire its target again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, cast

import arq.worker
import pytest
from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.constants import in_progress_key_prefix, retry_key_prefix
from arq.jobs import Job, JobStatus
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

from tai42_backend_arq import scheduler
from tai42_backend_arq.scheduler import wait_job_result
from tai42_backend_arq.settings import TaskFailedError, arq_settings, job_deserializer, job_serializer
from tai42_backend_arq.tools import backend_list_failed_tasks
from tai42_backend_arq.worker import ArqWorkerRuntime

#: The failure a caller and the failed-task listing read for a job whose run was cut.
_CUT_TYPE = "JobRunCutError"
_CUT = "JobRunCutError('the run was cut (its worker died or was stopped mid-run) and is not run again')"

_STARTS_WITHIN = 5.0

# arq's own ``Worker.close`` closes its pool through redis-py's deprecated ``close``.
pytestmark = pytest.mark.filterwarnings("ignore:Call to deprecated close:DeprecationWarning")


async def _no_redis_info(redis: Any, log_func: Any) -> None:
    return None


@pytest.fixture
async def queue(redis_url: str, settings_queue: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ArqRedis]:
    """An enqueue handle on the fake Redis server the workers consume, on the settings queue."""
    # A worker's start logs the server's ``INFO``, a command the fake server does not serve.
    monkeypatch.setattr(arq.worker, "log_redis_info", _no_redis_info)
    redis = await create_pool(
        RedisSettings.from_dsn(redis_url),
        job_serializer=job_serializer,
        job_deserializer=job_deserializer,
        default_queue_name=settings_queue,
    )
    try:
        yield redis
    finally:
        await redis.aclose()


async def _dispatch(queue: ArqRedis, **arguments: Any) -> Job:
    job = await queue.enqueue_job(
        "tool_execution",
        **{arq_settings().tool_name_arg: "echo", WORKER_SECRET_CAPABILITY_ARG: False},
        **arguments,
    )
    assert job is not None
    return job


async def _runtime(*options: str) -> ArqWorkerRuntime:
    runtime = ArqWorkerRuntime.from_args(list(options))
    runtime.bind_progress(lambda *, next_within: None)
    await runtime.build()
    return runtime


async def _drain_burst() -> None:
    """Run one burst worker until its queue is empty."""
    runtime = await _runtime("--burst")
    try:
        await asyncio.wait_for(runtime.run_on_loop(), _STARTS_WITHIN)
    finally:
        await runtime.aclose()


async def _refusal(job: Job) -> TaskFailedError:
    with pytest.raises(TaskFailedError) as refused:
        await wait_job_result(job, timeout=_STARTS_WITHIN)
    return refused.value


async def test_a_job_picked_up_again_after_a_hard_worker_death_never_runs_its_body(
    stub_app, queue, bind_pool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="arq.worker")
    job = await _dispatch(queue, text="hi")
    # What a worker that died inside the job leaves: the pickup it counted, the job still
    # queued, and (once its in-progress key expired) nothing marking the job as running.
    await queue.incr(retry_key_prefix + job.job_id)
    assert not await queue.exists(in_progress_key_prefix + job.job_id)

    await _drain_burst()

    stub_app.tools.run_tool_mock.assert_not_awaited()
    refused = await _refusal(job)
    assert refused.error_type == _CUT_TYPE
    assert refused.error_repr == _CUT
    bind_pool(queue)
    assert await backend_list_failed_tasks() == [{"task_id": job.job_id, "error": _CUT}]
    # What the operator reads in the worker log for the refused pickup.
    assert f"{job.job_id}:tool_execution failed, {_CUT_TYPE}: the run was cut" in caplog.text


async def test_a_job_cut_by_a_cold_stop_is_refused_at_its_next_pickup(stub_app, queue) -> None:
    entered = asyncio.Event()

    async def body(key: str, arguments: dict[str, Any]) -> Any:
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()
        return "ran again"

    stub_app.tools.run_tool_mock.side_effect = body
    job = await _dispatch(queue, text="hi")

    runtime = await _runtime()
    run = asyncio.ensure_future(runtime.run_on_loop())
    try:
        await asyncio.wait_for(entered.wait(), _STARTS_WITHIN)
        runtime.request_terminate()
        with pytest.raises(asyncio.CancelledError):
            await run
        # arq records the cut job's outcome under a shield; let it land before closing.
        await asyncio.gather(*runtime.worker.tasks.values())
    finally:
        await runtime.aclose()
    # The cold stop left the cut job queued for the next worker.
    assert await job.status() == JobStatus.queued

    await _drain_burst()

    assert stub_app.tools.run_tool_mock.await_count == 1
    refused = await _refusal(job)
    assert refused.error_type == _CUT_TYPE
    assert refused.error_repr == _CUT


async def test_a_job_runs_its_body_once_on_its_first_pickup(stub_app, queue) -> None:
    stub_app.tools.run_tool_mock.return_value = "done"
    job = await _dispatch(queue, text="hi")

    await _drain_burst()

    assert stub_app.tools.run_tool_mock.await_count == 1
    assert await wait_job_result(job, timeout=_STARTS_WITHIN) == "done"


async def _schedule(queue: ArqRedis, name: str) -> Job:
    """A one-minute interval schedule whose pending driver job is due now."""
    settings = arq_settings()
    driver = await queue.enqueue_job("task_scheduler", name)
    assert driver is not None
    # redis-py types its asyncio commands as the sync-or-async union.
    await cast(
        "Awaitable[int]",
        queue.hset(
            settings.arq_schedule_key(name),
            mapping={
                "target": "tool_execution",
                "args": "[]",
                "kwargs": f'{{"{settings.tool_name_arg}": "echo", "{WORKER_SECRET_CAPABILITY_ARG}": false}}',
                "cron_or_interval": "60",
                "enabled": "true",
                "job_id": driver.job_id,
                "last_scheduled_ts": str(time.time()),
            },
        ),
    )
    return driver


@asynccontextmanager
async def _unlocked(redis: Any, schedule_name: str) -> AsyncIterator[None]:
    yield


async def test_a_schedule_driver_picked_up_again_moves_the_schedule_on_without_firing(
    stub_app, queue, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=scheduler.__name__)
    # The schedule lock releases through a Lua script, which the fake server does not run.
    monkeypatch.setattr(scheduler, "schedule_lock", _unlocked)
    name = "every-minute"
    driver = await _schedule(queue, name)
    await queue.incr(retry_key_prefix + driver.job_id)

    # Not a burst run: the driver defers its own replacement, which a burst worker would wait for.
    runtime = await _runtime()
    run = asyncio.ensure_future(runtime.run_on_loop())
    try:
        assert await wait_job_result(driver, timeout=_STARTS_WITHIN) is None
        runtime.request_drain()
        with pytest.raises(asyncio.CancelledError):
            await run
    finally:
        await runtime.aclose()

    stub_app.tools.run_tool_mock.assert_not_awaited()
    assert (
        f"Schedule '{name}': job {driver.job_id} was picked up again (try 2) after its run was cut; "
        "the fire is not repeated and the schedule moves on to its next run."
    ) in caplog.messages
    stored = await queue.hget(arq_settings().arq_schedule_key(name), "job_id")
    assert stored is not None
    pending = stored.decode()
    assert pending != driver.job_id
    assert await Job(pending, queue, _queue_name=arq_settings().queue_name).status() == JobStatus.deferred
