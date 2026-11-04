"""asyncio in a process whose prelude reshaped the interpreter's threads.

A process prelude that reshapes the interpreter's blocking primitives can run many of the
process's threads on one OS thread. asyncio records the running loop per OS thread, so every
thread of that OS thread sees the loop of its OS thread as running. The standard library's
future chaining (``asyncio.futures._chain_future``, behind ``wrap_future``, ``to_thread``,
``run_in_executor`` and ``run_coroutine_threadsafe``) decides by that record whether the side
completing or cancelling a chained future is on the loop's own thread: on its thread it acts at
once, from any other it goes through ``call_soon_threadsafe``, which wakes the loop. In a
reshaped process another thread takes the first path, its callback is queued without waking the
loop, and a loop waiting in its selector with nothing else due never runs it.

:func:`bind_future_chaining` installs a future chaining that decides by thread identity instead —
``threading.get_ident()`` against the loop's recorded ``_thread_id``, the test the loop itself
applies to calls that must come from its own thread. The standard library's loops
(``asyncio.BaseEventLoop`` and its subclasses) record ``_thread_id`` while they run; loops of other
implementations, uvloop's among them, do not carry it, so a process that binds this chaining runs
the standard loop. Under real threads, for a standard loop the process itself runs, both tests give
the same answer; under a reshaping prelude only thread identity tells the loop's thread from the
others, because the prelude reshapes ``threading.get_ident`` per thread. The replacement is
module-level, so every standard loop of the process chains through it from the moment it is bound.
"""

from __future__ import annotations

import asyncio.futures
import concurrent.futures
import threading
from functools import partial
from typing import Any

#: The standard library's futures module, whose chaining is replaced here and whose helpers it
#: reuses: private names the type stubs do not declare.
_futures: Any = asyncio.futures


def _on_own_thread(loop: Any) -> bool:
    """Whether the calling thread is the thread ``loop`` runs on."""
    return loop._thread_id == threading.get_ident()


def _require_future(value: object, argument: str) -> None:
    if not _futures.isfuture(value) and not isinstance(value, concurrent.futures.Future):
        raise TypeError(f"A future is required for {argument} argument")


def _loop_of(future: Any) -> Any:
    """The loop of an asyncio future; ``None`` for a ``concurrent.futures`` one."""
    return _futures._get_loop(future) if _futures.isfuture(future) else None


def _set_state(future: Any, other: Any) -> None:
    if _futures.isfuture(future):
        _futures._copy_future_state(other, future)
    else:
        _futures._set_concurrent_future_state(future, other)


def _cancel_source(source: Any, source_loop: Any, destination: Any) -> None:
    """Cancel ``source`` once ``destination`` was cancelled: at once on its loop's thread, else through its loop."""
    if not destination.cancelled():
        return
    if source_loop is None or _on_own_thread(source_loop):
        source.cancel()
    else:
        source_loop.call_soon_threadsafe(source.cancel)


def _copy_to_destination(destination: Any, dest_loop: Any, source: Any) -> None:
    """Copy the completed ``source``'s state: at once on the destination loop's thread, else through it.

    A closed loop runs on no thread, so a state for a destination on a closed loop is dropped.
    """
    if dest_loop is None or _on_own_thread(dest_loop):
        _set_state(destination, source)
    elif not dest_loop.is_closed():
        dest_loop.call_soon_threadsafe(_set_state, destination, source)


def _chain_future(source: Any, destination: Any) -> None:
    """Chain two futures so that when one completes, so does the other.

    The result (or exception) of ``source`` is copied to ``destination``; a cancelled
    ``destination`` cancels ``source``. Either may be an asyncio or a ``concurrent.futures``
    future. A side completing or cancelling on its loop's own thread acts at once; from any other
    thread it hands the step to the loop with ``call_soon_threadsafe``.
    """
    _require_future(source, "source")
    _require_future(destination, "destination")
    destination.add_done_callback(partial(_cancel_source, source, _loop_of(source)))
    source.add_done_callback(partial(_copy_to_destination, destination, _loop_of(destination)))


def future_chaining_bound() -> bool:
    """Whether asyncio's future chaining is the one :func:`bind_future_chaining` installs."""
    return _futures._chain_future is _chain_future


def bind_future_chaining() -> None:
    """Make asyncio's future chaining decide "on the loop's thread" by thread identity, process-wide.

    Called once in a process started through a prelude that reshapes the interpreter's blocking
    primitives, before the process runs any event loop. Binding again when it is bound already
    leaves it as it is.

    Raises:
        RuntimeError: asyncio's future chaining is neither the standard library's own nor the one
            installed here — something else replaced it, and layering over it would hide which
            chaining runs.
    """
    current = _futures._chain_future
    if current is _chain_future:
        return
    if getattr(current, "__module__", None) != _futures.__name__:
        raise RuntimeError(
            f"cannot bind asyncio's future chaining: asyncio.futures._chain_future is {current!r} "
            f"from {getattr(current, '__module__', None)!r}, not the standard library's own"
        )
    _futures._chain_future = _chain_future
