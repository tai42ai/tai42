"""The event loops that are closing, and the refusal every owner of a loop-bound resource raises on one.

A loop is closing once the code that ends it has marked it so, at the start of its end, before it
awaits the release of the loop's resources (:meth:`~tai42_kit.backend.PrivateLoop.close` does;
:func:`~tai42_kit.loop_bound.release_loop_bound_resources` itself marks nothing, since a loop that
lives on leases anew after a release). From then on every owner the kit keeps per loop (the pooled
clients, the resource registries) refuses a new lease on that loop with ``RuntimeError``, as a closed
connection pool refuses a new connection: a resource the release has already passed would otherwise
be opened anew and stay open past the loop's close. A lease held before the mark keeps working until it is released.

A loop stays closing for the rest of its life; the mark drops with the loop. The set is reached
from every thread that runs a loop, so it is read and written under a lock.
"""

import threading
from asyncio import AbstractEventLoop
from weakref import WeakSet

_closing_loops: WeakSet[AbstractEventLoop] = WeakSet()
_closing_lock = threading.Lock()


def mark_loop_closing(loop: AbstractEventLoop) -> None:
    """Mark ``loop`` as closing: every owner of a loop-bound resource refuses a new lease on it from now on."""
    with _closing_lock:
        _closing_loops.add(loop)


def loop_is_closing(loop: AbstractEventLoop) -> bool:
    """Whether ``loop`` has been marked closing."""
    with _closing_lock:
        return loop in _closing_loops


def refuse_lease_on_closing_loop(loop: AbstractEventLoop, owner: str, lease: str) -> None:
    """Raise ``RuntimeError`` when ``loop`` is closing, naming ``owner`` and the ``lease`` it refuses."""
    if loop_is_closing(loop):
        raise RuntimeError(
            f"{owner} cannot {lease} on event loop {loop!r}: the loop is closing, "
            "its loop-bound resources are being released"
        )
