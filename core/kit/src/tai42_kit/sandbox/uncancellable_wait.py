"""A wait on cleanup work that a cancellation of the caller does not cut short.

Cleanup that must finish once a call has failed or was cancelled (the kill of its command, the
teardown of a half-built session) runs as its own task, awaited through
:func:`wait_through_cancellation`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import anyio


async def wait_through_cancellation(task: asyncio.Future[Any]) -> asyncio.CancelledError | None:
    """Wait for ``task`` to end, through every cancellation of the caller; return the first one, or ``None``.

    ``asyncio.wait`` never cancels the task it waits on nor raises the task's own error, so the
    task runs to its end and its outcome stays on it for the caller to read. Once the caller is
    cancelled the wait goes on inside a shielded anyio scope (an anyio cancel scope re-delivers its
    cancellation at every wake-up, so a plain re-await would end at once) and through every further
    plain asyncio cancel. The caller re-raises the returned cancellation, or a cancellation of its
    own, once it has read the task's outcome.
    """
    try:
        await asyncio.wait([task])
    except asyncio.CancelledError as cancelled:
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.wait([task])
                except asyncio.CancelledError:
                    # A further plain asyncio cancel: the task's cancel count keeps it, and the
                    # wait goes on until the work ends.
                    continue
        return cancelled
    return None
