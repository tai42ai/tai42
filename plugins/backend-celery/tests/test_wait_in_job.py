"""A job does not wait for another job's result: the wait tools refuse inside a job and run outside one.

Inside a celery job the tools raise before they dispatch or wait, so a refused call leaves no
task behind; the job chains the next task with a callback instead. The refusal holds on every
pool, the green ones included, where celery's own guard against a wait inside a task is off.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from celery import _state
from celery.concurrency import gevent
from celery.result import assert_will_not_block

import tai42_backend_celery.extensions.extensions as extensions
from tai42_backend_celery.core.tasks import tool_execution
from tai42_backend_celery.tools import tools

_REFUSED = "cannot wait for another job's result from inside a job: chain it with a callback instead"


def sample_tool(text: str) -> str:
    """Echo ``text``."""
    return text


class _FakeAsyncResult:
    """A finished task whose result is ``done``."""

    id = "task-1"
    status = "SUCCESS"

    def ready(self) -> bool:
        return True

    def get(self, timeout: float | None = None) -> Any:
        return "done"


class _FakeTask:
    """Records each dispatch and answers it with the finished task."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def apply_async(self, args: Any = None, kwargs: Any = None, **opts: Any) -> _FakeAsyncResult:
        self.calls.append(kwargs)
        return _FakeAsyncResult()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeTask:
    task = _FakeTask()
    monkeypatch.setattr(extensions, "tool_execution", task)
    monkeypatch.setattr(tools, "AsyncResult", lambda task_id, app=None: _FakeAsyncResult())
    return task


async def _sync_task() -> Any:
    return await extensions.sync_task(sample_tool, "sample_tool", "doc")(text="hi")


async def _task_result() -> Any:
    return tools.backend_task_result("task-1", timeout=5)


_WAITS: dict[str, Callable[[], Any]] = {
    "sample_tool_sync_task": _sync_task,
    "backend_task_result": _task_result,
}


def _in_job(call: Callable[[], Any]) -> Any:
    """Run ``call`` as the body of a job of the tool-execution task."""
    try:
        return tool_execution.run_async(call())
    finally:
        tool_execution.close_loop()


@pytest.mark.parametrize("name", list(_WAITS))
def test_a_wait_tool_refuses_inside_a_job(fake: _FakeTask, name: str) -> None:
    with pytest.raises(RuntimeError) as refused:
        _in_job(_WAITS[name])

    assert str(refused.value) == f"{name} {_REFUSED}"
    assert fake.calls == []


@pytest.mark.parametrize("name", list(_WAITS))
async def test_a_wait_tool_returns_the_result_outside_a_job(fake: _FakeTask, name: str) -> None:
    assert await _WAITS[name]() == "done"


def test_a_job_reads_another_jobs_finished_result_without_waiting(fake: _FakeTask) -> None:
    """With no ``timeout`` the tool only reads the result's current state, which a job may do."""

    async def read() -> Any:
        return tools.backend_task_result("task-1")

    assert _in_job(read) == "done"


def test_a_wait_tool_refuses_inside_a_job_with_celerys_own_guard_off(
    fake: _FakeTask, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The green pools (``gevent``, ``eventlet``) turn celery's own guard off; the job still may not wait."""
    monkeypatch.setattr(_state, "_task_join_will_block", gevent.TaskPool.task_join_will_block)
    assert_will_not_block()

    for name, call in _WAITS.items():
        with pytest.raises(RuntimeError) as refused:
            _in_job(call)
        assert str(refused.value) == f"{name} {_REFUSED}"
    assert fake.calls == []
