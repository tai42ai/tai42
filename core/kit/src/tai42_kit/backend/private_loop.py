"""An event loop a job's own thread runs itself, one job at a time, and the end of that loop.

A backend whose pool gives every running job an OS thread with no loop running on it lets the job
run its coroutine on a loop of its own: :class:`PrivateLoop`. A backend may make one for each job
and close it as the job ends, or keep one for job after job and close it as the process that ran
them stops; each run is one job's, and :meth:`PrivateLoop.close` ends the loop the same way for both.

Every run of the loop makes it the thread's current event loop while it runs and leaves the thread
with none after, as ``asyncio.Runner`` does, and runs it until the future it waits for is done and
the callbacks that end scheduled have run. A task that raises a ``SystemExit`` or
``KeyboardInterrupt`` ends with it, and asyncio throws the same exception out of the call that runs
the loop as well; the loop keeps a record of its tasks and reads the exit a task ended with, so the
run tells a task's exit, after which the loop runs on and the exit reaches the job it belongs to by
the ordinary await, from an exception raised in the loop's own code by no task (a signal handler, a
pool's time limit raised into the job's thread, ``loop.stop()``, a done callback raising a task's
exit again, the loop failing to run at all).

An exception raised by no task ends the job: the job's task is cancelled and run to its end, its
cleanup included, and the job then raises the exception. A further exception while that task ends
(a signal in the wind-down's own code, an exception the loop raises on every run) ends that and is
what the job ends with, the first exception as its context; the loop so left with its work unended
is closed at once (:meth:`PrivateLoop.close`) and serves no further job.

The kit keeps its loop-bound resources (pooled clients, checkpoint savers, stores) per loop, and the
loop's close ends the loop in the order ``asyncio.Runner`` does, with the kit's release as the
application's own clean-up that runs inside the run, before the runner's close: after the mark below, the
release runs on the loop itself, while every task left on it is still alive, since a resource may own
tasks of its own (a connection pool's workers) that its close awaits to their end. Every task left
on the loop is then cancelled and run to its end, round after round until no task is left, so a
task made while the others end (one a task's cleanup starts, one the release leaves running, one a
thread hands the loop) is ended as well; the async generators left suspended on it are then closed
and the tasks their close made ended the same way, and the loop closes. Before the release, the close
marks the loop closing (:mod:`tai42_kit.loop_closing`): a task that leases a pooled client or a registry from
then on (one still running, or one in its cleanup) gets a ``RuntimeError`` naming the loop as
closing, as a closed connection pool refuses a new connection, so nothing is opened that the release
would not close; a lease the task held from before keeps working until it is released. Work that
starts itself anew each time it is cancelled keeps the close running, as a task that ignores its
cancel keeps ``asyncio.run`` waiting.

The loop's default executor (the threads of ``asyncio.to_thread`` and ``run_in_executor(None, ...)``)
is shut down as the loop closes without waiting for its threads, where ``asyncio.run`` joins them:
a thread cannot be cancelled, so a job that awaited one is not held by it once the exception that
ends the job has reached the loop, and the thread ends on its own, its result unread. When that
exception reaches a loop waiting for a thread is the pool's to say: a signal handler's interrupts
the loop's wait at once, while an exception raised into the job's thread from another thread lands
only once the loop runs Python code again, which a loop waiting for its I/O does not do until the
wait ends (with the thread's end, at the latest). A thread that hands the loop work after its close
fails at once with asyncio's ``RuntimeError``.

Pure Python cannot guard every instruction against an asynchronous exception (a signal handler's,
or one a pool raises into the thread), so an exception that lands in this module's own code is
raised where it lands: on the job's thread the job raises it, and in ``close``, ``close`` raises it.
Inside a callback the loop runs, asyncio itself catches it: a ``SystemExit`` or
``KeyboardInterrupt`` it throws out of the loop, which ends the job as above; any other exception it
logs at ERROR and runs on, so the job does not raise it and what that callback was to do stays
undone. The callback by which this module ends a run stops the loop even when such an exception
lands in its ``loop.stop()`` call, so the run still ends, as its work did.

Two windows stay open. An exception that lands at the first instruction of that callback, or in
asyncio's own scheduling of it as the work ends, is logged before the loop is stopped, and the run
waits on until the process is ended from outside, as a run of ``asyncio.run`` does in the same
case. A thread's hand-off that reaches the loop from the start of the close's last pass over it until
its closing is dropped with the loop's pending callbacks, and a thread that waits for that
hand-off's result waits on.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import weakref
from collections.abc import Callable, Coroutine
from typing import Any, cast

from tai42_kit.loop_bound import release_loop_bound_resources
from tai42_kit.loop_closing import mark_loop_closing

__all__ = ["PrivateLoop"]

logger = logging.getLogger(__name__)


class PrivateLoop:
    """An event loop of a job's own, run by the job's thread itself, one job at a time, until :meth:`close`."""

    def __init__(self) -> None:
        """Make the loop, with a record of the tasks it makes."""
        self._loop = asyncio.new_event_loop()
        self._loop.set_task_factory(_LoopRecord(self._loop))

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The asyncio loop the jobs run on."""
        return self._loop

    @property
    def closed(self) -> bool:
        """Whether the loop is closed: by :meth:`close`, by a job whose work on it could not be ended, or directly."""
        return self._loop.is_closed()

    def run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coroutine`` to completion on the loop as one job, and return its result.

        The job raises what the coroutine's task ends with, a ``SystemExit`` or ``KeyboardInterrupt``
        included: the loop runs on after a task's exit, so an exit from any task of the coroutine
        reaches its awaiter as any exception does, and the loop serves the next job.

        An exception raised by no task (a signal handler, a time limit raised into the job's thread,
        ``loop.stop()``, a done callback raising a task's exit again) cancels the coroutine's task
        and runs the loop until that task has ended, its cleanup included, and is then raised; an
        error the task ends with after that cancel is logged at ERROR with its traceback, and so is
        one the task had already ended with when the exception came, the log saying it came before.

        A further exception while the task is cancelled and ends ends that: the job raises it, the
        first as its context, and the loop is closed (:meth:`close`). A job that holds no task of
        its coroutine closes the coroutine: the loop is closed, its task factory was replaced, or an
        exception landed as the task was made, before the job held it. In that last case the task
        stays on the loop, and the loop's next run runs it: it fails at once on the closed
        coroutine, which asyncio logs at ERROR as an exception never retrieved, and that run's own
        job is unaffected.
        """
        loop = self._loop
        task: asyncio.Task[Any] | None = None
        try:
            record = _LoopRecord.of(loop)
            task = loop.create_task(coroutine)
            try:
                record.run_until_done(task)
            except BaseException as outside:
                try:
                    cancelled = task.cancel()
                    record.run_until_done(task)
                except BaseException:
                    self.close()
                    raise
                _log_the_error_the_job_does_not_raise(task, raised=outside, cancelled=cancelled)
                raise
            return task.result()
        finally:
            if task is None:
                coroutine.close()

    def close(self) -> None:
        """Release the kit's resources for the loop, end the work left on it, close its async generators, close it.

        Once this thread can run the loop, the loop is marked closing, then the kit's release runs
        while the tasks left on the loop are still alive, so a resource whose close awaits tasks of
        its own finds them running; from the mark a new lease of a pooled client or a registry on
        the loop raises ``RuntimeError``. Every task on the loop is then cancelled and the loop run
        until each has ended, round after round until no task is left, as ``asyncio.run`` ends its
        tasks on its way out; an error a task ends with then is logged at ERROR with its traceback.
        The async generators left suspended on the loop are then closed, followed by the same end of
        the tasks their close made. A release that fails raises once all of that has run. A step
        that raises ends the steps after it, and the loop still closes. A loop running (on another
        thread) cannot be closed: the close raises ``RuntimeError``, leaves the loop running and
        unmarked, and releases nothing. The loop's default executor is shut down without waiting
        for its threads (the module docstring tells why). A loop already closed is left as it is.
        """
        loop = self._loop
        if loop.is_closed():
            return
        try:
            record = _LoopRecord.of(loop)
            # Marked only once this thread can run the loop: a close refused here leaves a loop
            # that runs on unmarked, still leasing.
            record.raise_unless_runnable()
            mark_loop_closing(loop)
            release = _run_to_its_end(record, release_loop_bound_resources)
            _run_to_its_end(record, loop.shutdown_asyncgens)
            release.result()
        finally:
            loop.close()


class _LoopRecord:
    """What this module keeps of one loop: its tasks, and the run that drives it now.

    Installed as the loop's task factory, so it sees every task the loop makes (``create_task``,
    ``ensure_future``, ``gather``, ``TaskGroup``, ``run_coroutine_threadsafe``) and keeps each one
    while it lives. asyncio sets a ``SystemExit`` or ``KeyboardInterrupt`` on the task that raised
    it and throws the same object out of the loop in the same step, so the run that catches it asks
    :meth:`claims` before anything else runs on the loop; each task's exit is claimed once, so the
    same object thrown again by no task (a done callback raising it again) is not a task's. The exit
    a finished task holds is read as asyncio's own repr and stack code read it, ``Task._exception``,
    which marks nothing retrieved; the tasks keep their names, coroutines and stacks.

    ``run`` is the token of the :meth:`run_until_done` call driving the loop, ``None`` between
    runs: the stop that ends a run is the one that call scheduled, and no other.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        """The record of ``loop``, with no task and no run yet."""
        self.loop = loop
        self._tasks: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()
        self.run: object | None = None

    def __call__(
        self, loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, Any], **kwargs: Any
    ) -> asyncio.Task[Any]:
        """Make the task of ``coro`` on ``loop`` and keep it."""
        task = Task(coro, loop=loop, **kwargs)
        self._tasks.add(task)
        return task

    def claims(self, escaped: BaseException) -> bool:
        """Whether a kept task of the loop ended with ``escaped``; the task is then kept no longer."""
        for task in self._tasks:
            if task.done() and not task.cancelled() and _exception_of(task) is escaped:
                self._tasks.discard(task)
                return True
        return False

    @staticmethod
    def of(loop: asyncio.AbstractEventLoop) -> _LoopRecord:
        """The record of ``loop``, made by :class:`PrivateLoop`; ``TypeError`` when its task factory was replaced."""
        factory = loop.get_task_factory()
        if not isinstance(factory, _LoopRecord):
            raise TypeError(
                f"the job loop's task factory is {factory!r}, not the one that records its tasks; something replaced it"
            )
        return factory

    def run_until_done(self, future: asyncio.Future[Any]) -> None:
        """Run the loop until ``future`` is done and the callbacks its end scheduled have run.

        The loop is the thread's current event loop while it runs, and the thread has none after, as
        ``asyncio.Runner`` leaves it. A thread whose own event loop is running cannot run it: the run
        raises ``RuntimeError`` before it touches the thread's current loop. Whether the run is over is
        read from ``future`` itself each time the loop returns; the run's own callback on ``future``
        (:meth:`_stop_for`) only stops the loop as ``future`` ends. When an exception cut the loop's
        pass short after ``future`` was done, one more pass runs the callbacks its end scheduled.

        A ``SystemExit`` or ``KeyboardInterrupt`` thrown out of the loop by a task that ended with
        it is that task's (:meth:`claims`): the loop runs on, and the exit reaches the task's
        awaiter. Any other exception out of the loop is raised by no task, and leaves here.
        ``loop.stop()`` called by anything else while ``future`` is pending raises ``RuntimeError``
        here, as asyncio's own ``run_until_complete`` does; the stop callback of an earlier run, left
        on the loop when an exception cut that run short after its future was done, stops nothing.
        """
        loop = self.loop
        self.raise_unless_runnable()
        run = object()
        stop = functools.partial(self._stop_for, run)
        self.run = run
        try:
            future.add_done_callback(stop)
            asyncio.set_event_loop(loop)
            while True:
                if future.done():
                    loop.stop()
                try:
                    loop.run_forever()
                except BaseException as escaped:
                    if self.claims(escaped):
                        continue
                    raise
                if not future.done():
                    raise RuntimeError("loop.stop() was called on the job loop before the work it ran had ended")
                return
        finally:
            future.remove_done_callback(stop)
            self.run = None
            asyncio.set_event_loop(None)

    def raise_unless_runnable(self) -> None:
        """Raise ``RuntimeError`` when this thread cannot run the loop: it runs already, or another loop runs here."""
        if self.loop.is_running():
            raise RuntimeError("this event loop is already running")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Cannot run the event loop while another loop is running")

    def _stop_for(self, run: object, _: asyncio.Future[Any]) -> None:
        """Stop the loop when ``run`` is the run driving it now.

        An exception that lands in the ``loop.stop()`` call (one raised into the thread at the first
        instruction of ``loop.stop()``, before it has stopped the loop) stops the loop all the same,
        by a second ``loop.stop()``, then goes on to asyncio, which logs it at ERROR, or throws it out
        of the loop when it is a ``SystemExit`` or ``KeyboardInterrupt``. One that asyncio logs and
        that lands before the ``loop.stop()`` call, at this callback's own first instruction, leaves
        the run waiting: the module docstring names that window.
        """
        if self.run is run:
            try:
                self.loop.stop()
            except BaseException:
                self.loop.stop()
                raise


class Task(asyncio.Task[Any]):
    """A task of a job loop, named as asyncio names any task.

    ``BaseEventLoop.create_task`` calls ``set_name(name)`` on a task a factory made even when no
    name was given (CPython 3.13), which would name every such task ``'None'``; no name keeps the
    one the task has. The class carries asyncio's own name because asyncio prints a task's class
    name in its repr and in its "exception was never retrieved" report, so a job loop's tasks read
    there as any task does.
    """

    def set_name(self, value: object) -> None:
        """Name the task ``value``; ``None`` leaves its name as it is."""
        if value is not None:
            super().set_name(value)


def _exception_of(task: asyncio.Task[Any]) -> BaseException | None:
    """The exception a finished ``task`` ended with, read without marking it retrieved (``Task._exception``)."""
    return cast(Any, task)._exception


def _end_every_task(record: _LoopRecord) -> None:
    """Cancel every task on the record's loop and run it until each has ended, round after round until none is left.

    A task made while the others end is cancelled and ended in the next round. An error a task ends
    with is logged at ERROR with its traceback.
    """
    while left := asyncio.all_tasks(record.loop):
        for task in left:
            task.cancel()
        for task in left:
            record.run_until_done(task)
        for task in left:
            if not task.cancelled() and (error := task.exception()) is not None:
                logger.error(
                    "a task left on the job loop ended with this error after the loop's close cancelled it: %r",
                    task,
                    exc_info=(type(error), error, error.__traceback__),
                )


def _run_to_its_end(record: _LoopRecord, step: Callable[[], Coroutine[Any, Any, Any]]) -> asyncio.Task[Any]:
    """Run ``step()`` as a task of the record's loop until it is done, then end every task left; return its task.

    A step of the close, not a job's work: its own outcome is read from the task it returns. The
    coroutine is made only once the thread can run the loop, so a run refused (a loop running here
    already) leaves no coroutine unawaited; an exception out of the loop's run before the step has
    begun (a loop that fails on every run) closes the step's coroutine unrun, then is raised.
    """
    record.raise_unless_runnable()
    coroutine = step()
    task = record.loop.create_task(coroutine)
    try:
        record.run_until_done(task)
    except BaseException:
        if inspect.getcoroutinestate(coroutine) == inspect.CORO_CREATED:
            coroutine.close()
        raise
    _end_every_task(record)
    return task


def _log_the_error_the_job_does_not_raise(task: asyncio.Task[Any], *, raised: BaseException, cancelled: bool) -> None:
    """Log at ERROR the error a job's ``task`` ended with, the job raising ``raised`` instead.

    ``cancelled`` tells whether the task was cancelled for ``raised`` or had already ended when it
    came; the log says which. Nothing is logged when the task ended with no error or with the cancel.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    when = (
        "ended with this error after it was cancelled"
        if cancelled
        else "had ended with this error before an exception raised by none of its tasks reached the job"
    )
    logger.error(
        "the job's coroutine %s; the job raises %r instead",
        when,
        raised,
        exc_info=(type(error), error, error.__traceback__),
    )
