"""The host's running event loop shared with jobs on other threads: each hands its coroutine there and waits.

The jobs here are plain threads beside a loop the host runs on a thread of its own.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import logging
import threading
import time
from collections.abc import Coroutine, Iterator
from typing import Any

import pytest

from tai42_kit.backend import SharedLoop

_MARK: contextvars.ContextVar[str | None] = contextvars.ContextVar("_MARK", default=None)


async def _sleep_then(seconds: float, value: object = None) -> object:
    await asyncio.sleep(seconds)
    return value


@pytest.fixture
def host_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A loop the host runs on a thread of its own, beside the jobs that hand it their coroutines."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="host loop", daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def _on(loop: asyncio.AbstractEventLoop, coroutine: Coroutine[Any, Any, Any]) -> Any:
    """Run ``coroutine`` on the host's ``loop`` itself, not as a job; return its result."""
    return asyncio.run_coroutine_threadsafe(coroutine, loop).result(5)


def test_the_shared_loop_runs_the_coroutine_on_the_host_s_loop_and_returns_its_result(
    host_loop: asyncio.AbstractEventLoop,
) -> None:
    async def _where() -> tuple[bool, str]:
        return asyncio.get_running_loop() is host_loop, threading.current_thread().name

    assert SharedLoop(host_loop).run(_where()) == (True, "host loop")


def test_the_shared_loop_raises_the_coroutine_s_exception(host_loop: asyncio.AbstractEventLoop) -> None:
    async def _fails() -> None:
        raise ValueError("raised by the coroutine")

    with pytest.raises(ValueError, match="raised by the coroutine"):
        SharedLoop(host_loop).run(_fails())


def test_jobs_at_once_overlap_on_the_shared_loop(host_loop: asyncio.AbstractEventLoop) -> None:
    loops = SharedLoop(host_loop)
    outcomes: dict[int, object] = {}

    def _job(n: int) -> None:
        outcomes[n] = loops.run(_sleep_then(0.3, n))

    started = time.monotonic()
    jobs = [threading.Thread(target=_job, args=(n,)) for n in range(2)]
    for job in jobs:
        job.start()
    for job in jobs:
        job.join(5)

    assert outcomes == {0: 0, 1: 1}
    assert time.monotonic() - started < 0.55, "the two jobs ran one after the other"


def test_the_coroutine_runs_in_the_context_of_the_job_that_handed_it(host_loop: asyncio.AbstractEventLoop) -> None:
    async def _mark() -> str | None:
        return _MARK.get()

    token = _MARK.set("the job's mark")
    try:
        assert SharedLoop(host_loop).run(_mark()) == "the job's mark"
    finally:
        _MARK.reset(token)
    assert _on(host_loop, _mark()) is None, "the job's context leaked into the host's"


def test_a_job_whose_task_the_host_cancels_raises_the_cancel(host_loop: asyncio.AbstractEventLoop) -> None:
    started = threading.Event()

    async def _held() -> None:
        started.set()
        await asyncio.sleep(5)

    async def _cancel_every_task_but_this_one() -> None:
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    def _cancel_once_started() -> None:
        assert started.wait(5)
        _on(host_loop, _cancel_every_task_but_this_one())

    canceller = threading.Thread(target=_cancel_once_started)
    canceller.start()
    with pytest.raises(concurrent.futures.CancelledError):
        SharedLoop(host_loop).run(_held())
    canceller.join(5)


class _Kill(BaseException):
    """An interrupt a pool throws into a job's wait (its time limit, a revoke)."""


def _interrupt_the_wait(monkeypatch: pytest.MonkeyPatch, *interrupts: tuple[BaseException, threading.Event]) -> None:
    """Raise each interrupt in turn out of the job's wait once its event is set, as a pool ends a wait."""
    wait = concurrent.futures.wait
    left = list(interrupts)

    def _interrupted(futures: Any, *args: Any, **kwargs: Any) -> Any:
        if left:
            interrupt, when = left.pop(0)
            assert when.wait(5)
            raise interrupt
        return wait(futures, *args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "wait", _interrupted)


def test_an_interrupted_wait_cancels_the_job_s_task_waits_for_its_cleanup_and_raises_the_interrupt(
    host_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    steps: list[str] = []

    async def _cleans_up() -> None:
        started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(0.1)
            steps.append("cleaned up")
            raise

    _interrupt_the_wait(monkeypatch, (_Kill("time limit"), started))
    with pytest.raises(_Kill, match="time limit") as raised:
        SharedLoop(host_loop).run(_cleans_up())

    assert steps == ["cleaned up"], "the job raised before its coroutine had ended"
    assert raised.value.__context__ is None


def test_a_wait_interrupted_again_cancels_again_and_raises_the_last_chained_to_the_first(
    host_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, first_cancel = threading.Event(), threading.Event()
    cancels: list[int] = []

    async def _swallows_two_cancels() -> str:
        started.set()
        while len(cancels) < 2:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancels.append(len(cancels))
                first_cancel.set()
        return "returned"

    _interrupt_the_wait(monkeypatch, (_Kill("time limit"), started), (_Kill("revoke"), first_cancel))
    with pytest.raises(_Kill, match="revoke") as raised:
        SharedLoop(host_loop).run(_swallows_two_cancels())

    assert cancels == [0, 1]
    assert isinstance(raised.value.__context__, _Kill)
    assert str(raised.value.__context__) == "time limit"


def test_an_error_the_job_ends_with_while_its_wait_was_interrupted_is_logged(
    host_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started = threading.Event()

    async def _fails_in_cleanup() -> None:
        started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise OSError("cleanup failed") from None

    _interrupt_the_wait(monkeypatch, (_Kill("revoke"), started))
    with (
        caplog.at_level(logging.ERROR, logger="tai42_kit.backend.shared_loop"),
        pytest.raises(_Kill, match="revoke"),
    ):
        SharedLoop(host_loop).run(_fails_in_cleanup())

    [record] = [record for record in caplog.records if record.name == "tai42_kit.backend.shared_loop"]
    assert record.getMessage() == (
        "the job ended with this error while its wait was interrupted; the job raises _Kill('revoke') instead"
    )
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "cleanup failed"


def test_a_job_whose_task_cannot_be_made_raises_the_error_and_closes_its_coroutine(
    host_loop: asyncio.AbstractEventLoop,
) -> None:
    def _refuses(loop: asyncio.AbstractEventLoop, coroutine: Coroutine[Any, Any, Any], **kwargs: Any) -> Any:
        raise RuntimeError("no task made")

    host_loop.call_soon_threadsafe(host_loop.set_task_factory, _refuses)
    coroutine = _sleep_then(0, "ran")
    with pytest.raises(RuntimeError, match="no task made"):
        SharedLoop(host_loop).run(coroutine)

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


def test_close_refuses_every_later_job_closes_its_coroutine_and_leaves_the_host_s_loop_running(
    host_loop: asyncio.AbstractEventLoop,
) -> None:
    loops = SharedLoop(host_loop)
    loops.close()
    coroutine = _sleep_then(0, "ran")

    with pytest.raises(RuntimeError, match=r"^the jobs' shared event loop was let go; it runs no further job$"):
        loops.run(coroutine)

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert _on(host_loop, _sleep_then(0, "the host's loop runs on")) == "the host's loop runs on"
    assert not host_loop.is_closed()


def test_a_job_handed_once_the_host_s_loop_has_closed_raises_and_closes_its_coroutine() -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    coroutine = _sleep_then(0, "ran")

    with pytest.raises(RuntimeError, match="Event loop is closed"):
        SharedLoop(loop).run(coroutine)

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
