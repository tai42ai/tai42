"""``backend_task_result`` status/timeout matrix over a canned fake Job."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from arq.jobs import JobStatus, ResultNotFound

from tai42_backend_arq import tools
from tai42_backend_arq.settings import TaskFailedError


class _FakeJob:
    """Stand-in for ``arq.jobs.Job`` driven by canned status/result behavior."""

    def __init__(self, *, statuses: list[JobStatus], result: Any) -> None:
        # ``statuses`` is consumed one entry per ``status()`` call; the last
        # entry is repeated once exhausted.
        self._statuses = list(statuses)
        self._result = result

    async def status(self) -> JobStatus:
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    async def result(self, timeout: float | None = None) -> Any:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


async def _run(*, statuses: list[JobStatus], result: Any, timeout: float | None) -> Any:
    job = _FakeJob(statuses=statuses, result=result)
    with (
        patch.object(tools.RedisPoolManager, "get", AsyncMock(return_value=object())),
        patch.object(tools, "Job", return_value=job),
    ):
        return await tools.backend_task_result("task-1", timeout=timeout)


async def test_no_timeout_returns_snapshot_when_not_complete() -> None:
    out = await _run(statuses=[JobStatus.in_progress], result="ignored", timeout=None)
    assert out == "Task task-1 is not ready (status: JobStatus.in_progress)"


async def test_no_timeout_returns_result_when_complete() -> None:
    out = await _run(statuses=[JobStatus.complete], result={"value": 42}, timeout=None)
    assert out == {"value": 42}


async def test_not_found_returns_clear_value() -> None:
    out = await _run(statuses=[JobStatus.not_found], result="ignored", timeout=5)
    assert out == "Task task-1 not found"


async def test_timeout_waits_for_completion() -> None:
    """With a timeout, an incomplete status must NOT early-return; result() waits."""
    out = await _run(statuses=[JobStatus.in_progress], result="done", timeout=5)
    assert out == "done"


async def test_timeout_elapsed_returns_not_ready() -> None:
    """When the wait elapses (arq raises TimeoutError) the value matches the no-wait path."""
    out = await _run(
        statuses=[JobStatus.in_progress, JobStatus.in_progress],
        result=TimeoutError(),
        timeout=1,
    )
    assert out == "Task task-1 is not ready (status: JobStatus.in_progress)"


async def test_result_not_found_returns_clear_value() -> None:
    out = await _run(statuses=[JobStatus.complete], result=ResultNotFound("gone"), timeout=5)
    assert out == "No result found for task task-1"


async def test_unexpected_error_propagates_not_swallowed() -> None:
    """A real failure must surface, not be encoded as a normal-looking string."""
    boom = RuntimeError("redis exploded")
    with pytest.raises(RuntimeError, match="redis exploded"):
        await _run(statuses=[JobStatus.complete], result=boom, timeout=5)


async def test_job_exception_propagates() -> None:
    """A stored failure replaying out of ``job.result()`` re-raises whole, never
    stringified. With this backend's JSON result payloads that replay is the
    ``TaskFailedError`` the deserializer revives from the stored tagged
    description; the fake drives the propagation path with a plain exception."""
    with pytest.raises(ValueError, match="task blew up"):
        await _run(statuses=[JobStatus.complete], result=ValueError("task blew up"), timeout=None)


async def test_replayed_abort_raises_detail_not_cancellation() -> None:
    """A stored abort replays out of ``job.result()`` as its revived
    ``CancelledError``; the tool re-raises it as ``TaskFailedError`` carrying
    the stored detail — a raw CancelledError escaping the tool would read as a
    cancellation of the tool call itself."""
    with pytest.raises(TaskFailedError, match=r"CancelledError\(\)"):
        await _run(statuses=[JobStatus.complete], result=asyncio.CancelledError("CancelledError()"), timeout=None)


async def test_own_cancellation_propagates_never_reads_as_stored_abort() -> None:
    """A cancellation of the task CALLING ``backend_task_result`` surfaces from
    the same ``await job.result()`` as a replayed stored abort. It must
    propagate as the cancellation it is, never convert into a
    ``TaskFailedError``."""
    entered = asyncio.Event()

    class _HangingJob:
        async def status(self) -> JobStatus:
            return JobStatus.complete

        async def result(self, timeout: float | None = None) -> Any:
            entered.set()
            await asyncio.Event().wait()

    with (
        patch.object(tools.RedisPoolManager, "get", AsyncMock(return_value=object())),
        patch.object(tools, "Job", return_value=_HangingJob()),
    ):
        task = asyncio.get_running_loop().create_task(tools.backend_task_result("task-1", timeout=5))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_task_status_returns_enum_value() -> None:
    job = _FakeJob(statuses=[JobStatus.queued], result=None)
    with (
        patch.object(tools.RedisPoolManager, "get", AsyncMock(return_value=object())),
        patch.object(tools, "Job", return_value=job),
    ):
        assert await tools.backend_task_status("task-1") == "queued"
