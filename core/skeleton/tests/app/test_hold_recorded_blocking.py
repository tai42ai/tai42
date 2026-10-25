"""A blocking sync span held through ``hold_recorded_blocking``: entered and exited off the loop, never left held."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import Any

import pytest

from tai42_skeleton.app.admission import RequestAdmission
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.app.reload_gate import GateParty, hold_recorded_blocking

_BOUND_S = 10.0


class _Span:
    """A span whose entry blocks its thread until ``release`` is set, recording enter and exit.

    A class-based context manager, so only an explicit exit leaves it: dropping it unexited
    leaves it held, as a lock on a file descriptor kept open would be.
    """

    def __init__(
        self, *, refuse: Exception | None = None, hold_exit: bool = False, exit_error: Exception | None = None
    ) -> None:
        self.events: list[str] = []
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.refused = threading.Event()
        self.exiting = threading.Event()
        self.release_exit = threading.Event()
        self.exited = threading.Event()
        self._refuse = refuse
        self._hold_exit = hold_exit
        self._exit_error = exit_error

    def hold(self) -> _Span:
        return self

    def __enter__(self) -> None:
        self.blocked.set()
        if not self.release.wait(_BOUND_S):
            raise TimeoutError("the test never released the span")
        if self._refuse is not None:
            self.refused.set()
            raise self._refuse
        self.events.append(f"enter:{threading.current_thread() is threading.main_thread()}")
        self.entered.set()

    def __exit__(self, *_exc: object) -> None:
        self.exiting.set()
        if self._hold_exit and not self.release_exit.wait(_BOUND_S):
            raise TimeoutError("the test never released the span's exit")
        self.events.append(f"exit:{threading.current_thread() is threading.main_thread()}")
        self.exited.set()
        if self._exit_error is not None:
            raise self._exit_error


def _waiting_party() -> tuple[GateParty, Epoch, RequestAdmission]:
    serving = Epoch(number=0)
    serving.admit()
    request = RequestAdmission(epoch=serving)
    return GateParty(request=request, task=None, task_epoch=None), serving, request


async def test_the_span_is_entered_and_exited_off_the_loop_with_its_waiter_recorded() -> None:
    span = _Span()
    party, serving, request = _waiting_party()
    inside = asyncio.Event()

    async def hold() -> None:
        async with hold_recorded_blocking(span.hold(), party):
            inside.set()

    task = asyncio.create_task(hold())
    assert await asyncio.to_thread(span.blocked.wait, _BOUND_S)
    assert request in serving.waiting
    assert serving.in_flight == 0
    span.release.set()
    await task
    assert inside.is_set()
    assert span.events == ["enter:False", "exit:False"]
    assert request not in serving.waiting
    assert serving.in_flight == 1


async def test_an_error_entering_the_span_propagates_and_ends_the_wait() -> None:
    span = _Span(refuse=OSError("the lock file cannot be opened"))
    span.release.set()
    party, serving, request = _waiting_party()

    with pytest.raises(OSError, match="cannot be opened"):
        async with hold_recorded_blocking(span.hold(), party):
            pytest.fail("the body ran without the span")
    assert span.events == []
    assert request not in serving.waiting


async def test_a_waiter_cancelled_while_its_thread_blocks_leaves_the_span_released() -> None:
    span = _Span()
    party, serving, request = _waiting_party()

    async def hold() -> None:
        async with hold_recorded_blocking(span.hold(), party):
            pytest.fail("the body of a cancelled waiter ran")

    task = asyncio.create_task(hold())
    assert await asyncio.to_thread(span.blocked.wait, _BOUND_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert request not in serving.waiting
    # The thread enters the span once it is released, and exits it at once: no holder remains.
    span.release.set()
    assert await asyncio.to_thread(span.exited.wait, _BOUND_S)
    assert span.events == ["enter:False", "exit:False"]


async def test_a_holder_cancelled_inside_the_span_still_exits_it() -> None:
    span = _Span()
    span.release.set()
    inside = asyncio.Event()

    async def hold() -> None:
        async with hold_recorded_blocking(span.hold(), GateParty(request=None, task=None, task_epoch=None)):
            inside.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await _reached(inside)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert span.events == ["enter:False", "exit:False"]


async def _cancelled_again_while_exiting(task: asyncio.Task[None], span: _Span) -> None:
    """Wait for the span's exit to start on its thread, cancel ``task`` again, and check it waits for the exit."""
    assert await asyncio.to_thread(span.exiting.wait, _BOUND_S)
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done(), "the holder returned while the span's exit was still running"
    assert not span.exited.is_set()
    span.release_exit.set()


async def _reached(event: asyncio.Event) -> None:
    """Wait for ``event``; fail the test when it is not set within the bound."""
    try:
        await asyncio.wait_for(event.wait(), _BOUND_S)
    except TimeoutError:
        pytest.fail("the holder never got inside the span")


def _holder_inside_the_span(span: _Span) -> tuple[asyncio.Task[None], asyncio.Event]:
    inside = asyncio.Event()

    async def hold() -> None:
        async with hold_recorded_blocking(span.hold(), GateParty(request=None, task=None, task_epoch=None)):
            inside.set()
            await asyncio.Event().wait()

    return asyncio.create_task(hold()), inside


async def test_a_second_cancel_during_the_exit_returns_only_once_the_span_is_out() -> None:
    span = _Span(hold_exit=True)
    span.release.set()
    task, inside = _holder_inside_the_span(span)
    await _reached(inside)
    task.cancel()
    await _cancelled_again_while_exiting(task, span)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert span.exited.is_set()
    assert span.events == ["enter:False", "exit:False"]


async def test_an_error_exiting_the_span_propagates_through_a_second_cancel() -> None:
    span = _Span(hold_exit=True, exit_error=OSError("releasing the span failed"))
    span.release.set()
    task, inside = _holder_inside_the_span(span)
    await _reached(inside)
    task.cancel()
    await _cancelled_again_while_exiting(task, span)
    with pytest.raises(OSError, match="releasing the span failed") as raised:
        await task
    assert isinstance(raised.value.__context__, asyncio.CancelledError)
    assert span.events == ["enter:False", "exit:False"]


async def test_a_waiter_cancelled_as_its_entry_completes_exits_the_span_before_returning() -> None:
    span = _Span(hold_exit=True, exit_error=OSError("releasing the span failed"))
    party, serving, request = _waiting_party()

    async def hold() -> None:
        async with hold_recorded_blocking(span.hold(), party):
            pytest.fail("the body of a cancelled waiter ran")

    task = asyncio.create_task(hold())
    assert await asyncio.to_thread(span.blocked.wait, _BOUND_S)
    task.cancel()
    span.release.set()
    # Blocking the loop here lets the entry complete before the cancelled task runs again, so
    # the task finds the span entered and exits it itself.
    assert span.entered.wait(_BOUND_S)
    await _cancelled_again_while_exiting(task, span)
    with pytest.raises(OSError, match="releasing the span failed"):
        await task
    assert span.events == ["enter:False", "exit:False"]
    assert request not in serving.waiting


async def test_an_error_entering_the_span_after_its_waiter_was_cancelled_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    span = _Span(refuse=OSError("the lock file cannot be opened"))
    party, _serving, _request = _waiting_party()

    async def hold() -> None:
        async with hold_recorded_blocking(span.hold(), party):
            pytest.fail("the body of a cancelled waiter ran")

    task = asyncio.create_task(hold())
    assert await asyncio.to_thread(span.blocked.wait, _BOUND_S)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.app.reload_gate"):
        span.release.set()
        assert await asyncio.to_thread(span.refused.wait, _BOUND_S)
        for _ in range(50):
            if caplog.records:
                break
            await asyncio.sleep(0.01)
    [record] = caplog.records
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], OSError)
    assert span.events == []


async def test_an_error_entering_the_span_as_its_waiter_is_cancelled_reaches_the_waiter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    span = _Span(refuse=OSError("the lock file cannot be opened"))
    party, serving, request = _waiting_party()

    async def hold() -> None:
        async with hold_recorded_blocking(span.hold(), party):
            pytest.fail("the body ran without the span")

    task = asyncio.create_task(hold())
    assert await asyncio.to_thread(span.blocked.wait, _BOUND_S)
    task.cancel()
    span.release.set()
    # Blocking the loop here lets the entry fail before the cancelled task runs again, so the
    # error lands in a future the cancellation already cancelled.
    assert span.refused.wait(_BOUND_S)
    with (
        caplog.at_level(logging.ERROR, logger="tai42_skeleton.app.reload_gate"),
        pytest.raises(OSError, match="cannot be opened") as raised,
    ):
        await task
    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    assert caplog.records == []
    assert request not in serving.waiting


def _release_the_exit_on_a_cancel_during_it(span: _Span) -> None:
    """Install on the running loop a task factory whose tasks release ``span``'s exit when cancelled during it."""

    class _ObservedTask[T](asyncio.Task[T]):
        def cancel(self, msg: object = None) -> bool:
            if span.exiting.is_set():
                span.release_exit.set()
            return super().cancel(msg)

    def factory[T](loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, T], /, **kwargs: Any) -> asyncio.Task[T]:
        return _ObservedTask(coro, loop=loop, **kwargs)

    asyncio.get_running_loop().set_task_factory(factory)


def test_loop_teardown_waits_for_the_exit_and_delivers_its_error() -> None:
    span = _Span(hold_exit=True, exit_error=OSError("releasing the span failed"))
    span.release.set()
    holders: list[asyncio.Task[None]] = []

    async def main() -> None:
        _release_the_exit_on_a_cancel_during_it(span)
        task, inside = _holder_inside_the_span(span)
        holders.append(task)
        await _reached(inside)
        task.cancel()
        assert await asyncio.to_thread(span.exiting.wait, _BOUND_S)
        # Returning now tears the loop down, cancelling every task still running, while the
        # span's exit still blocks on its thread; that cancel is what releases the exit.
        assert not span.release_exit.is_set()

    asyncio.run(main())
    [task] = holders
    assert span.release_exit.is_set(), "the loop teardown never cancelled a task during the span's exit"
    assert span.exited.is_set()
    assert not task.cancelled(), "the holder returned before the span's exit completed"
    error = task.exception()
    assert isinstance(error, OSError)
    assert str(error) == "releasing the span failed"
