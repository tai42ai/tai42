"""A mutual-exclusion lock for the coroutines of one process, on whichever event loop and thread each runs.

An ``asyncio.Lock`` excludes the tasks of one event loop. A process that runs several loops at
once (a serving loop beside a backend job's private loop) needs a lock whose state is the
process's, not a loop's: the state here is a ``threading.Condition``, the hold is taken and
released on a dedicated thread through :func:`~tai42_kit.thread_wait.hold_on_thread`, and the
awaiting coroutine waits for it through a future, so no loop's thread blocks. Waiters take the
lock in arrival order.

A forking consumer calls :meth:`ProcessLock.reset_after_fork` from its
``os.register_at_fork(after_in_child=...)`` hook: a child inherits the parent's hold, which only
the parent's holder thread could release, and that thread does not exist in the child.

Import-safe: ``threading``, ``collections`` and :mod:`tai42_kit.thread_wait` only.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tai42_kit.thread_wait import hold_on_thread


class ProcessLock:
    """A lock whose holder may be a coroutine on any event loop, on any thread of the process.

    Not re-entrant: a holder that asks for the lock again waits for itself.
    """

    def __init__(self) -> None:
        """Create the lock free."""
        self._cond = threading.Condition()
        # The token of the hold that has the lock, ``None`` while it is free.
        self._holder: object | None = None
        # The waiting holds' tokens in arrival order; the head takes the lock next.
        self._queue: deque[object] = deque()

    @property
    def locked(self) -> bool:
        """Whether the lock is held; a plain read, safe from any thread and outside any loop."""
        return self._holder is not None

    @asynccontextmanager
    async def held(self) -> AsyncIterator[None]:
        """Hold the lock for the body, taken in arrival order.

        Cancellable while waiting (the wait is given up and the lock is never taken) and during
        the body (the lock is released on the way out, as on any exit); either way the release
        is observed before the cancellation propagates.
        """
        token = object()

        def acquire(stop: threading.Event) -> bool:
            with self._cond:
                self._queue.append(token)
                self._cond.wait_for(lambda: stop.is_set() or (self._holder is None and self._queue[0] is token))
                if stop.is_set():
                    self._queue.remove(token)
                    # The waiter behind this one may be the head now.
                    self._cond.notify_all()
                    return False
                self._queue.popleft()
                self._holder = token
                return True

        def release() -> None:
            with self._cond:
                # An acquire that gave up holds nothing to release.
                if self._holder is token:
                    self._holder = None
                    self._cond.notify_all()

        async with hold_on_thread(acquire, release, wake=self._wake, name="tai-process-lock"):
            yield

    def _wake(self) -> None:
        """Wake every waiting acquire so a stopped one re-reads its stop event."""
        with self._cond:
            self._cond.notify_all()

    def reset_after_fork(self) -> None:
        """Start a forked child with the lock free; for the fork-child hook only.

        The child inherits the parent's holder and waiters, whose threads are the parent's; the
        condition is replaced too, as the fork may have caught it held.
        """
        self._cond = threading.Condition()
        self._holder = None
        self._queue = deque()
