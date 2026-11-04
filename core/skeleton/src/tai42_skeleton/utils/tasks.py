"""Stopping a task this code owns."""

from __future__ import annotations

import asyncio
from typing import Any


async def cancel_and_wait(task: asyncio.Future[Any]) -> None:
    """Cancel ``task`` and return once it has ended; an exception it ended with is raised.

    The task's own cancellation is not raised: it is the end this call asked for. A cancellation of
    the caller while it waits is not the task's and propagates as itself, the task still winding
    down. A task already done is only waited on.
    """
    task.cancel()
    await asyncio.wait((task,))
    if not task.cancelled():
        task.result()
