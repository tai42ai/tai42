"""Await a wait or a hold that blocks a thread, without blocking the event loop.

A wait on a ``threading`` primitive (a :class:`threading.Condition`, an event) blocks the
thread it runs on, so it cannot run on an event loop's thread without stopping every other
task there. These helpers run it on a dedicated thread of its own, never the loop's default
executor (the work the wait is for may need that executor), and the awaiting coroutine waits
for it through a future the thread settles, so the loop keeps turning meanwhile.

A cancellation of the awaiting task signals the thread to stop at once: the helper sets the
thread's stop event, then calls ``wake`` so a thread asleep on a condition re-reads it (setting
an event alone does not wake a ``Condition.wait_for``). Either way the helper returns only once
the thread has finished, so nothing the thread does outlives the call. An error the thread
raises is raised to the awaiting coroutine.

Import-safe: ``threading`` and ``asyncio`` only.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager


async def wait_on_thread(wait: Callable[[threading.Event], None], *, wake: Callable[[], None], name: str) -> None:
    """Run ``wait(stop)`` on a dedicated thread named ``name`` and await its end.

    ``wait`` returns early once ``stop`` is set and ``wake`` has been called; it must not run
    on past that. A cancellation of the awaiting task stops the wait, waits for the thread to
    finish, and then propagates.
    """
    loop = asyncio.get_running_loop()
    ended: asyncio.Future[None] = loop.create_future()
    stop = threading.Event()

    def run() -> None:
        try:
            wait(stop)
        except BaseException as exc:
            loop.call_soon_threadsafe(_resolve, ended, exc)
        else:
            loop.call_soon_threadsafe(_resolve, ended, None)

    threading.Thread(target=run, name=name, daemon=True).start()
    try:
        await asyncio.wait({ended})
    except asyncio.CancelledError:
        stop.set()
        wake()
        await _thread_end(ended)
        raise
    ended.result()


@asynccontextmanager
async def hold_on_thread(
    acquire: Callable[[threading.Event], bool],
    release: Callable[[], None],
    *,
    wake: Callable[[], None],
    name: str,
) -> AsyncIterator[None]:
    """Take a hold on a dedicated thread named ``name`` and keep it for the body.

    The thread runs ``acquire(stop)``, which returns whether it took the hold (``False``: it
    gave up because ``stop`` was set), then holds until the body is over and runs ``release()``
    on that same thread, whether or not the acquire took the hold, so a hold that must be
    released by the thread that took it is. The body runs on the loop once the hold is taken.

    Cancellable while the hold is being taken (the thread gives up the acquire and the
    cancellation propagates) and during the body (released on the way out, as on any exit).
    The release is observed before this returns, so a caller that acts right after it does not
    race the thread.
    """
    loop = asyncio.get_running_loop()
    held: asyncio.Future[None] = loop.create_future()
    ended: asyncio.Future[None] = loop.create_future()
    stop = threading.Event()

    def run() -> None:
        try:
            try:
                if acquire(stop):
                    loop.call_soon_threadsafe(_resolve, held, None)
                    stop.wait()
                else:
                    # The awaiting side went away first and its future is already settled:
                    # this keeps the rule that nothing awaits a future no one settles.
                    loop.call_soon_threadsafe(_resolve, held, _AcquireAbandonedError())
            finally:
                release()
        except BaseException as exc:
            loop.call_soon_threadsafe(_resolve, held, exc)
            loop.call_soon_threadsafe(_resolve, ended, exc)
        else:
            loop.call_soon_threadsafe(_resolve, ended, None)

    threading.Thread(target=run, name=name, daemon=True).start()
    try:
        await held
    except BaseException:
        stop.set()
        wake()
        await _thread_end(ended)
        ended.result()
        raise
    try:
        yield
    finally:
        stop.set()
        wake()
        await _thread_end(ended)
        ended.result()


async def _thread_end(ended: asyncio.Future[None]) -> None:
    """Wait until the thread has settled ``ended``, then raise any cancellation that came meanwhile.

    The thread was told to stop, so the wait is short; returning before it ends would let
    whatever the thread still does outlive its caller. So a cancellation of the awaiting task
    does not cut the wait short: it is held until the thread has ended and raised then.
    """
    cancelled: asyncio.CancelledError | None = None
    while not ended.done():
        try:
            await asyncio.wait({ended})
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        raise cancelled


class _AcquireAbandonedError(RuntimeError):
    """The acquire on the thread gave up before it took the hold (its awaiter went away)."""


def _resolve(future: asyncio.Future[None], exc: BaseException | None) -> None:
    """Settle ``future`` from the thread, on the loop.

    A future the loop already settled is left as it is: a cancelled awaiter cancels the future
    it awaited.
    """
    if future.done():
        return
    if exc is None:
        future.set_result(None)
    else:
        future.set_exception(exc)
