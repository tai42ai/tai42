"""The host's running event loop, shared with jobs that run on another thread of execution.

A backend whose pool runs its jobs as green threads of the host's one OS thread cannot give a job a
loop of its own: asyncio runs one loop per OS thread at a time, and the host already runs its loop
there. :class:`SharedLoop` hands each job's coroutine to the host's loop instead, and the job waits
for its result.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import logging
from collections.abc import Coroutine
from typing import Any

__all__ = ["SharedLoop"]

logger = logging.getLogger(__name__)


class SharedLoop:
    """A loop that already runs on another thread of execution, to which a job hands its coroutine and then waits.

    The loop is the host's: this object neither runs, stops nor closes it, and the kit's resources
    bound to it are the host's, released by its stop. A job's coroutine runs as a task of that loop,
    in the context the job handed it in, and the job's thread waits for the task's outcome. Where
    every job is a green thread of the loop's own OS thread, that wait yields to the other green
    threads, so the jobs' coroutines interleave on the loop with the host's own work. Jobs run on it
    while the host runs it; a job handed once it has closed, or once :meth:`close` has run, raises
    at once.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Hand the jobs ``loop``, the host's running loop."""
        self._loop = loop
        self._closed = False

    def run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coroutine`` to completion on the host's loop and return its result, or raise its exception.

        The job raises what the coroutine's task ends with, ``CancelledError`` when the task was
        cancelled (as the host's run ends every task left on its loop).

        An exception raised into the job's wait (its pool's time limit, a stop of the job) cancels the
        coroutine's task on the loop and waits on until that task has ended, its cleanup included:
        the job holds what it holds until its coroutine has ended, and then raises that exception.
        Each further exception raised into the wait is forwarded as another cancel the same way, and
        the job ends with the last, each earlier one as its context. An error the coroutine ends with
        meanwhile is logged at ERROR with its traceback, since the job raises the wait's exception. A
        coroutine that swallows the cancel is waited for until it returns, and the job still ends
        with the wait's exception.
        """
        job = self._hand(coroutine)
        ending: BaseException | None = None
        while not job.outcome.done():
            try:
                concurrent.futures.wait([job.outcome])
            except BaseException as interrupt:
                self._loop.call_soon_threadsafe(self._cancel, job)
                ending = _chained(interrupt, ending)
        if ending is None:
            return job.outcome.result()
        error = None if job.outcome.cancelled() else job.outcome.exception()
        if error is not None:
            logger.error(
                "the job ended with this error while its wait was interrupted; the job raises %r instead",
                ending,
                exc_info=(type(error), error, error.__traceback__),
            )
        raise ending

    def _hand(self, coroutine: Coroutine[Any, Any, Any]) -> _HandedJob:
        """Hand ``coroutine`` to the loop as a job, started there in the context it was handed in.

        Refused with a ``RuntimeError`` once :meth:`close` has run, or the host's loop has closed;
        a refused job closes its coroutine.
        """
        if self._closed:
            coroutine.close()
            raise RuntimeError("the jobs' shared event loop was let go; it runs no further job")
        job = _HandedJob(coroutine)
        try:
            self._loop.call_soon_threadsafe(self._start, job, context=job.context)
        except BaseException:
            coroutine.close()
            raise
        return job

    def _start(self, job: _HandedJob) -> None:
        """On the loop: make the job's task, whose end settles the job's outcome."""
        try:
            job.task = self._loop.create_task(job.coroutine)
        except Exception as error:
            job.coroutine.close()
            job.outcome.set_exception(error)
            return
        job.task.add_done_callback(functools.partial(self._settle, job))

    @staticmethod
    def _settle(job: _HandedJob, task: asyncio.Task[Any]) -> None:
        """On the loop: set the job's outcome from how its task ended."""
        if task.cancelled():
            # ``cancel`` alone wakes no ``concurrent.futures.wait``; the notify does.
            job.outcome.cancel()
            job.outcome.set_running_or_notify_cancel()
        elif (error := task.exception()) is not None:
            job.outcome.set_exception(error)
        else:
            job.outcome.set_result(task.result())

    @staticmethod
    def _cancel(job: _HandedJob) -> None:
        """On the loop: cancel the job's task; none was made when its start failed, which its outcome holds."""
        if job.task is not None:
            job.task.cancel()

    def close(self) -> None:
        """Let the host's loop go: every later job is refused; the loop and its kit resources stay the host's."""
        self._closed = True


class _HandedJob:
    """A job's coroutine handed to the shared loop, the context it was handed in, its task there, and its outcome."""

    def __init__(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        """Hand ``coroutine`` in the current context, with no task yet; the loop makes it, and settles ``outcome``."""
        self.coroutine = coroutine
        self.context = contextvars.copy_context()
        self.task: asyncio.Task[Any] | None = None
        self.outcome: concurrent.futures.Future[Any] = concurrent.futures.Future()


def _chained(interrupt: BaseException, earlier: BaseException | None) -> BaseException:
    """``interrupt``, with ``earlier``, the interrupt before it, as its context; the one to raise now.

    Each further interrupt keeps the one before, so the last carries them all. Raise it outside an
    ``except`` block: a raise inside one sets the context to the exception that block handles.
    """
    if earlier is not None and earlier is not interrupt:
        interrupt.__context__ = earlier
    return interrupt
