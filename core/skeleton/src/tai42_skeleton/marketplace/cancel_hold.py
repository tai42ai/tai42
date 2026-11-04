"""Run a marketplace operation's write to its end, holding a cancellation until it is done.

A write that changes the plugin environments, the manifest, the env store and the install
store row together must end whole: complete, or unwound when one of its steps fails. A
cancellation that cut it short would leave some of those changed and the rest not, so the
write runs as its own task and the awaiting task's cancellation is held until it ends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


async def run_to_end[T](write: Coroutine[Any, Any, T]) -> T:
    """Await ``write`` to its end, however often the awaiting task is cancelled meanwhile.

    ``write`` runs as its own task, which a cancellation of the awaiting task does not reach. A
    cancellation that lands meanwhile is held until ``write`` has ended: when ``write`` completed
    it is re-raised and the result dropped; an error ``write`` raised propagates instead, with the
    held cancellation as its ``__context__``. A cancellation of ``write``'s own task (the loop shutting down)
    propagates as it is.
    """
    running = asyncio.ensure_future(write)
    cancelled: asyncio.CancelledError | None = None
    while not running.done():
        try:
            # ``wait`` neither cancels the write nor raises its error: it only returns once it is done.
            await asyncio.wait((running,))
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
    try:
        result = running.result()
    except BaseException as error:
        if cancelled is not None and error is not cancelled:
            error.__context__ = cancelled
        raise
    if cancelled is not None:
        raise cancelled
    return result
