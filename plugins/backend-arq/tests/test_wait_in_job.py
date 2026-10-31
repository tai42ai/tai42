"""A job does not wait for another job's result: the wait tools refuse inside a job and run outside one.

Inside an arq job the tools raise before they enqueue or wait, so a refused call leaves no job
behind; the job chains the next job with a callback instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from arq.jobs import JobStatus

from tai42_backend_arq import extensions, tools, worker

_REFUSED = "cannot wait for another job's result from inside a job: chain it with a callback instead"


async def sample_tool(text: str) -> str:
    """Echo ``text``."""
    return text


class _FakeArq:
    """Records enqueue calls and answers each with a job whose result is ``done``."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)

        class _Job:
            job_id = "job-1"

            async def status(self) -> JobStatus:
                return JobStatus.complete

            async def result(self, timeout: float | None = None) -> Any:
                return "done"

        return _Job()


@pytest.fixture
def fake(bind_pool: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch) -> _FakeArq:
    arq = _FakeArq()
    bind_pool(arq)

    async def handle(redis: Any, job_id: str) -> Any:
        return await arq.enqueue_job(job_id)

    monkeypatch.setattr(tools, "job_handle", handle)
    return arq


def _waits() -> dict[str, Callable[[], Awaitable[Any]]]:
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")
    return {
        "sample_tool_sync_task": lambda: branch(text="hi"),
        "backend_task_result": lambda: tools.backend_task_result("job-1", timeout=5),
    }


async def _in_job(call: Callable[[], Awaitable[Any]]) -> Any:
    async def body(ctx: dict[str, Any]) -> Any:
        return await call()

    return await worker._in_job_span(body).coroutine({})


@pytest.mark.parametrize("name", ["sample_tool_sync_task", "backend_task_result"])
async def test_a_wait_tool_refuses_inside_a_job(fake: _FakeArq, name: str) -> None:
    with pytest.raises(RuntimeError) as refused:
        await _in_job(_waits()[name])

    assert str(refused.value) == f"{name} {_REFUSED}"
    assert fake.calls == []


@pytest.mark.parametrize("name", ["sample_tool_sync_task", "backend_task_result"])
async def test_a_wait_tool_returns_the_result_outside_a_job(fake: _FakeArq, name: str) -> None:
    assert await _waits()[name]() == "done"


async def test_a_job_reads_another_jobs_finished_result_without_waiting(fake: _FakeArq) -> None:
    """With no ``timeout`` the tool only reads the result's current state, which a job may do."""
    assert await _in_job(lambda: tools.backend_task_result("job-1")) == "done"
