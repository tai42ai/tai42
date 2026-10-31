"""The arq jobs that run manifest code hold the process's fork-gate job span for their whole run.

Jobs run as coroutines on the loop a config reload rebuilds on, and a sync step a job hands
to ``asyncio.to_thread`` keeps running on its worker thread while the loop is busy, so the
span is what keeps ``callback_job`` and ``tool_execution`` out of a re-import: a rebuild
waits for them, and one that arrives while a rebuild is pending starts after it, without
holding the loop. The schedule driver ``task_scheduler`` runs platform code only and waits
for the target it enqueued, so it holds no span and a rebuild never waits on it.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from tai42_kit.fork_gate import fork_gate

from tai42_backend_arq import worker
from tai42_backend_arq.worker import ArqWorkerRuntime

_FUNCTIONS = ("task_scheduler", "callback_job", "tool_execution")
_SPANNED = ("callback_job", "tool_execution")
_ARRIVES = 5.0
_STAYS_CLEAR = 0.3
_REBUILD_BUDGET = 30.0


class _BuiltWorker:
    """Records the job functions the runtime builds its arq worker with."""

    def __init__(self, **kwargs: Any) -> None:
        self.functions = {function.name: function for function in kwargs["functions"]}


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[Any]]]:
    """Each job function body replaced by one that records what it saw, keyed by name."""
    seen: dict[str, list[Any]] = {name: [] for name in _FUNCTIONS}

    def recorder(name: str) -> Any:
        async def body(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            seen[name].append(fork_gate.live_spans)
            step = kwargs.get("step")
            if step is not None:
                await asyncio.to_thread(step)
            return name

        body.__qualname__ = name
        return body

    for name in _FUNCTIONS:
        monkeypatch.setattr(worker, name, recorder(name))
    monkeypatch.setattr(worker, "_ProgressReportingWorker", _BuiltWorker)
    assert fork_gate.live_spans == 0
    yield seen
    assert fork_gate.live_spans == 0


async def _functions() -> dict[str, Any]:
    runtime = ArqWorkerRuntime.from_args([])
    await runtime.build()
    built = runtime._worker
    assert isinstance(built, _BuiltWorker)
    return built.functions


@pytest.mark.parametrize("name", _SPANNED)
async def test_each_job_runs_inside_a_job_span(jobs: dict[str, list[Any]], name: str) -> None:
    functions = await _functions()

    assert await functions[name].coroutine({"job_try": 1}) == name

    assert jobs[name] == [1]


async def test_task_scheduler_runs_outside_a_job_span(jobs: dict[str, list[Any]]) -> None:
    functions = await _functions()

    assert await functions["task_scheduler"].coroutine({}) == "task_scheduler"

    assert jobs["task_scheduler"] == [0]


async def test_a_rebuild_does_not_wait_on_a_schedule_waiting_for_its_target(
    jobs: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A schedule that enqueued its target and waits for the result while a rebuild is
    pending: the rebuild enters at once, and the target starts after it."""
    built: dict[str, Any] = {}
    driving = threading.Event()
    target_waits = threading.Event()
    rebuilt = threading.Event()
    target_ran_during_rebuild: list[bool] = []

    async def drive_schedule(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        driving.set()
        while not fork_gate.blocked:
            await asyncio.sleep(0.01)
        # arq runs the target as a job of its own, started from no caller's context.
        target = asyncio.create_task(built["tool_execution"].coroutine({"job_try": 1}), context=contextvars.Context())
        await asyncio.sleep(_STAYS_CLEAR / 3)
        target_waits.set()
        return await target

    def rebuild() -> None:
        with fork_gate.exclusive(timeout=_REBUILD_BUDGET):
            rebuilt.set()
            assert target_waits.wait(_ARRIVES)
            target_ran_during_rebuild.append(bool(jobs["tool_execution"]))

    drive_schedule.__qualname__ = "task_scheduler"
    monkeypatch.setattr(worker, "task_scheduler", drive_schedule)
    built.update(await _functions())
    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        schedule = asyncio.create_task(built["task_scheduler"].coroutine({}))
        assert await asyncio.to_thread(driving.wait, _ARRIVES)
        rebuilder = threading.Thread(target=rebuild, daemon=True)
        rebuilder.start()
        assert await asyncio.to_thread(rebuilt.wait, _STAYS_CLEAR)
        assert await asyncio.wait_for(schedule, _ARRIVES) == "tool_execution"
        await asyncio.to_thread(rebuilder.join, _ARRIVES)

    assert target_ran_during_rebuild == [False]
    assert jobs["tool_execution"] == [1]
    assert not caplog.records


async def test_a_rebuild_waits_for_a_job_whose_sync_step_runs_off_the_loop(jobs: dict[str, list[Any]]) -> None:
    functions = await _functions()
    step_started = threading.Event()
    step_may_end = threading.Event()
    rebuilt = threading.Event()

    def step() -> None:
        step_started.set()
        assert step_may_end.wait(_ARRIVES)

    def rebuild() -> None:
        with fork_gate.exclusive(timeout=_REBUILD_BUDGET):
            rebuilt.set()

    job = asyncio.create_task(functions["tool_execution"].coroutine({"job_try": 1}, step=step))
    assert await asyncio.to_thread(step_started.wait, _ARRIVES)
    rebuilder = threading.Thread(target=rebuild, daemon=True)
    rebuilder.start()
    try:
        assert not await asyncio.to_thread(rebuilt.wait, _STAYS_CLEAR)
    finally:
        step_may_end.set()
    assert await asyncio.wait_for(job, _ARRIVES) == "tool_execution"
    assert await asyncio.to_thread(rebuilt.wait, _ARRIVES)
    await asyncio.to_thread(rebuilder.join, _ARRIVES)


async def test_a_job_that_arrives_while_a_rebuild_holds_the_gate_starts_after_it(
    jobs: dict[str, list[Any]],
) -> None:
    functions = await _functions()
    held = threading.Event()
    release = threading.Event()

    def rebuild() -> None:
        with fork_gate.exclusive(timeout=_ARRIVES):
            held.set()
            release.wait(_ARRIVES)

    rebuilder = threading.Thread(target=rebuild, daemon=True)
    rebuilder.start()
    assert await asyncio.to_thread(held.wait, _ARRIVES)
    job = asyncio.create_task(functions["tool_execution"].coroutine({"job_try": 1}))
    turns = 0
    deadline = time.monotonic() + _STAYS_CLEAR
    while time.monotonic() < deadline:
        await asyncio.sleep(0.01)
        turns += 1
    assert turns > 1
    assert jobs["tool_execution"] == []
    release.set()
    assert await asyncio.wait_for(job, _ARRIVES) == "tool_execution"
    assert jobs["tool_execution"] == [1]
    await asyncio.to_thread(rebuilder.join, _ARRIVES)
