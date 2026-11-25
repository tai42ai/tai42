"""One read per key while it is in flight: a second caller of the same key awaits the first read.

Nothing is remembered once a read lands, success or failure: every caller that awaited it sees the
same result or the same exception, and the next caller starts a fresh read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class _Flight[T]:
    """One read in flight under ``key`` and the number of callers awaiting it."""

    def __init__(self, flights: dict[str, _Flight[T]], key: str, read: Coroutine[Any, Any, T]) -> None:
        self._flights = flights
        self._key = key
        self.waiters = 0
        flights[key] = self
        self.task: asyncio.Task[T] = asyncio.ensure_future(self._land(read))

    async def _land(self, read: Coroutine[Any, Any, T]) -> T:
        try:
            return await read
        finally:
            self.leave()

    def leave(self) -> None:
        """Stop answering later callers of the key; a read already awaited goes on for its waiters."""
        if self._flights.get(self._key) is self:
            del self._flights[self._key]


class SingleFlight[T]:
    """Reads keyed by name, each issued once while it is in flight, on the loop that awaits it."""

    def __init__(self) -> None:
        """Start with no read in flight."""
        self._flights: dict[str, _Flight[T]] = {}

    async def run(self, key: str, read: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """The result of ``read()`` for ``key``, joining the read of ``key`` already in flight.

        A cancelled caller leaves the read to the callers still awaiting it; the read is cancelled
        when its last caller is.
        """
        flight = self._flights.get(key)
        if flight is None:
            flight = _Flight(self._flights, key, read())
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0 and not flight.task.done():
                flight.leave()
                flight.task.cancel()

    def detach(self, matches: Callable[[str], bool]) -> None:
        """Stop the reads in flight whose key ``matches`` from answering later callers.

        Each such read goes on for the callers already awaiting it; the next caller of its key
        starts a fresh read.
        """
        for flight in [flight for key, flight in self._flights.items() if matches(key)]:
            flight.leave()


__all__ = ["SingleFlight"]
