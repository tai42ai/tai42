"""The kill seam: the platform tells every driver about every node of a run it takes down.

A kill ends a run. The platform revokes every park of the run itself (pruned, its ticket cleared,
its channel delivery withdrawn) BEFORE any driver is told, then fires this seam once per NODE of
the run: every park id of the run and every chained-call key any of its parks is nested under.
Drivers register a handler here at import; each handler ends the super-step the node belongs to in
its OWN durable state (its checkpoint, its resolution records) and resumes and notifies nobody —
the platform reaches every caller and every nested run of the killed run through this same seam,
and delivers the run's single terminal afterward.

A handler is fired with ``(node, reason)`` and returns whether its driver OWNS the node: ``True``
when it ended the live super-step the node belongs to, or a resolved or detached tombstone stands
for it; ``False`` when it never indexed the node. A handler that cannot finish yet (a lease a live
drive holds, a barrier gone while its entry is live, a nested child's link still pending) raises
:class:`ParkKillNotReadyError`; the platform keeps the kill's durable record and redelivers it, so
every handler must be idempotent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeAlias

__all__ = [
    "ParkKillHandler",
    "ParkKillNotReadyError",
    "ParkKillOwnerMissingError",
    "fire_park_killed",
    "register_park_kill_handler",
]


class ParkKillNotReadyError(RuntimeError):
    """A kill step cannot finish yet; the platform keeps the kill's durable record and redelivers it."""

    def __init__(self, node: str, detail: str) -> None:
        """Name the ``node`` whose kill step cannot finish and ``detail``, why."""
        super().__init__(f"kill of {node!r} is not ready: {detail}")
        self.node = node


class ParkKillOwnerMissingError(ParkKillNotReadyError):
    """No registered driver owns a node every kill must find an owner for (see :func:`fire_park_killed`)."""


ParkKillHandler: TypeAlias = Callable[[str, str], Awaitable[bool]]  # noqa: UP040

# Every registered handler with the continuation tool its driver binds, in registration order.
_park_kill_handlers: list[tuple[str, ParkKillHandler]] = []


def register_park_kill_handler(handler: ParkKillHandler, *, continuation_tool: str) -> None:
    """Register a driver's kill handler; ``continuation_tool`` names the resume continuation the driver binds.

    The parks a driver owns carry ``continuation_tool`` as their continuation, so a kill of such a
    park must find this driver owning it. A driver registers once at import; handlers accumulate,
    so the platform reaches every driver from one fire.
    """
    _park_kill_handlers.append((continuation_tool, handler))


async def fire_park_killed(node: str, reason: str, *, continuation_tool: str | None) -> None:
    """Fire every registered handler for ``node`` of a killed run, in registration order.

    ``node`` is a park id (``continuation_tool`` its continuation) or a chained-call key
    (``continuation_tool`` None). A handler that raises propagates at once (a later handler is not
    fired this pass) and the platform redelivers. When no handler owned the node this raises
    :class:`ParkKillOwnerMissingError` iff the node must be owned: a chained-call key (a parking
    driver minted it and parks on it or detaches it), or a park whose continuation tool a
    registered driver binds (that driver has not recorded its park yet). A park whose continuation
    no registered driver binds belongs to a driver that keeps no state, and nobody owning it is no
    error.
    """
    owned = False
    for _tool, handler in _park_kill_handlers:
        if await handler(node, reason):
            owned = True
    if owned:
        return
    if continuation_tool is None:
        raise ParkKillOwnerMissingError(node, "no driver owns this chained call yet")
    if any(tool == continuation_tool for tool, _handler in _park_kill_handlers):
        raise ParkKillOwnerMissingError(node, f"its driver ({continuation_tool!r}) has not recorded this park yet")
