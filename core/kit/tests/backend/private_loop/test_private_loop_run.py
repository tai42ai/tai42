"""How a job's private event loop runs a job: its result, its exit, an interrupt from outside, its current loop."""

from __future__ import annotations

import asyncio
import errno
import inspect
import logging
import re
import signal
import threading
import time
from collections.abc import Callable
from contextlib import closing
from typing import Any, cast

import pytest

from tai42_kit.backend import PrivateLoop
from tai42_kit.backend.private_loop import Task, _LoopRecord

from .conftest import _LOGGER, _sleep_then


async def _raise(exc: BaseException) -> None:
    raise exc


async def _child_raising(exc: BaseException, how: str) -> None:
    """Await a child task that raises ``exc``, made the way ``how`` names."""
    if how == "gather":
        await asyncio.gather(_raise(exc))
    elif how == "create_task":
        await asyncio.create_task(_raise(exc))
    else:
        async with asyncio.TaskGroup() as group:
            group.create_task(_raise(exc))


_CHILD_TASKS = ["gather", "create_task", "TaskGroup"]


def _raise_from_a_callback(exc: BaseException) -> Callable[[], None]:
    def raises() -> None:
        raise exc

    return raises


class _TimeLimitError(Exception):
    """A time limit a job's pool raises out of the loop from its signal handler."""


async def _stalled_upload() -> None:
    await asyncio.sleep(60)


async def _what_asyncio_shows_of_the_job_and_a_child() -> dict[str, object]:
    """Start an unnamed child task; return the name, coroutine, repr and stack asyncio shows of it and of the job."""
    job = asyncio.current_task()
    assert job is not None
    child = asyncio.create_task(_stalled_upload())
    await asyncio.sleep(0)
    try:
        return {
            "job name": job.get_name(),
            "job coroutine": job.get_coro().__qualname__,  # type: ignore[union-attr]
            "child name": child.get_name(),
            "child coroutine": child.get_coro().__qualname__,  # type: ignore[union-attr]
            "child repr": repr(child),
            "child stack": [frame.f_code.co_name for frame in child.get_stack()],
        }
    finally:
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)


def _a_second_signal_in(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, *, after: BaseException, interrupt: BaseException
) -> Callable[[], None]:
    """Make ``owner.name`` raise ``interrupt`` once, on its first call after the returned callback raised ``after``.

    The callback raises ``after`` in the loop's own code; the patched call stands for the code a
    second signal lands in while the work on the loop ends.
    """
    real = getattr(owner, name)
    happened: set[str] = set()

    def _patched(self: object, *args: Any, **kwargs: Any) -> object:
        if "after" in happened and "interrupt" not in happened:
            happened.add("interrupt")
            raise interrupt
        return real(self, *args, **kwargs)

    def _raises_after() -> None:
        happened.add("after")
        raise after

    monkeypatch.setattr(owner, name, _patched)
    return _raises_after


async def _schedules_an_exit_and_returns(exit_: BaseException) -> str:
    """Leave a callback raising ``exit_`` on the running loop, and return at once, before it runs."""
    asyncio.get_running_loop().call_soon(_raise_from_a_callback(exit_))
    return "done"


def _breaks_the_loop_s_wait() -> None:
    """Make the running loop's wait for its I/O fail on every pass, as a selector whose descriptor is gone."""

    def _select(timeout: float | None = None) -> object:
        raise OSError(errno.EBADF, "Bad file descriptor")

    cast(Any, asyncio.get_running_loop())._selector.select = _select


async def _breaks_its_loop() -> str:
    _breaks_the_loop_s_wait()
    await asyncio.sleep(0.05)
    return "ok"


class _LandsInTheStopError(Exception):
    """An exception a pool raises into the job's thread, landing as the loop is stopped."""


def _current_loop() -> asyncio.AbstractEventLoop | None:
    """The thread's current event loop, the one ``asyncio.set_event_loop`` set, or ``None`` when none is."""
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        return None


def _in_a_thread(work: Callable[[], object]) -> object:
    """Run ``work`` in a thread of its own, which has no current loop yet, and return what it returned or raised."""
    outcome: list[object] = []

    def _runs() -> None:
        try:
            outcome.append(work())
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=_runs, daemon=True)
    thread.start()
    thread.join(5)
    [result] = outcome
    return result


def test_a_closed_private_loop_refuses_a_job_and_closes_its_coroutine(private: PrivateLoop) -> None:
    private.close()
    unrun = _sleep_then(0, 1)

    with pytest.raises(RuntimeError, match="Event loop is closed"):
        private.run(unrun)
    assert inspect.getcoroutinestate(unrun) == inspect.CORO_CLOSED


@pytest.mark.parametrize("how", _CHILD_TASKS)
def test_a_private_loop_raises_a_child_task_s_exit_in_its_job_and_serves_the_next(
    private: PrivateLoop, how: str
) -> None:
    exit_ = SystemExit(2)

    with pytest.raises(SystemExit) as raised:
        private.run(_child_raising(exit_, how))

    assert raised.value is exit_
    assert private.run(_sleep_then(0, "ok")) == "ok"


def test_a_private_loop_job_whose_coroutine_catches_its_child_s_exit_returns_its_value(private: PrivateLoop) -> None:
    async def _catches() -> str:
        try:
            await _child_raising(KeyboardInterrupt(), "gather")
        except KeyboardInterrupt:
            return "caught"
        return "not raised"

    assert private.run(_catches()) == "caught"
    assert private.run(_sleep_then(0, "ok")) == "ok"


def test_an_interrupt_from_outside_a_private_loop_s_tasks_ends_the_job_after_its_cleanup(private: PrivateLoop) -> None:
    """A raise in the loop's own code (a signal handler, a plain callback) is no task's: it ends the job."""
    interrupt = KeyboardInterrupt()
    cleanups: list[str] = []

    async def _interrupted() -> None:
        asyncio.get_running_loop().call_soon(_raise_from_a_callback(interrupt))
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cleanups.append("cleanup")
            raise
        cleanups.append("finished")

    with pytest.raises(KeyboardInterrupt) as raised:
        private.run(_interrupted())

    assert raised.value is interrupt
    assert cleanups == ["cleanup"]
    assert private.run(_sleep_then(0, "ok")) == "ok"


def test_a_time_limit_a_signal_handler_raises_ends_the_job_after_its_cleanup_and_before_the_release(
    steps: list[tuple[str, asyncio.AbstractEventLoop]],
) -> None:
    """The job's task is cancelled and ends before the job raises; the release then runs once, on its loop."""

    def _limit(signum: int, frame: object) -> None:
        raise _TimeLimitError("past the budget")

    async def _overruns() -> None:
        loop = asyncio.get_running_loop()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            steps.append(("cancelled", loop))
            raise
        finally:
            steps.append(("cleanup", loop))

    private = PrivateLoop()
    earlier = signal.signal(signal.SIGALRM, _limit)
    started = time.monotonic()
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.2)
        with pytest.raises(_TimeLimitError), closing(private):
            private.run(_overruns())
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, earlier)
    elapsed = time.monotonic() - started

    loop = private.loop
    assert steps == [("cancelled", loop), ("cleanup", loop), ("release", loop)]
    assert private.closed
    assert elapsed < 1.5, f"the job raised {elapsed:.1f}s in"


def test_an_error_a_private_loop_job_s_cleanup_raises_after_an_interrupt_is_logged(
    private: PrivateLoop, caplog: pytest.LogCaptureFixture
) -> None:
    async def _fails_its_cleanup() -> None:
        asyncio.get_running_loop().call_soon(_raise_from_a_callback(KeyboardInterrupt()))
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise OSError("cleanup failed") from None

    with caplog.at_level(logging.ERROR, logger=_LOGGER), pytest.raises(KeyboardInterrupt):
        private.run(_fails_its_cleanup())

    [record] = [record for record in caplog.records if record.name == _LOGGER]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], OSError)
    assert str(record.exc_info[1]) == "cleanup failed"


def test_an_unnamed_task_on_a_job_loop_is_named_as_asyncio_names_any_task(private: PrivateLoop) -> None:
    shown = private.run(_what_asyncio_shows_of_the_job_and_a_child())

    assert re.fullmatch(r"Task-\d+", str(shown["job name"]))
    assert re.fullmatch(r"Task-\d+", str(shown["child name"]))


def test_a_task_on_a_job_loop_shows_its_own_coroutine_in_its_repr(private: PrivateLoop) -> None:
    shown = private.run(_what_asyncio_shows_of_the_job_and_a_child())

    assert shown["job coroutine"] == "_what_asyncio_shows_of_the_job_and_a_child"
    assert shown["child coroutine"] == "_stalled_upload"
    assert re.match(r"<Task pending name='Task-\d+' coro=<_stalled_upload\(\) running at ", str(shown["child repr"]))


def test_the_stack_of_a_running_task_on_a_job_loop_is_its_coroutine_s(private: PrivateLoop) -> None:
    assert private.run(_what_asyncio_shows_of_the_job_and_a_child())["child stack"] == ["_stalled_upload"]


def test_a_private_loop_whose_task_factory_was_replaced_fails_the_job_and_closes_its_coroutine(
    private: PrivateLoop,
) -> None:
    """The job raises the replaced factory's ``TypeError``; the close raises it too, and closes the loop."""
    private.loop.set_task_factory(None)
    unrun = _sleep_then(0, 1)

    with pytest.raises(TypeError, match="something replaced it"):
        private.run(unrun)

    assert inspect.getcoroutinestate(unrun) == inspect.CORO_CLOSED
    with pytest.raises(TypeError, match="something replaced it"):
        private.close()
    assert private.closed


def test_an_interrupt_while_a_private_loop_tells_whose_the_exit_is_still_ends_the_job_after_its_cleanup(
    private: PrivateLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_, interrupt = SystemExit(7), KeyboardInterrupt("second signal")
    raises_the_exit = _a_second_signal_in(monkeypatch, _LoopRecord, "claims", after=exit_, interrupt=interrupt)
    cleanups: list[str] = []

    async def _interrupted() -> None:
        asyncio.get_running_loop().call_soon(raises_the_exit)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cleanups.append("cleanup")
            raise

    with pytest.raises(KeyboardInterrupt) as raised:
        private.run(_interrupted())

    assert raised.value is interrupt
    assert interrupt.__context__ is exit_
    assert cleanups == ["cleanup"]
    assert private.run(_sleep_then(0, "ok")) == "ok"


def test_a_further_exception_while_a_private_job_s_task_ends_closes_the_loop(
    monkeypatch: pytest.MonkeyPatch, steps: list[tuple[str, asyncio.AbstractEventLoop]]
) -> None:
    """It lands in the cancel: the job raises it, chained to the first; the loop is closed, its task ended."""
    first, again = KeyboardInterrupt("first signal"), KeyboardInterrupt("second signal")
    raises_the_first = _a_second_signal_in(monkeypatch, Task, "cancel", after=first, interrupt=again)
    private = PrivateLoop()

    async def _interrupted() -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon(raises_the_first)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            steps.append(("cleanup", loop))
            raise

    with pytest.raises(KeyboardInterrupt) as raised:
        private.run(_interrupted())

    assert raised.value is again
    assert again.__context__ is first
    assert private.closed
    assert steps == [("release", private.loop), ("cleanup", private.loop)]


def test_an_exit_a_callback_raises_after_a_private_job_returned_ends_the_job_and_leaves_nothing_on_the_loop(
    private: PrivateLoop,
) -> None:
    """The run after the exit runs the loop's ready callbacks to their end, so the next job on the loop runs."""
    exit_ = SystemExit(7)

    with pytest.raises(SystemExit) as raised:
        private.run(_schedules_an_exit_and_returns(exit_))

    assert raised.value is exit_
    assert list(cast(Any, private.loop)._ready) == []
    assert private.run(_sleep_then(0, "ok")) == "ok"


def test_a_child_s_exit_in_the_pass_that_ends_a_private_job_still_lets_the_job_return_its_value(
    private: PrivateLoop,
) -> None:
    """The child wakes in the same pass as the job's stop, right after it; its exit cuts that pass short."""

    exit_ = SystemExit(3)
    children: list[asyncio.Task[None]] = []

    async def _child(job: asyncio.Task[Any]) -> None:
        await job
        raise exit_

    async def _job() -> str:
        job = cast(asyncio.Task[Any], asyncio.current_task())
        children.append(asyncio.get_running_loop().create_task(_child(job)))
        await asyncio.sleep(0)
        return "value"

    outcome: dict[str, object] = {}

    def _runs() -> None:
        try:
            outcome["job"] = private.run(_job())
        except BaseException as exc:
            outcome["job"] = exc

    job = threading.Thread(target=_runs, daemon=True)
    job.start()
    job.join(3)
    hung = job.is_alive()
    if hung:
        private.loop.call_soon_threadsafe(private.loop.stop)
        job.join(3)

    assert not hung, "the job's run did not end after its child's exit"
    assert outcome == {"job": "value"}
    [child] = children
    assert child.exception() is exit_


def test_a_private_job_run_inside_another_loop_raises_the_nested_run_error_once_per_run_and_closes(
    steps: list[tuple[str, asyncio.AbstractEventLoop]],
) -> None:
    """A loop that cannot run at all is run three times (the job, the cancel, the close), then the job raises."""
    inner_loop, outer_loop = PrivateLoop(), PrivateLoop()
    inner = _sleep_then(0, "inner")

    async def _runs_a_job_inside() -> BaseException | None:
        try:
            inner_loop.run(inner)
        except BaseException as exc:
            return exc
        return None

    try:
        raised = outer_loop.run(_runs_a_job_inside())
    finally:
        outer_loop.close()
    # A loop that cannot run cannot end the task it made: the coroutine is closed here, so its task
    # is left as plain asyncio leaves it for the same input.
    inner.close()

    assert isinstance(raised, RuntimeError), raised
    chain: list[BaseException] = []
    link: BaseException | None = raised
    while link is not None:
        chain.append(link)
        link = link.__context__
    assert [(type(exc), str(exc)) for exc in chain] == [
        (RuntimeError, "Cannot run the event loop while another loop is running")
    ] * 3
    assert inner_loop.closed


def test_a_private_loop_that_fails_on_every_run_fails_its_job_at_once_and_closes(
    steps: list[tuple[str, asyncio.AbstractEventLoop]],
) -> None:
    private = PrivateLoop()
    outcomes: dict[str, object] = {}

    def _job() -> None:
        try:
            outcomes["broken"] = private.run(_breaks_its_loop())
        except BaseException as exc:
            outcomes["broken"] = exc

    job = threading.Thread(target=_job, daemon=True)
    job.start()
    job.join(5)

    raised = outcomes.get("broken")
    assert isinstance(raised, OSError), f"the job never ended: {raised!r}"
    assert raised.errno == errno.EBADF
    assert private.closed


def test_a_private_job_that_stops_its_own_loop_raises_and_the_next_job_runs(private: PrivateLoop) -> None:
    async def _stops_the_loop() -> None:
        asyncio.get_running_loop().stop()
        await asyncio.sleep(5)

    with pytest.raises(RuntimeError) as raised:
        private.run(_stops_the_loop())

    assert str(raised.value) == "loop.stop() was called on the job loop before the work it ran had ended"
    assert private.run(_sleep_then(0, "ok")) == "ok"


def test_an_error_a_private_job_s_task_ended_with_before_an_exit_raised_by_no_task_is_logged_as_one_it_had_ended_with(
    private: PrivateLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """The task was never cancelled, so the log says its error came before the exit, not after a cancel."""
    exit_ = SystemExit(7)
    own = ValueError("the coroutine's own error")

    async def _fails() -> None:
        asyncio.get_running_loop().call_soon(_raise_from_a_callback(exit_))
        raise own

    with caplog.at_level(logging.ERROR, logger=_LOGGER), pytest.raises(SystemExit) as raised:
        private.run(_fails())

    assert raised.value is exit_
    [record] = [record for record in caplog.records if record.name == _LOGGER]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == (
        "the job's coroutine had ended with this error before an exception raised by none of its tasks reached "
        "the job; the job raises SystemExit(7) instead"
    )
    assert record.exc_info is not None
    assert record.exc_info[1] is own


def test_an_exception_landing_in_the_stop_that_ends_a_run_is_logged_and_the_run_still_ends(
    private: PrivateLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """asyncio logs an exception raised inside a callback the loop runs; the job's run stops all the same."""
    real_stop = private.loop.stop
    landed = _LandsInTheStopError("past the budget")
    stops: list[str] = []

    def _stop() -> None:
        if not stops:
            stops.append("landed")
            raise landed
        stops.append("stopped")
        real_stop()

    cast(Any, private.loop).stop = _stop
    outcome: dict[str, object] = {}

    def _job() -> None:
        try:
            outcome["job"] = private.run(_sleep_then(0, "value"))
        except BaseException as exc:
            outcome["job"] = exc

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        job = threading.Thread(target=_job, daemon=True)
        job.start()
        job.join(3)
        if job.is_alive():
            real_stop()
            job.join(3)

    assert outcome == {"job": "value"}
    assert stops == ["landed", "stopped"]
    [record] = [
        record
        for record in caplog.records
        if record.name == "asyncio" and record.exc_info and record.exc_info[1] is landed
    ]
    assert record.levelno == logging.ERROR


def test_a_private_loop_is_its_thread_s_current_loop_while_it_runs_and_the_thread_has_none_after(
    private: PrivateLoop,
) -> None:
    async def _current_while_running() -> asyncio.AbstractEventLoop | None:
        return _current_loop()

    def _work() -> tuple[object, object, object]:
        during = private.run(_current_while_running())
        after_the_job = _current_loop()
        private.close()
        return during, after_the_job, _current_loop()

    assert _in_a_thread(_work) == (private.loop, None, None)


def test_a_private_job_run_inside_another_loop_leaves_the_outer_loop_the_thread_s_current_loop(
    steps: list[tuple[str, asyncio.AbstractEventLoop]],
) -> None:
    inner_loop, outer_loop = PrivateLoop(), PrivateLoop()
    inner = _sleep_then(0, "inner")

    async def _runs_a_job_inside() -> asyncio.AbstractEventLoop | None:
        with pytest.raises(RuntimeError, match="another loop is running"):
            inner_loop.run(inner)
        return _current_loop()

    def _work() -> object:
        try:
            return outer_loop.run(_runs_a_job_inside())
        finally:
            outer_loop.close()

    current = _in_a_thread(_work)
    inner.close()

    assert inner_loop.closed
    assert current is outer_loop.loop
