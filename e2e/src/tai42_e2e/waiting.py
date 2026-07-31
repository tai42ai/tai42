"""The single waiting primitive for the whole suite.

Every ``wait_for`` poll a condition on a fixed interval until a deadline; on
timeout it raises with the caller's message plus the last detail the predicate
produced, so a failure says exactly what never became true. Ad-hoc
``time.sleep`` / ``asyncio.sleep`` are banned by the ruff config: a race is
either bounded by a deadline here or it is a bug."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class WaitTimeout(TimeoutError):
    """A ``wait_for`` deadline elapsed before the predicate returned truthy."""


def wait_for[T](
    predicate: Callable[[], T | None],
    *,
    deadline: float,
    interval: float = 0.1,
    message: str = "condition never became true",
) -> T:
    """Poll ``predicate`` every ``interval`` seconds until it returns a truthy
    value or ``deadline`` seconds elapse. Returns the truthy value. Raises
    :class:`WaitTimeout` naming the last falsy result on timeout.

    The predicate is expected to be cheap and side-effect free; it may raise a
    transient connection error while a service warms, which is treated as "not
    ready yet" only for the connection-refused / 503 shapes callers pass — any
    other exception propagates immediately (never silently swallowed)."""
    start = time.monotonic()
    last: T | None = None
    while True:
        last = predicate()
        if last:
            return last
        if time.monotonic() - start >= deadline:
            raise WaitTimeout(f"{message} (after {deadline:.1f}s; last result: {last!r})")
        time.sleep(interval)  # noqa: TID251 — the sanctioned poll interval; every other sleep is banned


async def wait_for_async[T](
    predicate: Callable[[], Awaitable[T | None]],
    *,
    deadline: float,
    interval: float = 0.1,
    message: str = "condition never became true",
) -> T:
    """Async twin of :func:`wait_for` for predicates that must ``await`` (HTTP
    probes, MCP calls, census reads)."""
    start = time.monotonic()
    last: T | None = None
    while True:
        last = await predicate()
        if last:
            return last
        if time.monotonic() - start >= deadline:
            raise WaitTimeout(f"{message} (after {deadline:.1f}s; last result: {last!r})")
        await asyncio.sleep(interval)  # noqa: TID251 — the sanctioned poll interval; every other sleep is banned


def align_to_window(window_seconds: float) -> None:
    """Block until just after the next ``window_seconds`` boundary of the wall
    clock, so a fixed-window rate-limit test cannot straddle two windows. The
    limiter keys windows on ``floor(now / window)``; starting a burst right
    after a boundary gives the full window's budget before the next rollover."""
    now = time.time()
    remaining = window_seconds - (now % window_seconds)
    deadline = now + remaining + 0.05

    def past_boundary() -> bool:
        return time.time() >= deadline

    wait_for(past_boundary, deadline=window_seconds + 1.0, interval=0.02, message="window boundary never reached")
