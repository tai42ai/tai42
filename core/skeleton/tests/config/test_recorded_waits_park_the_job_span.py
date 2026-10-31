"""A job waiting through ``hold_recorded`` or ``hold_recorded_blocking`` parks its fork-gate span.

The holder of the lock or span it waits on may be starting a module re-import, which must not
wait for a job that cannot move until the holder lets go. The span counts again before the job
holds what it waited for, and a job stopped while its span counts again is left holding nothing.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from tai42_kit.fork_gate import fork_gate
from tai42_kit.process_lock import ProcessLock

from tai42_skeleton.app.reload_gate import GateParty, hold_recorded, hold_recorded_blocking

from .fake_pipeline import FakeConfigStore


@contextmanager
def _span_held_elsewhere(store: FakeConfigStore) -> Iterator[Callable[[], None]]:
    """Hold ``store``'s span as another process would; yields the call that lets it go.

    Let go on exit too, so a failing test never leaves a thread blocked entering the span.
    """
    store.span_lock.acquire()
    held = [True]

    def let_go() -> None:
        held[0] = False
        store.span_lock.release()

    try:
        yield let_go
    finally:
        if held[0]:
            store.span_lock.release()


_ARRIVES = 5.0
_STAYS_CLEAR = 0.2


def _nobody() -> GateParty:
    return GateParty(request=None, task=None, task_epoch=None)


class _HeldElsewhere:
    """A lock held by work that is no job, from :meth:`take` until :meth:`let_go`."""

    def __init__(self, lock: ProcessLock) -> None:
        self._lock = lock
        self._let_go = asyncio.Event()
        self._holder: asyncio.Task[None] | None = None

    async def _hold(self) -> None:
        async with self._lock.held():
            await self._let_go.wait()

    async def take(self) -> None:
        self._holder = asyncio.create_task(self._hold())
        await _until(lambda: self._lock.locked)

    async def let_go(self) -> None:
        """Release the lock; returns once the release is observed."""
        assert self._holder is not None
        self._let_go.set()
        await asyncio.wait_for(self._holder, _ARRIVES)


class _Rebuild:
    """A rebuild held on a helper thread from :meth:`hold` until :meth:`release`."""

    def __init__(self) -> None:
        self._start = threading.Event()
        self._held = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        if not self._start.wait(_ARRIVES):
            return
        with fork_gate.exclusive(timeout=_ARRIVES):
            self._held.set()
            self._release.wait(_ARRIVES)

    async def hold(self) -> None:
        self._start.set()
        assert await asyncio.to_thread(self._held.wait, _ARRIVES)

    def release(self) -> None:
        self._release.set()

    def end(self) -> None:
        self._start.set()
        self._release.set()
        self._thread.join(_ARRIVES)
        assert not self._thread.is_alive()


@pytest.fixture
def rebuild() -> Iterator[_Rebuild]:
    held = _Rebuild()
    yield held
    held.end()


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(_ARRIVES):
        while not condition():
            await asyncio.sleep(0.005)


async def test_a_job_waiting_for_a_recorded_lock_does_not_count_until_it_holds_it() -> None:
    lock = ProcessLock()
    elsewhere = _HeldElsewhere(lock)
    await elsewhere.take()
    inside: list[int] = []

    async def job() -> None:
        async with fork_gate.job_span_async(timeout=_ARRIVES), hold_recorded(lock, _nobody()):
            inside.append(fork_gate.live_spans)

    task = asyncio.create_task(job())
    await asyncio.sleep(_STAYS_CLEAR)
    assert fork_gate.live_spans == 0
    await elsewhere.let_go()
    await asyncio.wait_for(task, _ARRIVES)
    assert inside == [1]


async def test_a_job_cancelled_while_its_span_counts_again_does_not_keep_the_recorded_lock(rebuild: _Rebuild) -> None:
    lock = ProcessLock()
    elsewhere = _HeldElsewhere(lock)
    await elsewhere.take()
    ran: list[bool] = []

    async def job() -> None:
        async with fork_gate.job_span_async(timeout=_ARRIVES), hold_recorded(lock, _nobody()):
            ran.append(True)

    task = asyncio.create_task(job())
    await asyncio.sleep(_STAYS_CLEAR)
    assert fork_gate.live_spans == 0
    await rebuild.hold()
    await elsewhere.let_go()
    await _until(lambda: lock.locked)
    task.cancel()
    await asyncio.sleep(_STAYS_CLEAR)
    assert not task.done()
    rebuild.release()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _ARRIVES)
    assert ran == []
    assert not lock.locked
    assert fork_gate.live_spans == 0


async def test_a_job_blocked_entering_a_recorded_span_does_not_count_until_it_is_inside() -> None:
    store = FakeConfigStore()
    inside: list[int] = []

    async def job() -> None:
        async with (
            fork_gate.job_span_async(timeout=_ARRIVES),
            hold_recorded_blocking(store.exclusive_write(), _nobody()),
        ):
            inside.append(fork_gate.live_spans)

    with _span_held_elsewhere(store) as let_go:
        task = asyncio.create_task(job())
        await asyncio.sleep(_STAYS_CLEAR)
        assert fork_gate.live_spans == 0
        let_go()
        await asyncio.wait_for(task, _ARRIVES)
    assert inside == [1]
    assert store.events == ["exclusive_write:enter", "exclusive_write:exit"]


async def test_a_job_cancelled_while_its_span_counts_again_leaves_the_recorded_span(rebuild: _Rebuild) -> None:
    store = FakeConfigStore()
    ran: list[bool] = []

    async def job() -> None:
        async with (
            fork_gate.job_span_async(timeout=_ARRIVES),
            hold_recorded_blocking(store.exclusive_write(), _nobody()),
        ):
            ran.append(True)

    with _span_held_elsewhere(store) as let_go:
        task = asyncio.create_task(job())
        await asyncio.sleep(_STAYS_CLEAR)
        assert fork_gate.live_spans == 0
        await rebuild.hold()
        let_go()
        await _until(lambda: store.events == ["exclusive_write:enter"])
        task.cancel()
        await asyncio.sleep(_STAYS_CLEAR)
        assert not task.done()
        rebuild.release()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _ARRIVES)
    assert ran == []
    assert store.events == ["exclusive_write:enter", "exclusive_write:exit"]
    assert not store.span_lock.locked()
    assert fork_gate.live_spans == 0
