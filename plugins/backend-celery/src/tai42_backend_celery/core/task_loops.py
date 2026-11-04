"""The event loops a task's jobs run their async work on.

asyncio runs one loop per OS thread at a time, so how a job reaches a loop follows how the pool
runs its jobs. A pool that gives every running job an OS thread of its own (``prefork``, ``solo``,
``threads``) lets each job run a loop itself: :class:`PrivateLoops` keeps one loop per job running
at once, each a :class:`~tai42_kit.backend.private_loop.PrivateLoop` that runs job after job and
ends as the kit ends such a loop. A green pool (``gevent``, ``eventlet``) runs every job of the
worker process, whatever its task, as a green thread of the process's one OS thread, which runs one
loop: the host's, which the launch serves on. The kit's
:class:`~tai42_kit.backend.shared_loop.SharedLoop` hands each job's coroutine to that loop, and the
job's wait for its result yields to the pool, so the jobs' async work interleaves on the one loop.

A private loop whose job's work could not be ended is closed by the job and leaves the open ones as
the job gives it back; the next job makes a loop anew. Each private loop releases the kit's
loop-bound resources (pooled clients, checkpoint savers, stores) on itself as it closes
(:meth:`PrivateLoops.close`, once the process that ran the jobs stops).

The host's loop is the host's to run: an exception its run raises, and a ``SystemExit`` or
``KeyboardInterrupt`` a job's task raises there, which asyncio throws out of the loop, end the
host's run of it and with it the process, the host ending every task left on the loop, the jobs'
among them, as it leaves. The kit resources bound to the host's loop are the host's, released by
its stop.

Pure Python cannot guard every instruction against an asynchronous exception (a signal handler's,
or one a green library throws), so an exception that lands in this module's own bookkeeping is
raised where it lands: on a job's own thread the job raises it, and in ``close``, ``close`` raises
it. One that lands as a job gives its loop back leaves that loop held by no job and served to none
again; ``close`` still ends it.
"""

from __future__ import annotations

import threading
from collections.abc import Coroutine
from typing import Any

from tai42_kit.backend import PrivateLoop


class PrivateLoops:
    """One loop per job running at once: the job runs it itself, and a free one serves the next job."""

    def __init__(self) -> None:
        """Start with no loop; the first job makes one."""
        self._lock = threading.Lock()
        self._free: list[PrivateLoop] = []
        self._open: list[PrivateLoop] = []

    def run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coroutine`` to completion as one job on a loop no other job is running, and return its result.

        The loop runs it as :meth:`~tai42_kit.backend.private_loop.PrivateLoop.run` states, and is
        free for the next job after, unless the job's work on it could not be ended and it closed.
        A job for which no loop could be taken closes ``coroutine``.
        """
        try:
            private = self._hold()
        except BaseException:
            coroutine.close()
            raise
        try:
            return private.run(coroutine)
        finally:
            self._give_back(private)

    def _hold(self) -> PrivateLoop:
        """Take a free open loop, or make one when none is."""
        with self._lock:
            private: PrivateLoop | None = None
            while private is None and self._free:
                private = self._free.pop()
                if private.closed:
                    self._open.remove(private)
                    private = None
            if private is None:
                private = PrivateLoop()
                self._open.append(private)
        return private

    def _give_back(self, private: PrivateLoop) -> None:
        """Free ``private`` for the next job; a closed one leaves the open loops instead."""
        with self._lock:
            if private in self._open:
                if private.closed:
                    self._open.remove(private)
                else:
                    self._free.append(private)

    @property
    def count(self) -> int:
        """How many loops are open."""
        with self._lock:
            return len(self._open)

    def close(self) -> None:
        """Close every loop (:meth:`~tai42_kit.backend.private_loop.PrivateLoop.close`), each even when another's fails.

        The failures are then raised together.
        """
        with self._lock:
            loops, self._open, self._free = self._open, [], []
        failures: list[Exception] = []
        for private in loops:
            try:
                private.close()
            except Exception as failure:
                failures.append(failure)
        if failures:
            raise ExceptionGroup("closing the task's event loops failed", failures)
