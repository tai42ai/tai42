"""A loop heartbeat: how many of its expected ticks an event loop delivered while a job ran."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable

TICK = 0.02
# A job long enough that a held loop would lose most of its ticks.
GIL_HOLDING_PATTERN = r"(a+)+$"
GIL_HOLDING_SUBJECT = "a" * 25 + "b"


async def tick_ratio[T](job: Awaitable[T]) -> tuple[T, float]:
    """Await ``job`` while a heartbeat ticks every ``TICK`` seconds; return its result and the tick ratio."""
    ticks = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(TICK)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    started = time.monotonic()
    try:
        result = await job
    finally:
        elapsed = time.monotonic() - started
        stop.set()
        await beat
    return result, ticks / (elapsed / TICK)
