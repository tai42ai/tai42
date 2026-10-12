"""Every arq ``Job`` handle the plugin builds reads the configured queue (``ARQ_QUEUE_NAME``).

The worker consumes and the enqueuer writes the queue the settings name. A ``Job`` handle reads
that queue's sorted set to tell a queued job from a missing one and to keep waiting on a running
one, so a handle on any other queue reads a queued job as not found and gives up on a running
job's result at once.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from arq.constants import default_queue_name
from arq.jobs import JobStatus
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

from tai42_backend_arq import scheduler, tasks, tools
from tai42_backend_arq.settings import ArqSettings

from .conftest import lease_of

_QUEUE = "ns-1:arq:queue"


class _RecordingJob:
    """A ``Job`` stand-in recording the queue it was built for; arq's own default when none is given."""

    def __init__(self, built: list[str], status: JobStatus) -> None:
        self._built = built
        self._status = status

    def __call__(
        self, job_id: str, redis: Any = None, _queue_name: str = default_queue_name, _deserializer: Any = None
    ) -> Any:
        self._built.append(_queue_name)
        status = self._status

        class _Job:
            async def status(self) -> JobStatus:
                return status

            async def result(self, timeout: float | None = None) -> Any:
                return {"value": 1}

        return _Job()


def _settings() -> ArqSettings:
    return ArqSettings(queue_name=_QUEUE, callback_timeout=5)


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    for module in (tools, tasks, scheduler):
        monkeypatch.setattr(module, "arq_settings", _settings)
    return []


async def test_the_task_status_tool_reads_the_configured_queue(monkeypatch, built) -> None:
    monkeypatch.setattr(tools, "Job", _RecordingJob(built, JobStatus.queued))
    monkeypatch.setattr(tools.pool, "arq_connection", lease_of(object()))

    assert await tools.backend_task_status("task-1") == "queued"
    assert built == [_QUEUE]


async def test_the_task_result_tool_reads_the_configured_queue(monkeypatch, built) -> None:
    monkeypatch.setattr(tools, "Job", _RecordingJob(built, JobStatus.in_progress))
    monkeypatch.setattr(tools.pool, "arq_connection", lease_of(object()))

    assert await tools.backend_task_result("task-1", timeout=5) == {"value": 1}
    assert built == [_QUEUE]


async def test_the_cancel_tool_reads_the_configured_queue(monkeypatch, built) -> None:
    monkeypatch.setattr(tools, "Job", _RecordingJob(built, JobStatus.complete))
    monkeypatch.setattr(tools.pool, "arq_connection", lease_of(object()))

    assert "cannot be canceled" in await tools.backend_cancel_task("task-1")
    assert built == [_QUEUE]


async def test_the_callback_job_reads_the_configured_queue(monkeypatch, built, stub_app) -> None:
    monkeypatch.setattr(tasks, "Job", _RecordingJob(built, JobStatus.complete))
    stub_app.tools.run_tool_mock = AsyncMock(return_value="chained")

    callback = {"tool": "next_tool", "expr": {"content": "."}, "carried_kwargs": {WORKER_SECRET_CAPABILITY_ARG: False}}
    await tasks.callback_job({"redis": object()}, "job-1", callback)

    assert built == [_QUEUE]


async def test_a_schedule_abort_reads_the_configured_queue(monkeypatch, built, fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    await fake_redis.hset("schedule-key", mapping={"job_id": "job-1"})
    monkeypatch.setattr(scheduler, "Job", _RecordingJob(built, JobStatus.queued))
    monkeypatch.setattr(scheduler, "request_job_abort", AsyncMock(return_value=True))

    await scheduler.abort_schedule_task("schedule-key")

    assert built == [_QUEUE]
