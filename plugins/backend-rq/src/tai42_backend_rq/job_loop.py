"""How a job's coroutine reaches an event loop: the rq job classes the worker pools dequeue their jobs as.

rq runs a job's function in ``Job._execute``; a function that returns a coroutine needs a loop to run
it on, and asyncio runs one loop per OS thread at a time. Each worker class declares its job class
(``job_class``), and rq fetches every job it dequeues as that class. Both classes run the function
the same way and differ only in the loop a coroutine it returns runs on.

``prefork`` and ``solo`` (:class:`PrivateLoopJob`) give each job's run an OS thread with no loop
running on it, so a job runs its coroutine on a fresh loop of its own
(:class:`~tai42_kit.backend.private_loop.PrivateLoop`), which ends with the job: the kit's loop-bound
resources (pooled clients, checkpoint savers, stores) are released on it, then the tasks left on it
end, and it closes.

``gevent`` (:class:`HostLoopJob`) runs the work loop as a green thread of the host's OS thread, where
the host already runs its loop: a job hands its coroutine to that loop
(:class:`~tai42_kit.backend.shared_loop.SharedLoop`) and waits for its result, its wait yielding to
the host's loop. The kit resources bound to the host's loop are the host's, released by its stop.

A function that returns no coroutine returns its value as rq returns it.
"""

from __future__ import annotations

import abc
import asyncio
import threading
from _thread import LockType
from collections.abc import Coroutine
from contextlib import closing
from typing import Any, ClassVar

from rq.job import Job
from tai42_kit.backend import PrivateLoop, SharedLoop


class _LoopJob(Job, abc.ABC):
    """A job whose function's coroutine runs on the loop its class runs coroutines on."""

    def _execute(self) -> Any:
        """Run the job's function; a coroutine it returns runs on the class's loop (:meth:`_run_coroutine`)."""
        if not self.func:
            raise ValueError("Cannot execute job: function is None")
        result = self.func(*self.args, **self.kwargs)
        if not asyncio.iscoroutine(result):
            return result
        return self._run_coroutine(result)

    @abc.abstractmethod
    def _run_coroutine(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coroutine``, the job function's, to completion and return its result, or raise its exception."""


class PrivateLoopJob(_LoopJob):
    """A job that runs its coroutine on a fresh loop of its own, the ``prefork`` and ``solo`` pools' job.

    The loop runs the coroutine as :meth:`~tai42_kit.backend.private_loop.PrivateLoop.run` states:
    a time limit raised out of the loop (rq's job timeout) cancels the coroutine's task and runs it
    to its end before the job raises it. The loop then ends whatever the coroutine ended with
    (:meth:`~tai42_kit.backend.private_loop.PrivateLoop.close`): the kit's loop-bound resources are
    released on it, the tasks left on it are cancelled and end, and it closes without waiting on
    the threads the job started. A release that fails raises, the coroutine's own error, when it
    raised, as its context.
    """

    # Job id -> the loop the job's coroutine runs on now (:meth:`wake`), and the lock that guards
    # it against the loop's close. A lock of the OS thread: this job runs on ``prefork`` and ``solo``,
    # which patch nothing, and its waker is rq's stop-job listener, another OS thread.
    _running: ClassVar[dict[str, asyncio.AbstractEventLoop]] = {}
    _running_guard: ClassVar[LockType] = threading.Lock()

    def _run_coroutine(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coroutine`` on a fresh loop, then end that loop."""
        with closing(PrivateLoop()) as private:
            with self._running_guard:
                self._running[self.id] = private.loop
            try:
                return private.run(coroutine)
            finally:
                with self._running_guard:
                    del self._running[self.id]

    @classmethod
    def wake(cls, job_id: str) -> None:
        """Wake the loop ``job_id``'s coroutine runs on, if it runs one now.

        An exception raised into the job's thread from another thread lands once the loop runs
        Python code again, which a loop waiting for its I/O does only when the wait ends; woken, it
        runs at once, and the exception ends the job as
        :meth:`~tai42_kit.backend.private_loop.PrivateLoop.run` states.
        """
        with cls._running_guard:
            loop = cls._running.get(job_id)
            if loop is not None:
                loop.call_soon_threadsafe(_nothing)


def _nothing() -> None:
    """The callback that wakes a loop: it does nothing, the loop's run after its wait is the point."""


class HostLoopJob(_LoopJob):
    """A job that hands its coroutine to the host's running loop, the ``gevent`` pool's job.

    The worker's runtime hands the loop in once per process (:meth:`share_loop`) before the work loop
    starts; a coroutine job performed with no loop handed raises ``RuntimeError`` and closes its
    coroutine.
    """

    _shared_loop: ClassVar[SharedLoop | None] = None

    @classmethod
    def share_loop(cls, loop: asyncio.AbstractEventLoop) -> None:
        """Hand every later job ``loop``, the host's running loop; a later hand replaces it."""
        cls._shared_loop = SharedLoop(loop)

    def _run_coroutine(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Hand ``coroutine`` to the host's loop and wait for its result."""
        shared_loop = self._shared_loop
        if shared_loop is None:
            coroutine.close()
            raise RuntimeError(
                "no host event loop was handed to the gevent pool's jobs; run the worker under `tai backend`"
            )
        return shared_loop.run(coroutine)
