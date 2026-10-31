"""A fork made in a context emptied of its caller still belongs to the rebuild or the job span it runs under.

The re-import's steps run in contexts emptied of the driving request — the serving-app step
(``_outside_any_request``), the off-loop runner (``run_blocking``) and the deferred tail of an
engaged generation's retire — and the process's fork
gate must still recognise a fork there as the rebuild's own, never as a job racing it. The
off-loop runner called inside a job keeps that job's span the same way: a fork there nests in
the open span instead of queueing behind a rebuild that waits for the span to close.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import time
from collections.abc import Iterator

import pytest
from tai42_kit.fork_gate import fork_gate

from tai42_skeleton.app import epoch_retire
from tai42_skeleton.app.epoch import Epoch, _outside_any_request
from tai42_skeleton.app.lifecycle.off_loop import run_blocking

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")

# A guarded fork that waited on its own rebuild would take this long; one that does not, milliseconds.
_GUARD_WAIT = 30.0
_PROMPT = 3.0

_request_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_scope", default=None)


@pytest.fixture
def guarded() -> Iterator[None]:
    """Guard the process's forks for one test; the fork hooks stay registered but do nothing after it."""
    fork_gate.guard_forks(timeout=_GUARD_WAIT, tick=lambda: None)
    yield
    fork_gate._fork_hooks.guard = None


def _fork_and_time() -> float:
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return time.monotonic() - started


@pytest.mark.usefixtures("guarded")
async def test_a_fork_in_the_serving_app_step_does_not_wait_on_its_rebuild(caplog: pytest.LogCaptureFixture) -> None:
    _request_scope.set("the driving request")

    async def step() -> float:
        assert _request_scope.get() is None
        return _fork_and_time()

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with fork_gate.exclusive_async(timeout=_PROMPT):
            waited = await _outside_any_request(step)

    assert waited < _PROMPT
    assert "could not enter" not in caplog.text


@pytest.mark.usefixtures("guarded")
async def test_a_fork_in_the_off_loop_runner_does_not_wait_on_its_rebuild(caplog: pytest.LogCaptureFixture) -> None:
    async def step() -> float:
        return _fork_and_time()

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with fork_gate.exclusive_async(timeout=_PROMPT):
            waited = await asyncio.to_thread(run_blocking, step)

    assert waited < _PROMPT
    assert "could not enter" not in caplog.text


@pytest.mark.usefixtures("guarded")
def test_a_fork_in_the_off_loop_runner_inside_a_job_span_does_not_wait_behind_a_pending_rebuild(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def step() -> float:
        return _fork_and_time()

    def rebuild() -> None:
        with fork_gate.exclusive(timeout=_GUARD_WAIT):
            pass

    rebuilder = threading.Thread(target=rebuild, daemon=True)
    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        with fork_gate.job_span(timeout=_PROMPT):
            rebuilder.start()
            deadline = time.monotonic() + _PROMPT
            while not fork_gate.blocked:
                assert time.monotonic() < deadline, "the rebuild never registered"
                time.sleep(0.01)
            waited = run_blocking(step)
        rebuilder.join(timeout=_GUARD_WAIT)

    assert not rebuilder.is_alive()
    assert waited < _PROMPT
    assert "could not" not in caplog.text


@pytest.mark.usefixtures("guarded")
async def test_a_fork_in_the_deferred_retire_tail_does_not_wait_on_its_rebuild(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _request_scope.set("the driving request")
    forked: list[float] = []

    async def tail(old: Epoch, retired: int, budget: float) -> None:
        assert _request_scope.get() is None
        forked.append(_fork_and_time())

    async def no_pool_drain(epoch: int, deadline: float) -> None:
        return None

    monkeypatch.setattr(epoch_retire, "_deferred_drain_and_close", tail)
    monkeypatch.setattr(epoch_retire, "drain_epoch", no_pool_drain)
    old = Epoch(number=7)
    driver = asyncio.current_task()
    assert driver is not None
    old.driving_tasks.add(driver)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with fork_gate.exclusive_async(timeout=_PROMPT):
            await epoch_retire.retire_epoch(old, 7, 1.0)
            (deferred,) = epoch_retire.deferred_retires()
            await deferred

    (waited,) = forked
    assert waited < _PROMPT
    assert "could not enter" not in caplog.text


async def test_a_task_the_serving_app_step_starts_is_not_the_work_of_the_job_that_drove_the_rebuild() -> None:
    """A long-lived task the step creates (a lifespan supervisor, a periodic loop) outlives the
    rebuild and the job that drove it: once the rebuild has left, a job span it enters is a span
    of its own, not one nested in the driving job's."""
    later = asyncio.Event()

    async def outlives() -> int:
        await later.wait()
        async with fork_gate.job_span_async(timeout=_PROMPT):
            return fork_gate.live_spans

    async def step() -> asyncio.Task[int]:
        return asyncio.create_task(outlives())

    async with fork_gate.job_span_async(timeout=_PROMPT):
        async with fork_gate.exclusive_async(timeout=_PROMPT):
            task = await _outside_any_request(step)
        later.set()
        assert await asyncio.wait_for(task, _PROMPT) == 2


async def test_the_deferred_retire_tail_of_a_rebuild_a_job_drove_is_not_that_jobs_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferred tail outlives the rebuild and the job that drove it: a wait it parks leaves
    the driving job's span counted, and a job span it enters is a span of its own."""
    later = asyncio.Event()
    seen: dict[str, int] = {}

    async def tail(old: Epoch, retired: int, budget: float) -> None:
        await later.wait()
        async with fork_gate.parked_span(timeout=_PROMPT):
            seen["parked"] = fork_gate.live_spans
        async with fork_gate.job_span_async(timeout=_PROMPT):
            seen["own span"] = fork_gate.live_spans

    async def no_pool_drain(epoch: int, deadline: float) -> None:
        return None

    monkeypatch.setattr(epoch_retire, "_deferred_drain_and_close", tail)
    monkeypatch.setattr(epoch_retire, "drain_epoch", no_pool_drain)
    old = Epoch(number=8)
    driver = asyncio.current_task()
    assert driver is not None
    old.driving_tasks.add(driver)

    async with fork_gate.job_span_async(timeout=_PROMPT):
        async with fork_gate.exclusive_async(timeout=_PROMPT):
            await epoch_retire.retire_epoch(old, 8, 1.0)
            (deferred,) = epoch_retire.deferred_retires()
        later.set()
        await asyncio.wait_for(deferred, _PROMPT)

    assert seen == {"parked": 1, "own span": 2}
