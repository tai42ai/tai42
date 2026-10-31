"""A lock for the coroutines of one process: holders on different event loops and threads exclude
each other, waiters take it in arrival order, a cancellation never leaves it held, the loop keeps
turning while a holder waits, and a forked child's copy starts free."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any

from tai42_kit.process_lock import ProcessLock

# Budget for an event that MUST fire (generous: only a real hang exceeds it).
_ARRIVES = 5.0
# Budget for an event that must NOT fire (short: proves non-progress cheaply).
_STAYS_CLEAR = 0.2
# How long a holder keeps the lock in the overlap tests.
_HOLD = 0.3


def _lock_threads() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == "tai-process-lock")


def _on_a_loop_of_its_own(
    work: Callable[[], Coroutine[Any, Any, None]],
) -> tuple[threading.Thread, list[BaseException]]:
    """Start a thread that runs ``work()`` on a new event loop of its own; collect what it raises."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(work())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name="test-own-loop")
    thread.start()
    return thread, errors


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(_ARRIVES):
        while not condition():
            await asyncio.sleep(0.005)


async def test_holders_on_two_loops_in_two_threads_exclude_each_other() -> None:
    lock = ProcessLock()
    windows: list[tuple[float, float]] = []
    first_holds = threading.Event()
    seen_locked: list[bool] = []

    async def first() -> None:
        async with lock.held():
            started = time.monotonic()
            first_holds.set()
            await asyncio.sleep(_HOLD)
            windows.append((started, time.monotonic()))

    async def second() -> None:
        assert await asyncio.to_thread(first_holds.wait, _ARRIVES)
        seen_locked.append(lock.locked)
        async with lock.held():
            started = time.monotonic()
            await asyncio.sleep(_HOLD)
            windows.append((started, time.monotonic()))

    threads = [_on_a_loop_of_its_own(first), _on_a_loop_of_its_own(second)]
    for thread, _ in threads:
        thread.join(_ARRIVES * 2)
    assert [errors for _, errors in threads] == [[], []]
    assert seen_locked == [True]
    (first_start, first_end), (second_start, _) = windows
    assert second_start >= first_end > first_start


async def test_waiters_take_the_lock_in_arrival_order() -> None:
    lock = ProcessLock()
    order: list[str] = []
    release = asyncio.Event()

    async def holder() -> None:
        async with lock.held():
            await release.wait()

    async def waiter(name: str) -> None:
        async with lock.held():
            order.append(name)

    holding = asyncio.create_task(holder())
    await _until(lambda: lock.locked)
    waiters = []
    for name in ("a", "b", "c"):
        waiters.append(asyncio.create_task(waiter(name)))
        # The next waiter arrives only once this one is queued.
        await _until(lambda: len(lock._queue) == len(waiters))
    release.set()
    await asyncio.wait_for(asyncio.gather(holding, *waiters), _ARRIVES)
    assert order == ["a", "b", "c"]


async def test_a_hold_cancelled_while_waiting_leaves_the_lock_free_and_its_thread_ended() -> None:
    lock = ProcessLock()
    release = asyncio.Event()
    entered: list[str] = []

    async def holder() -> None:
        async with lock.held():
            await release.wait()

    async def waiter() -> None:
        async with lock.held():
            entered.append("cancelled waiter")  # pragma: no cover - it never takes the lock

    holding = asyncio.create_task(holder())
    await _until(lambda: lock.locked)
    waiting = asyncio.create_task(waiter())
    await _until(lambda: len(lock._queue) == 1)
    waiting.cancel()
    try:
        await asyncio.wait_for(waiting, _ARRIVES)
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - the waiter is cancelled
        raise AssertionError("the waiter was not cancelled")
    await _until(lambda: _lock_threads() == 1)
    release.set()
    await asyncio.wait_for(holding, _ARRIVES)
    assert not lock.locked

    async with asyncio.timeout(_STAYS_CLEAR * 5), lock.held():
        entered.append("later holder")
    assert entered == ["later holder"]
    await _until(lambda: _lock_threads() == 0)


async def test_a_hold_cancelled_in_its_body_is_released_before_the_cancellation_propagates() -> None:
    lock = ProcessLock()
    inside = asyncio.Event()
    locked_when_cancelled: list[bool] = []

    async def holder() -> None:
        try:
            async with lock.held():
                inside.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            locked_when_cancelled.append(lock.locked)
            raise

    holding = asyncio.create_task(holder())
    await asyncio.wait_for(inside.wait(), _ARRIVES)
    holding.cancel()
    try:
        await asyncio.wait_for(holding, _ARRIVES)
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - the holder is cancelled
        raise AssertionError("the holder was not cancelled")
    assert locked_when_cancelled == [False]
    await _until(lambda: _lock_threads() == 0)


async def test_locked_reads_whether_the_lock_is_held() -> None:
    lock = ProcessLock()
    assert not lock.locked
    async with lock.held():
        assert lock.locked
    assert not lock.locked


async def test_a_forked_childs_reset_leaves_a_held_lock_free_and_takeable() -> None:
    """Models the child: the parent's hold is live when the reset runs, and nothing in the child
    can release it, so the reset starts the lock free and the child's first holder takes it."""
    lock = ProcessLock()
    release = asyncio.Event()

    async def parent_holder() -> None:
        async with lock.held():
            await release.wait()

    holding = asyncio.create_task(parent_holder())
    await _until(lambda: lock.locked)
    lock.reset_after_fork()
    assert not lock.locked
    entered: list[bool] = []
    async with asyncio.timeout(_STAYS_CLEAR * 5), lock.held():
        entered.append(lock.locked)
    assert entered == [True]
    release.set()
    await asyncio.wait_for(holding, _ARRIVES)
    assert not lock.locked


async def test_the_loop_keeps_turning_while_a_holder_waits() -> None:
    lock = ProcessLock()
    release = asyncio.Event()
    ticks = 0

    async def holder() -> None:
        async with lock.held():
            await release.wait()

    async def waiter() -> None:
        async with lock.held():
            pass

    async def sibling() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    holding = asyncio.create_task(holder())
    await _until(lambda: lock.locked)
    waiting = asyncio.create_task(waiter())
    await _until(lambda: len(lock._queue) == 1)
    counting = asyncio.create_task(sibling())
    await asyncio.sleep(_STAYS_CLEAR)
    assert not waiting.done()
    assert ticks >= 5
    counting.cancel()
    release.set()
    await asyncio.wait_for(asyncio.gather(holding, waiting), _ARRIVES)
