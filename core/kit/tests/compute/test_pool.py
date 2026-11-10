"""``ComputePool`` — jobs run in spawned worker processes and are awaited without holding the loop."""

from __future__ import annotations

import asyncio
import importlib
import operator
import os
import re
import signal
import threading
import time
from collections.abc import Iterator
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest
from pebble import ProcessFuture, ProcessPool

from tai42_kit.compute import (
    ComputeJobFailedError,
    ComputePool,
    ComputePoolUnavailableError,
    ComputeWorkerLostError,
    WorkerTraceback,
)

from .heartbeat import GIL_HOLDING_PATTERN, GIL_HOLDING_SUBJECT, tick_ratio

_WORKER_MODULES = Path(__file__).parent / "worker_modules"


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.fixture
def pool() -> Iterator[ComputePool]:
    pool = ComputePool(1)
    pool.start()
    yield pool
    pool.stop()


@pytest.fixture
def two_worker_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[ComputePool]:
    """A pool whose workers, like the caller, import the job modules in ``worker_modules`` by name."""
    monkeypatch.syspath_prepend(str(_WORKER_MODULES))  # a spawned worker starts with the caller's path
    pool = ComputePool(2)
    pool.start()
    yield pool
    pool.stop()


async def test_a_gil_holding_job_leaves_the_loop_ticking(pool: ComputePool) -> None:
    await pool.run(os.getpid)  # the worker has imported what the job needs

    match, ratio = await tick_ratio(pool.run(re.match, GIL_HOLDING_PATTERN, GIL_HOLDING_SUBJECT))

    assert match is None
    assert ratio >= 0.8


async def test_the_same_job_on_the_loop_starves_the_heartbeat() -> None:
    async def inline() -> re.Match[str] | None:
        return re.match(GIL_HOLDING_PATTERN, GIL_HOLDING_SUBJECT)

    _, ratio = await tick_ratio(inline())

    assert ratio < 0.8


async def test_a_job_returns_its_result(pool: ComputePool) -> None:
    assert await pool.run(operator.add, 2, 3) == 5


async def test_the_jobs_exception_is_raised_as_itself_with_the_worker_traceback(pool: ComputePool) -> None:
    with pytest.raises(ZeroDivisionError) as raised:
        await pool.run(operator.truediv, 1, 0)

    assert isinstance(raised.value.__cause__, WorkerTraceback)
    assert "ZeroDivisionError" in str(raised.value.__cause__)


@pytest.mark.parametrize(
    ("module", "error"),
    [("attribute_error_at_import_in_a_worker", AttributeError), ("runtime_error_at_import_in_a_worker", RuntimeError)],
)
async def test_a_job_whose_module_fails_to_import_in_the_worker_raises_that_error(
    two_worker_pool: ComputePool, module: str, error: type[Exception]
) -> None:
    job = importlib.import_module(module).job

    started = time.monotonic()
    with pytest.raises(error, match="no app bound"):
        await asyncio.wait_for(two_worker_pool.run(job, timeout=2), 10)
    assert time.monotonic() - started < 2

    sleeping = asyncio.ensure_future(two_worker_pool.run(time.sleep, 1))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    with pytest.raises(error, match="no app bound"):
        await asyncio.wait_for(two_worker_pool.run(job, timeout=2), 10)
    assert time.monotonic() - started < 2

    assert await sleeping is None
    assert not two_worker_pool.broken
    assert await two_worker_pool.run(operator.add, 1, 1) == 2


async def test_a_jobs_exception_that_cannot_be_rebuilt_raises_compute_job_failed_error(
    two_worker_pool: ComputePool,
) -> None:
    raise_two_arg = importlib.import_module("exception_that_does_not_rebuild").raise_two_arg
    sleeping = asyncio.ensure_future(two_worker_pool.run(time.sleep, 1))
    await asyncio.sleep(0.3)

    with pytest.raises(ComputeJobFailedError, match="TwoArgError: 7: bad") as raised:
        await asyncio.wait_for(two_worker_pool.run(raise_two_arg), 10)

    cause = raised.value.__cause__
    assert isinstance(cause, WorkerTraceback)
    assert 'raise TwoArgError(7, "bad")' in str(cause)
    assert await sleeping is None
    assert not two_worker_pool.broken


async def test_a_jobs_exception_whose_str_raises_and_does_not_rebuild_raises_compute_job_failed_error(
    two_worker_pool: ComputePool,
) -> None:
    raise_unprintable = importlib.import_module("exception_whose_str_raises_and_does_not_rebuild").raise_unprintable
    sleeping = asyncio.ensure_future(two_worker_pool.run(time.sleep, 1))
    await asyncio.sleep(0.3)

    with pytest.raises(ComputeJobFailedError) as raised:
        await asyncio.wait_for(two_worker_pool.run(raise_unprintable), 10)

    assert (
        "exception_whose_str_raises_and_does_not_rebuild.UnprintableTwoArgError: <str() raised builtins.ValueError>"
        in str(raised.value)
    )
    cause = raised.value.__cause__
    assert isinstance(cause, WorkerTraceback)
    assert 'raise UnprintableTwoArgError(7, "bad")' in str(cause)
    assert await sleeping is None
    assert not two_worker_pool.broken
    assert await two_worker_pool.run(len, "x") == 1


async def test_a_timeout_terminates_the_worker_and_the_pool_answers_the_next_job(pool: ComputePool) -> None:
    first_worker = await pool.run(os.getpid)

    with pytest.raises(TimeoutError, match=r"exceeded its 0\.3s timeout; its worker was terminated"):
        await pool.run(time.sleep, 30, timeout=0.3)

    assert _gone(first_worker)
    next_worker = await pool.run(os.getpid)
    assert next_worker != first_worker
    assert await pool.run(operator.add, 1, 1) == 2


async def test_a_timeout_ends_a_job_running_in_c_code_at_once(pool: ComputePool) -> None:
    first_worker = await pool.run(os.getpid)
    started = time.monotonic()

    # ``sum`` over a range loops in C and never returns to bytecode, where a Python signal handler would run.
    with pytest.raises(TimeoutError, match=r"exceeded its 0\.2s timeout; its worker was terminated"):
        await pool.run(sum, range(1 << 40), timeout=0.2)

    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"the timed-out job answered after {elapsed:.2f}s"
    assert _gone(first_worker)


async def test_cancelling_the_caller_terminates_the_worker(pool: ComputePool) -> None:
    first_worker = await pool.run(os.getpid)
    job = asyncio.ensure_future(pool.run(time.sleep, 30))
    await asyncio.sleep(0.3)

    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job

    assert await pool.run(os.getpid) != first_worker
    assert _gone(first_worker)


async def test_a_worker_that_ends_mid_job_raises_worker_lost_and_is_replaced(pool: ComputePool) -> None:
    with pytest.raises(ComputeWorkerLostError, match="ended with exit code 7"):
        await pool.run(os._exit, 7)

    assert await pool.run(operator.add, 2, 2) == 4


async def test_an_unpicklable_job_raises_in_the_caller(pool: ComputePool) -> None:
    with pytest.raises(AttributeError, match="local object"):
        await pool.run(lambda: 1)


async def test_an_unpicklable_result_raises_in_the_caller(pool: ComputePool) -> None:
    with pytest.raises(TypeError, match="cannot pickle"):
        await pool.run(threading.Lock)


async def test_a_pool_that_is_not_started_raises() -> None:
    with pytest.raises(ComputePoolUnavailableError, match="is not started"):
        await ComputePool(1).run(operator.add, 1, 1)


async def test_a_stopped_pool_raises(pool: ComputePool) -> None:
    pool.stop()

    with pytest.raises(ComputePoolUnavailableError, match="was stopped"):
        await pool.run(operator.add, 1, 1)


async def test_stop_fails_the_jobs_still_running(pool: ComputePool) -> None:
    job = asyncio.ensure_future(pool.run(time.sleep, 30))
    queued = asyncio.ensure_future(pool.run(time.sleep, 30))
    await asyncio.sleep(0.3)

    await asyncio.to_thread(pool.stop)

    for pending in (job, queued):
        with pytest.raises(ComputePoolUnavailableError, match="stopped before the job finished"):
            await pending


async def test_a_pool_broken_by_an_idle_worker_dying_refuses_jobs(pool: ComputePool) -> None:
    os.kill(await pool.run(os.getpid), signal.SIGKILL)

    deadline = time.monotonic() + 10
    while True:
        try:
            await pool.run(operator.add, 1, 1)
        except ComputePoolUnavailableError as exc:
            # A job racing the breakage is aborted ("broke while running"); once broken, the pool refuses jobs.
            if "cannot run" in str(exc):
                break
        assert time.monotonic() < deadline, "the library never reported the pool broken"
        await asyncio.sleep(0.05)
    assert pool.broken


async def test_a_job_aborted_by_a_broken_pool_raises_unavailable(
    pool: ComputePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The library aborts every registered job with BrokenProcessPool when its pool breaks.
    def broken_schedule(*_: object, **__: object) -> ProcessFuture:
        future: ProcessFuture = ProcessFuture()
        future.set_exception(BrokenProcessPool("All workers expired"))
        return future

    monkeypatch.setattr(ProcessPool, "schedule", broken_schedule)

    with pytest.raises(ComputePoolUnavailableError, match="broke while running"):
        await pool.run(operator.add, 1, 1)


def test_a_pool_not_started_running_or_stopped_is_not_broken(pool: ComputePool) -> None:
    assert not ComputePool(1).broken
    assert not pool.broken

    pool.stop()

    assert not pool.broken


def test_start_twice_raises(pool: ComputePool) -> None:
    with pytest.raises(RuntimeError, match="already running"):
        pool.start()


def test_a_stopped_pool_does_not_start_again(pool: ComputePool) -> None:
    pool.stop()

    with pytest.raises(ComputePoolUnavailableError, match="does not start again"):
        pool.start()


def test_a_pool_whose_workers_do_not_start_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProcessPool, "active", property(lambda _: False))

    with pytest.raises(ComputePoolUnavailableError, match="did not start"):
        ComputePool(1).start()


def test_stopping_a_pool_that_never_started_marks_it_stopped() -> None:
    pool = ComputePool(3)
    pool.stop()

    assert pool.workers == 3
    with pytest.raises(ComputePoolUnavailableError, match="does not start again"):
        pool.start()


def test_fewer_than_one_worker_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one worker"):
        ComputePool(0)
