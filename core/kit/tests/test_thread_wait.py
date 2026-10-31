"""Awaiting a wait or a hold that blocks a thread: the loop keeps turning, a cancellation stops
the thread and is observed only once the thread has ended, and the thread's errors reach the
awaiting coroutine."""

from __future__ import annotations

import asyncio
import threading

import pytest

from tai42_kit.thread_wait import _resolve, hold_on_thread, wait_on_thread

# Budget for an event that MUST fire (generous: only a real hang exceeds it).
_ARRIVES = 5.0
# Budget for an event that must NOT fire (short: proves non-progress cheaply).
_STAYS_CLEAR = 0.2


class _Sleeper:
    """A condition a thread sleeps on until it is told to go on or to stop."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.go_on = False
        self.waiting = threading.Event()
        self.ended = threading.Event()

    def wake(self) -> None:
        with self.cond:
            self.cond.notify_all()

    def release(self) -> None:
        with self.cond:
            self.go_on = True
            self.cond.notify_all()

    def wait(self, stop: threading.Event) -> None:
        with self.cond:
            self.waiting.set()
            self.cond.wait_for(lambda: self.go_on or stop.is_set(), timeout=_ARRIVES)
        self.ended.set()


async def test_a_wait_on_a_thread_ends_when_the_thread_does_and_the_loop_turns_meanwhile():
    sleeper = _Sleeper()
    task = asyncio.create_task(wait_on_thread(sleeper.wait, wake=sleeper.wake, name="test-wait"))
    assert await asyncio.to_thread(sleeper.waiting.wait, _ARRIVES)
    await asyncio.sleep(_STAYS_CLEAR)
    assert not task.done()
    sleeper.release()
    await asyncio.wait_for(task, _ARRIVES)
    assert sleeper.ended.is_set()


async def test_a_cancelled_wait_on_a_thread_stops_the_thread_and_returns_once_it_has_ended():
    sleeper = _Sleeper()
    task = asyncio.create_task(wait_on_thread(sleeper.wait, wake=sleeper.wake, name="test-wait"))
    assert await asyncio.to_thread(sleeper.waiting.wait, _ARRIVES)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _ARRIVES)
    assert sleeper.ended.is_set()


async def test_a_wait_on_a_thread_raises_the_threads_error():
    def wait(stop: threading.Event) -> None:
        raise ValueError("the wait failed")

    with pytest.raises(ValueError, match="the wait failed"):
        await wait_on_thread(wait, wake=lambda: None, name="test-wait")


async def test_a_hold_on_a_thread_is_taken_and_released_on_one_thread():
    threads: list[int] = []

    def acquire(stop: threading.Event) -> bool:
        threads.append(threading.get_ident())
        return True

    def release() -> None:
        threads.append(threading.get_ident())

    async with hold_on_thread(acquire, release, wake=lambda: None, name="test-hold"):
        assert len(threads) == 1
    assert len(threads) == 2
    assert threads[0] == threads[1] != threading.get_ident()


async def test_a_hold_cancelled_while_it_is_taken_gives_up_and_still_releases():
    sleeper = _Sleeper()
    released = threading.Event()

    def acquire(stop: threading.Event) -> bool:
        sleeper.wait(stop)
        return sleeper.go_on

    async def hold() -> None:
        async with hold_on_thread(acquire, released.set, wake=sleeper.wake, name="test-hold"):
            raise AssertionError("the body ran without the hold")

    task = asyncio.create_task(hold())
    assert await asyncio.to_thread(sleeper.waiting.wait, _ARRIVES)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _ARRIVES)
    assert released.is_set()


async def test_a_hold_cancelled_in_its_body_is_released_before_the_cancellation_propagates():
    released = threading.Event()
    in_body = asyncio.Event()

    async def hold() -> None:
        async with hold_on_thread(lambda stop: True, released.set, wake=lambda: None, name="test-hold"):
            in_body.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await in_body.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _ARRIVES)
    assert released.is_set()


async def test_a_hold_raises_the_errors_of_its_acquire_and_its_release():
    def failed_acquire(stop: threading.Event) -> bool:
        raise ValueError("the acquire failed")

    with pytest.raises(ValueError, match="the acquire failed"):
        async with hold_on_thread(failed_acquire, lambda: None, wake=lambda: None, name="test-hold"):
            raise AssertionError("the body ran without the hold")

    def failed_release() -> None:
        raise ValueError("the release failed")

    with pytest.raises(ValueError, match="the release failed"):
        async with hold_on_thread(lambda stop: True, failed_release, wake=lambda: None, name="test-hold"):
            pass


async def test_resolve_settles_each_branch_exactly_once():
    """The thread's settler: success, failure, and the already-settled future a cancelled
    awaiter leaves behind (settling it again would raise InvalidStateError)."""
    loop = asyncio.get_running_loop()

    ok: asyncio.Future[None] = loop.create_future()
    _resolve(ok, None)
    assert ok.result() is None

    boom = RuntimeError("acquire blew up")
    failed: asyncio.Future[None] = loop.create_future()
    _resolve(failed, boom)
    assert failed.exception() is boom

    cancelled: asyncio.Future[None] = loop.create_future()
    cancelled.cancel()
    # Must be a no-op, not an InvalidStateError.
    _resolve(cancelled, None)
    _resolve(cancelled, boom)
    assert cancelled.cancelled()
