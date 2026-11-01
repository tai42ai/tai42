"""A one-shot exec's stdin, fed while its output is read.

A command that writes its input back as it reads it (``cat``, a filter) takes no more stdin while
its output waits unread, so a stdin written whole before any output is read never finishes once
the pipes and the transport's buffers fill. :func:`read_while_feeding` runs the two at once, as
asyncio's ``Process.communicate`` does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def read_while_feeding(read: Callable[[], Awaitable[None]], feed: Callable[[], Awaitable[None]] | None) -> None:
    """Run ``read`` (the exec's output to its end) and ``feed`` (its stdin, then its end) together.

    With no ``feed`` the read runs alone. The call returns once both have ended. The first of
    the two to fail raises: the other is cancelled and awaited, and its own failure, if it has
    one, is noted on the raised error. A cancelled call cancels both.
    """
    if feed is None:
        await read()
        return
    tasks = {
        "stdin feed": asyncio.ensure_future(feed()),
        "output read": asyncio.ensure_future(read()),
    }
    # The order the two end in, so the first to fail is the one that raises.
    ended: list[str] = []
    for name, task in tasks.items():
        task.add_done_callback(lambda _task, name=name: ended.append(name))
    try:
        await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_EXCEPTION)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.wait(tasks.values())
    failures = [
        (name, error)
        for name in ended
        if not tasks[name].cancelled() and (error := tasks[name].exception()) is not None
    ]
    if not failures:
        return
    (_, first), *others = failures
    for name, error in others:
        first.add_note(f"the {name} also failed: {error!r}")
    raise first
