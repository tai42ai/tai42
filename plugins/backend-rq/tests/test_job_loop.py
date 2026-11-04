"""How a job's coroutine reaches an event loop: a fresh loop of its own, or the host's running loop."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from typing import Any, cast

import pytest
from redis import Redis
from rq.timeouts import BaseDeathPenalty, JobTimeoutException, TimerDeathPenalty, UnixSignalDeathPenalty
from tai42_kit.backend import private_loop

from tai42_backend_rq import worker_pools
from tai42_backend_rq.job_loop import HostLoopJob, PrivateLoopJob

_seen: dict[str, Any] = {}


async def _reports_its_loop() -> str:
    _seen["loop"] = asyncio.get_running_loop()
    _seen["thread"] = threading.current_thread().name
    return "ran"


async def _raises() -> None:
    _seen["loop"] = asyncio.get_running_loop()
    raise ValueError("raised by the job")


def _sync() -> str:
    return "sync"


def _job(job_class: type[PrivateLoopJob | HostLoopJob], func: Any) -> Any:
    return job_class.create(func, connection=Redis())


@pytest.fixture(autouse=True)
def _fresh_seen() -> None:
    _seen.clear()


@pytest.fixture
def released_on(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.AbstractEventLoop]:
    """The loops the kit's loop-bound release ran on."""
    loops: list[asyncio.AbstractEventLoop] = []

    async def _release() -> None:
        loops.append(asyncio.get_running_loop())

    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _release)
    return loops


# What ran as a private loop's job ended, in order, each with the loop it ran on.
_steps: list[tuple[str, asyncio.AbstractEventLoop]] = []


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, asyncio.AbstractEventLoop]]]:
    """What ran as a job ended, in order, each with the loop it ran on: the job's cancel and cleanup, the release."""

    async def _release() -> None:
        _steps.append(("release", asyncio.get_running_loop()))

    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _release)
    _steps.clear()
    yield _steps
    _steps.clear()


_BUDGET_SECONDS = 1
# How far past its budget a job's failure may come: the job's own cancel and cleanup, and the release.
_MARGIN_SECONDS = 1.0


async def _waits_on() -> None:
    """Wait on, recording the job's cancel and its cleanup."""
    loop = asyncio.get_running_loop()
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        _steps.append(("cancelled", loop))
        raise
    finally:
        _steps.append(("cleanup", loop))


def _wait_in_python_code(real_select: Callable[[float | None], Any]) -> Callable[[float | None], Any]:
    """The loop's wait for its I/O, spent in Python code before the real wait returns at once."""

    def _select(timeout: float | None = None) -> Any:
        until = time.monotonic() + (0.05 if timeout is None else min(timeout, 0.05))
        while time.monotonic() < until:
            time.sleep(0.005)
        return real_select(0)

    return _select


async def _waits_on_in_the_loop_s_own_code() -> None:
    """Wait on while the loop's thread spends its wait for I/O in Python code.

    rq's ``solo`` timer raises its exception into the job's thread, where it lands at the next
    Python instruction; the loop's wait here is Python code of the loop's own, outside any task or
    callback, as a step of the loop's own is once it returns from the system's wait.
    """
    selector = cast(Any, asyncio.get_running_loop())._selector
    selector.select = _wait_in_python_code(selector.select)
    await _waits_on()


@pytest.mark.parametrize(
    ("penalty", "job"),
    [(UnixSignalDeathPenalty, _waits_on), (TimerDeathPenalty, _waits_on_in_the_loop_s_own_code)],
    ids=["prefork signal", "solo timer"],
)
def test_a_private_loop_job_past_its_budget_is_cancelled_and_ends_before_the_release_on_its_loop(
    penalty: type[BaseDeathPenalty],
    job: Callable[[], Coroutine[Any, Any, None]],
    steps: list[tuple[str, asyncio.AbstractEventLoop]],
) -> None:
    started = time.monotonic()
    with pytest.raises(JobTimeoutException), penalty(_BUDGET_SECONDS, JobTimeoutException):
        _job(PrivateLoopJob, job)._execute()
    elapsed = time.monotonic() - started

    [(_, loop), *_] = steps
    assert steps == [("cancelled", loop), ("cleanup", loop), ("release", loop)]
    assert loop.is_closed()
    assert elapsed < _BUDGET_SECONDS + _MARGIN_SECONDS, f"the job failed {elapsed:.1f}s in, past its budget"


async def _awaits_a_thread() -> None:
    await asyncio.to_thread(time.sleep, 4)


def test_a_private_loop_job_past_its_budget_fails_within_it_while_a_thread_it_awaits_runs_on(
    steps: list[tuple[str, asyncio.AbstractEventLoop]],
) -> None:
    """A thread cannot be cancelled: the job ends without waiting on it, and the thread ends on its own."""
    started = time.monotonic()
    with pytest.raises(JobTimeoutException), UnixSignalDeathPenalty(_BUDGET_SECONDS, JobTimeoutException):
        _job(PrivateLoopJob, _awaits_a_thread)._execute()
    elapsed = time.monotonic() - started

    assert [step for step, _ in steps] == ["release"]
    assert elapsed < _BUDGET_SECONDS + _MARGIN_SECONDS, f"the job failed {elapsed:.1f}s in, past its budget"


@pytest.fixture
def host_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A loop the host runs on a thread of its own, as it runs its loop beside the gevent pool's work loop."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="host loop", daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def test_a_private_loop_job_runs_its_coroutine_and_releases_the_kit_s_resources_on_that_loop_before_it_closes(
    released_on: list[asyncio.AbstractEventLoop],
) -> None:
    assert _job(PrivateLoopJob, _reports_its_loop)._execute() == "ran"

    assert released_on == [_seen["loop"]]
    assert _seen["loop"].is_closed()


def test_a_private_loop_job_whose_coroutine_raises_still_releases_and_raises_the_error(
    released_on: list[asyncio.AbstractEventLoop],
) -> None:
    with pytest.raises(ValueError, match="raised by the job"):
        _job(PrivateLoopJob, _raises)._execute()

    assert released_on == [_seen["loop"]]
    assert _seen["loop"].is_closed()


def test_a_failed_release_chains_onto_the_job_s_own_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fails() -> None:
        raise OSError("release failed")

    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _fails)

    with pytest.raises(OSError, match="release failed") as raised:
        _job(PrivateLoopJob, _raises)._execute()
    assert isinstance(raised.value.__context__, ValueError)


def _current_loop() -> asyncio.AbstractEventLoop | None:
    """The thread's current event loop, the one ``asyncio.set_event_loop`` set, or ``None`` when none is."""
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        return None


async def _reports_the_current_loop() -> bool:
    return _current_loop() is asyncio.get_running_loop()


def test_a_private_loop_job_s_loop_is_its_thread_s_current_loop_while_it_runs_and_the_thread_has_none_after(
    released_on: list[asyncio.AbstractEventLoop],
) -> None:
    seen: list[object] = []

    def _performs_the_job() -> None:
        seen.append(_job(PrivateLoopJob, _reports_the_current_loop)._execute())
        seen.append(_current_loop())

    thread = threading.Thread(target=_performs_the_job)
    thread.start()
    thread.join(5)

    assert seen == [True, None]


@pytest.mark.parametrize("job_class", [PrivateLoopJob, HostLoopJob])
def test_a_sync_job_function_returns_its_value_with_no_loop(
    job_class: type[PrivateLoopJob | HostLoopJob], released_on: list[asyncio.AbstractEventLoop]
) -> None:
    assert _job(job_class, _sync)._execute() == "sync"
    assert released_on == []


def test_a_host_loop_job_runs_its_coroutine_on_the_handed_loop(
    host_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch, released_on: list[asyncio.AbstractEventLoop]
) -> None:
    monkeypatch.setattr(HostLoopJob, "_shared_loop", None)
    HostLoopJob.share_loop(host_loop)

    assert _job(HostLoopJob, _reports_its_loop)._execute() == "ran"

    assert _seen["loop"] is host_loop
    assert _seen["thread"] == "host loop"
    assert released_on == [], "a job released the host loop's kit resources"
    assert not host_loop.is_closed()


def test_a_host_loop_job_with_no_handed_loop_raises_and_closes_its_coroutine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HostLoopJob, "_shared_loop", None)
    coroutines: list[Any] = []

    async def _never_runs() -> None:
        raise AssertionError("the coroutine ran")

    def _makes_a_coroutine() -> Any:
        coroutines.append(_never_runs())
        return coroutines[0]

    job = _job(HostLoopJob, _sync)
    monkeypatch.setattr(HostLoopJob, "func", property(lambda self: _makes_a_coroutine))

    with pytest.raises(RuntimeError, match="no host event loop was handed to the gevent pool's jobs"):
        job._execute()
    assert inspect.getcoroutinestate(coroutines[0]) == inspect.CORO_CLOSED


def test_each_worker_class_declares_the_job_class_of_its_pool() -> None:
    assert worker_pools.CustomRQWorker.job_class is PrivateLoopJob
    assert worker_pools.CustomRQSimpleWorker.job_class is PrivateLoopJob
    assert worker_pools.CustomRQGeventWorker.job_class is HostLoopJob
