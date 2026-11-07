"""The writer-exit edge a test stands in for when it drives a change below that edge.

In production every request, tool call and join runs under a writer-exit scope its edge opens and
settles (:mod:`tai42_skeleton.app.writer_exit`), and the edge admitting a change declared
``holds_writer_exit`` holds the worker's exit before the change runs. A test calling a route
handler, an operation or a config service directly stands in for that edge here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from tai42_skeleton.app import writer_exit
from tai42_skeleton.app.writer_exit import WriterExitScope, writer_exit_scope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tai42_skeleton.app.bus import WorkerBus

#: The change a stand-in edge admits.
ADMITTED_CHANGE = "the admitted change"


@asynccontextmanager
async def stand_in_edge() -> AsyncIterator[WriterExitScope]:
    """A writer-exit scope settled once the block ends, as the edge a test stands in for opens and settles it.

    The exit a change under it owes is settled when the block ends, so the hold it took is released
    and the exit runs on the test's bus.
    """
    with writer_exit_scope() as exits:
        yield exits
        await exits.settle()


def admit_change(bus: Any) -> None:
    """Hold the worker's exit on ``bus`` in the scope a stand-in edge opened, as that edge admits a declared change.

    A test driving a config door below its edge holds the exit the way the edge does, before the
    change runs. With no scope open no edge stands in, and the door requires no hold
    (:func:`~tai42_skeleton.app.writer_exit.require_writer_exit_held`).
    """
    scope = writer_exit._scope.get()
    if scope is not None and scope.is_open:
        scope.hold_exit(cast("WorkerBus", bus), ADMITTED_CHANGE)
