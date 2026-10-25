"""A job found by id is read on the queue it was sent to.

arq keeps no queue on a queued job: a ``Job`` handle reads its own queue's sorted set to
tell a queued job from a missing one, so a handle on another queue reads a queued job as
not found. ``job_handle`` probes the settings queue and every registered derived queue
and pins the handle to the one holding the id; every by-id door builds its handle there.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from arq.jobs import JobStatus
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

from tai42_backend_arq import scheduler, tasks, tools
from tai42_backend_arq.jobs import job_handle, known_queues, register_queue

from .conftest import SETTINGS_QUEUE

_GPU = f"{SETTINGS_QUEUE}.gpu"


async def test_the_status_tool_reads_a_labelled_job_on_its_queue(arq_redis) -> None:
    job = await tasks.enqueue_task(arq_redis, queue="gpu", backend_tool_name="t")

    assert await tools.backend_task_status(job.job_id) == JobStatus.queued.value


async def test_the_result_tool_waits_on_a_labelled_job(arq_redis) -> None:
    job = await tasks.enqueue_task(arq_redis, queue="gpu", backend_tool_name="t")

    answer = await tools.backend_task_result(job.job_id, timeout=0.2)

    assert answer.startswith(f"Task {job.job_id} is not ready")
    assert "queued" in answer


async def test_the_cancel_tool_aborts_a_labelled_job_on_its_queue(arq_redis, monkeypatch) -> None:
    monkeypatch.setenv("ARQ_TASK_TIMEOUT", "1")
    reset_all_settings()
    job = await tasks.enqueue_task(arq_redis, queue="gpu", countdown=30, backend_tool_name="t")

    answer = await tools.backend_cancel_task(job.job_id)

    assert answer == f"Task {job.job_id} abort requested but not confirmed"
    # arq moves a deferred job to the front of its own queue so a worker picks up the abort.
    assert await arq_redis.zscore(_GPU, job.job_id) == 1


async def test_a_job_on_the_settings_queue_resolves_there(arq_redis) -> None:
    await register_queue(arq_redis, _GPU)
    job = await tasks.enqueue_task(arq_redis, backend_tool_name="t")

    handle = await job_handle(arq_redis, job.job_id)

    assert handle._queue_name == SETTINGS_QUEUE
    assert await handle.status() == JobStatus.queued


async def test_an_unknown_id_resolves_to_the_settings_queue_as_not_found(arq_redis) -> None:
    await register_queue(arq_redis, _GPU)

    handle = await job_handle(arq_redis, "no-such-job")

    assert handle._queue_name == SETTINGS_QUEUE
    assert await handle.status() == JobStatus.not_found
    assert await tools.backend_task_status("no-such-job") == JobStatus.not_found.value


async def test_a_job_id_on_two_queues_raises_naming_both(arq_redis) -> None:
    await register_queue(arq_redis, _GPU)
    await arq_redis.zadd(SETTINGS_QUEUE, {"twice": 1})
    await arq_redis.zadd(_GPU, {"twice": 1})

    with pytest.raises(RuntimeError, match=r"job twice is queued on more than one queue") as raised:
        await job_handle(arq_redis, "twice")

    assert SETTINGS_QUEUE in str(raised.value)
    assert _GPU in str(raised.value)


async def test_known_queues_lead_with_the_settings_queue(arq_redis) -> None:
    await register_queue(arq_redis, f"{SETTINGS_QUEUE}.b")
    await register_queue(arq_redis, f"{SETTINGS_QUEUE}.a")
    await register_queue(arq_redis, SETTINGS_QUEUE)

    assert await known_queues(arq_redis) == [SETTINGS_QUEUE, f"{SETTINGS_QUEUE}.a", f"{SETTINGS_QUEUE}.b"]
    assert await arq_redis.smembers(f"{SETTINGS_QUEUE}:queues") == {
        f"{SETTINGS_QUEUE}.a".encode(),
        f"{SETTINGS_QUEUE}.b".encode(),
    }


class _HandleRecorder:
    """A ``job_handle`` stand-in recording each id it resolves; every handle reads ``status``."""

    def __init__(self, status: JobStatus) -> None:
        self.resolved: list[str] = []
        self._status = status

    async def __call__(self, redis: Any, job_id: str) -> Any:
        self.resolved.append(job_id)
        status = self._status

        class _Job:
            def __init__(self) -> None:
                self.job_id = job_id

            async def status(self) -> JobStatus:
                return status

            async def result(self, timeout: float | None = None) -> Any:
                return {"value": 1}

        return _Job()


async def test_the_callback_poll_resolves_its_predecessor_through_the_lookup(monkeypatch, stub_app) -> None:
    recorder = _HandleRecorder(JobStatus.complete)
    monkeypatch.setattr(tasks, "job_handle", recorder)
    stub_app.tools.run_tool_mock = AsyncMock(return_value="chained")

    callback = {"tool": "next_tool", "expr": {"content": "."}, "carried_kwargs": {WORKER_SECRET_CAPABILITY_ARG: False}}
    await tasks.callback_job({"redis": object()}, "job-1", callback)

    assert recorder.resolved == ["job-1"]


async def test_a_schedule_abort_resolves_its_pending_job_through_the_lookup(monkeypatch, fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    await fake_redis.hset("schedule-key", mapping={"job_id": "job-1"})
    recorder = _HandleRecorder(JobStatus.complete)
    monkeypatch.setattr(scheduler, "job_handle", recorder)

    await scheduler.abort_schedule_task("schedule-key")

    assert recorder.resolved == ["job-1"]


async def test_a_schedule_transition_resolves_the_replaced_job_through_the_lookup(monkeypatch, fake_redis) -> None:
    await fake_redis.hset("arq:schedule:s1", mapping={"job_id": "job-1", "enabled": "true"})
    recorder = _HandleRecorder(JobStatus.complete)
    monkeypatch.setattr(scheduler, "job_handle", recorder)

    await scheduler.safe_schedule_transition(fake_redis, "s1", defer_by=0, last_scheduled_ts=0.0)

    assert recorder.resolved == ["job-1"]


async def test_the_stalled_schedule_watchdog_resolves_the_pending_job_through_the_lookup(
    monkeypatch, fake_redis
) -> None:
    await fake_redis.hset("arq:schedule:s1", mapping={"job_id": "job-1", "enabled": "true"})
    recorder = _HandleRecorder(JobStatus.deferred)
    monkeypatch.setattr(scheduler, "job_handle", recorder)

    await scheduler.recover_stalled_schedules({"redis": fake_redis})

    assert recorder.resolved == ["job-1"]
