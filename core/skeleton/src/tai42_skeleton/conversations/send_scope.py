"""The window one channel send runs in: its medium clock, and the keepalive of what covers the send.

A seam opens the scope around the channel call it bounds. A hold the platform makes INSIDE that
call (the receipt wait between the messages of one notification) is the platform's own time, not
the medium's: it stops the clock, runs the seam's keepalive as the wait begins so the claim or
record covering the send stays live, and restarts the clock with the time it held added back.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class SendScope:
    """One platform-bounded channel send: its entered medium clock and what refreshes the cover of the send."""

    clock: asyncio.Timeout
    keepalive: Callable[[], Awaitable[None]]

    @asynccontextmanager
    async def platform_wait(self) -> AsyncIterator[None]:
        """Stop the medium clock for the platform's own wait, refresh the cover as it begins, then restart the clock.

        Refreshing as every wait begins bounds the time between two refreshes of the cover by one wait
        and the medium's bound: after the cover is taken before the send, or refreshed as a wait begins,
        at most one wait and the medium's time pass before the next wait begins or the send ends, so no
        refresh is needed after a wait. The clock restarts whether the keepalive or the wait returns or
        raises, so a send whose medium budget was spent before the wait is cut right after it, never
        during it.
        """
        deadline = self.clock.when()
        if deadline is None:
            raise RuntimeError("a send scope runs under a finite medium bound")
        loop = asyncio.get_running_loop()
        paused_at = loop.time()
        self.clock.reschedule(None)
        try:
            await self.keepalive()
            yield
        finally:
            self.clock.reschedule(deadline + (loop.time() - paused_at))


_scope: ContextVar[SendScope | None] = ContextVar("conversations_send_scope", default=None)


@asynccontextmanager
async def send_scope(bound: float, keepalive: Callable[[], Awaitable[None]]) -> AsyncIterator[asyncio.Timeout]:
    """Bound a channel call by ``bound`` seconds of the medium's time; the scope is ambient for a hold inside it."""
    async with asyncio.timeout(bound) as clock:
        token = _scope.set(SendScope(clock, keepalive))
        try:
            yield clock
        finally:
            _scope.reset(token)


def current_send_scope() -> SendScope | None:
    """The scope of the send running in this task, or ``None`` outside any platform-bounded send."""
    return _scope.get()


__all__ = ["SendScope", "current_send_scope", "send_scope"]
