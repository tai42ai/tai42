"""One read per key per loop while it is in flight: a second caller of the key on the same loop awaits the first read.

A caller on another loop starts its own read, since a read's task belongs to the loop that runs it. Nothing is
remembered once a read lands, success or failure: every caller that awaited it sees the same result or the same
exception, and the next caller starts a fresh read. The table of reads in flight is reached from every loop and
thread of the process, so it is kept under a lock.
"""

from __future__ import annotations

import asyncio
import threading
from asyncio import AbstractEventLoop
from collections.abc import Callable, Coroutine, Hashable
from typing import Any

type _Slot[K] = tuple[AbstractEventLoop, K]


class _Flight[K: Hashable, T]:
    """One read in flight under ``slot`` (its loop and key) and the number of callers awaiting it."""

    task: asyncio.Task[T]

    def __init__(self, flights: dict[_Slot[K], _Flight[K, T]], lock: threading.Lock, slot: _Slot[K]) -> None:
        self._flights = flights
        self._lock = lock
        self.slot = slot
        self.waiters = 0

    def start(self, read: Coroutine[Any, Any, T]) -> None:
        """Run ``read`` as this flight's task on the caller's loop."""
        self.task = asyncio.ensure_future(self._land(read))

    async def _land(self, read: Coroutine[Any, Any, T]) -> T:
        try:
            return await read
        finally:
            self.leave()

    def leave(self) -> None:
        """Stop answering later callers of the key; a read already awaited goes on for its waiters."""
        with self._lock:
            if self._flights.get(self.slot) is self:
                del self._flights[self.slot]


class SingleFlight[K: Hashable, T]:
    """Reads keyed by a hashable key, each issued once per loop while it is in flight; the table is locked."""

    def __init__(self) -> None:
        """Start with no read in flight."""
        self._flights: dict[_Slot[K], _Flight[K, T]] = {}
        self._lock = threading.Lock()

    async def run(self, key: K, read: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """The result of ``read()`` for ``key``, joining the read of ``key`` already in flight on this loop.

        A cancelled caller leaves the read to the callers still awaiting it; the read is cancelled
        when its last caller is.
        """
        slot = (asyncio.get_running_loop(), key)
        with self._lock:
            flight = self._flights.get(slot)
        if flight is None:
            # Only this loop's callers share ``slot`` and none runs before this step yields, so the read is made
            # and started outside the lock; it is registered before it starts, so its landing always unregisters it.
            pending = read()
            flight = _Flight(self._flights, self._lock, slot)
            with self._lock:
                self._flights[slot] = flight
            flight.start(pending)
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0 and not flight.task.done():
                flight.leave()
                flight.task.cancel()

    def detach(self, matches: Callable[[K], bool]) -> None:
        """Stop the reads in flight on every loop whose key ``matches`` from answering later callers.

        Each such read goes on for the callers already awaiting it; the next caller of its key
        starts a fresh read. Callable from any thread, with or without a running loop.
        """
        with self._lock:
            doomed = [flight for (_loop, key), flight in self._flights.items() if matches(key)]
        for flight in doomed:
            flight.leave()


__all__ = ["SingleFlight"]
