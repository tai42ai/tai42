"""A job does not wait for another job's result: the wait tools refuse inside a job and run outside one.

Every pool runs its jobs through the worker's ``perform_job`` (in the work-horse child on the
prefork pool, in the worker process on the others), and that is where a job is marked. Inside
a job the tools raise before they enqueue or wait, so a refused call leaves no job behind.
"""

from __future__ import annotations

import asyncio
import base64
import pickle
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from rq.job import JobStatus

from tai42_backend_rq import extensions, tools, worker_pools
from tai42_backend_rq.settings import rq_settings

from .conftest import FakeAsyncRedis, _patch_redis

_REFUSED = "cannot wait for another job's result from inside a job: chain it with a callback instead"
_WAITS = ("sample_tool_sync_task", "backend_task_result")
_POOLS = (worker_pools.CustomRQWorker, worker_pools.CustomRQSimpleWorker)


async def sample_tool(text: str) -> str:
    """Echo ``text``."""
    return text


class _FinishedJob:
    id = "job-1"

    def refresh(self) -> None:
        pass

    def get_status(self) -> str:
        return JobStatus.FINISHED

    def return_value(self) -> Any:
        return "done"


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every job the tools queue, each finished at once with ``done``, its result readable."""
    calls: list[dict[str, Any]] = []

    async def enqueue(*args: Any, **kwargs: Any) -> _FinishedJob:
        calls.append(kwargs)
        return _FinishedJob()

    monkeypatch.setattr(extensions, "enqueue_task", enqueue)
    settings = rq_settings()

    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key("job-1"): {"status": JobStatus.FINISHED.encode()}},
        streams={
            settings.rq_result_key("job-1"): [(b"0-1", {b"return_value": base64.b64encode(pickle.dumps("done"))})]
        },
    )
    _patch_redis(monkeypatch, redis)
    return calls


def _call(name: str) -> Callable[[], Awaitable[Any]]:
    if name == "backend_task_result":
        return lambda: tools.backend_task_result("job-1", timeout=5)
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")
    return lambda: branch(text="hi")


def _in_job(pool: type[Any], call: Callable[[], Awaitable[Any]], monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run ``call`` as the body of one job the ``pool`` worker performs, as rq runs a coroutine job."""
    outcome: list[Any] = []

    def vendor_perform_job(self: Any, job: Any, queue: Any) -> bool:
        loop = asyncio.new_event_loop()
        try:
            outcome.append(loop.run_until_complete(call()))
        finally:
            loop.close()
        return True

    for vendor in (worker_pools.Worker, worker_pools.SimpleWorker):
        monkeypatch.setattr(vendor, "perform_job", vendor_perform_job)
    assert object.__new__(pool).perform_job("job", "queue") is True
    return outcome[0]


@pytest.mark.parametrize("pool", _POOLS)
@pytest.mark.parametrize("name", _WAITS)
def test_a_wait_tool_refuses_inside_a_job(
    app: Any, enqueued: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, pool: type[Any], name: str
) -> None:
    with pytest.raises(RuntimeError) as refused:
        _in_job(pool, _call(name), monkeypatch)

    assert str(refused.value) == f"{name} {_REFUSED}"
    assert enqueued == []


@pytest.mark.parametrize("name", _WAITS)
async def test_a_wait_tool_returns_the_result_outside_a_job(enqueued: list[dict[str, Any]], name: str) -> None:
    assert await _call(name)() == "done"


@pytest.mark.parametrize("pool", _POOLS)
def test_a_job_reads_another_jobs_finished_result_without_waiting(
    app: Any, enqueued: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, pool: type[Any]
) -> None:
    """With no ``timeout`` the tool only reads the result's current state, which a job may do."""
    assert _in_job(pool, lambda: tools.backend_task_result("job-1"), monkeypatch) == "done"
