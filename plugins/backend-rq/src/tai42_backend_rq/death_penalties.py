"""How a job run in the worker process is ended: at its budget, or on a stop by RQ's stop-job command.

rq guards a job's body, and each of its callbacks, with a death penalty that raises the job's
timeout into it. The ``solo`` pool's penalty (:class:`WatchedTimerDeathPenalty`) raises it into
the job's OS thread with rq's timer; the ``gevent`` pool's (:class:`WatchedGeventDeathPenalty`)
throws it into the job's greenlet from its hub. Both record the section they guard, which the
worker's refresher reads to tell a section still running past its deadline, and raise a stop of
the job (:class:`JobStopped`) into that section the same way. A section takes one raise, the
budget's or the stop's, whichever comes first.
"""

from __future__ import annotations

import ctypes
import threading
import time
from _thread import LockType
from typing import Any, ClassVar

from rq.timeouts import BaseDeathPenalty, BaseTimeoutException, JobTimeoutException, TimerDeathPenalty

from tai42_backend_rq.job_loop import PrivateLoopJob
from tai42_backend_rq.unpatched import os_thread_lock


class JobStopped(BaseTimeoutException):
    """The stop of a running job by RQ's stop-job command, raised into the job by the penalty that guards it.

    It derives from rq's timeout base, as the job's timeout does, so rq takes it as it takes a late
    timeout. Landing inside rq's end of the guarded section, it is taken as that end and the job
    goes on as its body ended; landing as that end is entered, before it runs, it fails the job,
    which rq marks ``stopped`` even though its body completed. An exception raised into another
    thread is named by its class alone, so each stop is a class of its own (:meth:`of`) whose
    message names the job.
    """

    job_id: ClassVar[str]

    def __init__(self) -> None:
        """Name the job this stop ends; the job's id is the class's own."""
        super().__init__(f"job {self.job_id} stopped by RQ's stop-job command")

    @classmethod
    def of(cls, job_id: str) -> type[JobStopped]:
        """The stop of ``job_id``."""
        return type(
            cls.__name__, (cls,), {"job_id": job_id, "__module__": cls.__module__, "__qualname__": cls.__qualname__}
        )


class WatchedDeathPenalty(BaseDeathPenalty):
    """A job-budget penalty that records each section it guards while the section runs, and can stop it.

    rq guards a job's body, and each of its callbacks, with one penalty named by the job's id. The
    refresher of a non-forking worker reads the record (:meth:`overdue`) to tell a section still
    running past its deadline: the penalty's exception reaches the job within moments wherever it
    can, so a section still running a tick past its deadline has not ended on it, because it is
    blocked where the exception cannot reach or because it caught the exception and ran on. A stop
    of the job (:meth:`interrupt`) is raised into the section the penalty guards now, the way the
    job's timeout is.

    The penalty raises into its section at most once, by whichever comes first: the budget or a
    stop. The decision to raise, and the section's opening and its end, run under one guard, and
    the raising signal closes the section (:meth:`_close`) before that guard is released: no stop
    reaches the section any more and its budget is disarmed, so the other signal finds no section
    and raises nothing. On ``solo`` the raise itself is made under the guard; on ``gevent`` it is
    thrown into the job's greenlet from the hub once the guard is released, and a stop's throw
    lands only while the section still runs.
    The section's deadline stays recorded. A section that catches its raise and runs on is bounded
    by that deadline alone: once it runs a tick past it, the worker's refresher stops reporting and
    the liveness probe ends the worker. The records of a job's sections end with the job's run in
    the worker (:meth:`end_run`), whatever end its sections reached.
    """

    # Job id -> (monotonic deadline, timeout) of the section its penalty guards now.
    _armed: ClassVar[dict[str, tuple[float, float]]] = {}
    # Job id -> the penalty that guards the job's running section, which a stop raises into.
    _stoppable: ClassVar[dict[str, WatchedDeathPenalty]] = {}
    # Held while a raise is made into a section and while a section opens or ends, so nothing is
    # raised into a section before it has opened or once it has ended. A native lock: rq's stop-job
    # listener and rq's timer run on other OS threads on ``solo``.
    _raise_guard: ClassVar[LockType] = os_thread_lock()

    def __init__(
        self, timeout: float, exception: type[JobTimeoutException] = JobTimeoutException, **kwargs: Any
    ) -> None:
        """Guard the section of the job rq names by ``job_id`` (none: the section is not recorded)."""
        super().__init__(timeout, exception, **kwargs)
        self._job_id: str | None = kwargs.get("job_id")

    def __enter__(self) -> None:
        """Open the section; a raise that lands before the body starts ends the section here.

        No exit runs for a section whose entry raised, so its end runs here before the raise goes on.
        """
        try:
            self.setup_death_penalty()
        except BaseException:
            self.cancel_death_penalty()
            raise

    def setup_death_penalty(self) -> None:
        """Open the section under the guard: arm the penalty, record the deadline, and let a stop reach it."""
        with self._raise_guard:
            self._arm()
            if self._job_id is None:
                return
            if self._timeout > 0:
                self._armed[self._job_id] = (time.monotonic() + self._timeout, self._timeout)
            self._stoppable[self._job_id] = self

    def cancel_death_penalty(self) -> None:
        """End the section under the guard: close it, then drop its record.

        A raise that lands at the first instruction of this end skips it; the raise closed the
        section as it was made, and the section's deadline it leaves recorded is dropped as the job's
        run ends (:meth:`end_run`).
        """
        with self._raise_guard:
            try:
                self._close()
            finally:
                if self._job_id is not None:
                    self._armed.pop(self._job_id, None)

    @classmethod
    def overdue(cls, job_id: str, margin: float) -> float | None:
        """The timeout of ``job_id``'s guarded section when it still runs ``margin`` seconds past its deadline."""
        armed = cls._armed.get(job_id)
        if armed is None:
            return None
        deadline, timeout = armed
        return timeout if time.monotonic() > deadline + margin else None

    @classmethod
    def end_run(cls, job_id: str) -> None:
        """End the records of ``job_id``'s sections as the job's run ends, so no stop reaches the job any more."""
        with cls._raise_guard:
            penalty = cls._stoppable.get(job_id)
            if penalty is not None:
                penalty._close()
            cls._armed.pop(job_id, None)

    @classmethod
    def interrupt(cls, job_id: str) -> bool:
        """Raise the stop of ``job_id`` into its running section and close it; ``False`` when none is stoppable now.

        On ``gevent`` the stop is queued as a throw from the hub, which lands only while the section
        still runs.
        """
        with cls._raise_guard:
            penalty = cls._stoppable.get(job_id)
            if penalty is None:
                return False
            penalty._deliver(JobStopped.of(job_id))
            penalty._close()
            return True

    def _close(self) -> None:
        """Let no stop reach the section any more and disarm its budget; run under the guard, once or more."""
        if self._job_id is not None and self._stoppable.get(self._job_id) is self:
            del self._stoppable[self._job_id]
        self._disarm()

    def _arm(self) -> None:
        """Arm the section's budget, which raises the job's timeout into it and closes it (none for no budget)."""
        raise NotImplementedError

    def _deliver(self, stop: type[JobStopped]) -> None:
        """Raise ``stop`` into the section this penalty guards, which has not ended yet."""
        raise NotImplementedError

    def _disarm(self) -> None:
        """Disarm the section's budget, if it is armed."""
        raise NotImplementedError


class WatchedTimerDeathPenalty(WatchedDeathPenalty, TimerDeathPenalty):
    """The ``solo`` pool's penalty: rq's timer, raising the job's timeout and a stop of the job into its OS thread.

    The exception reaches the job at its next Python bytecode, so it cannot end a job blocked in C code
    before that code returns. Each raise also wakes the loop of the job's coroutine.
    """

    def handle_death_penalty(self) -> None:
        """On rq's timer thread at the budget: raise the job's timeout into the section and close it.

        The job's loop is woken as a stop wakes it (:meth:`_deliver`). The section's deadline stays
        recorded. A stop, or the section's end, may have closed the section while this callback was
        on the way; the callback then raises nothing.
        """
        with self._raise_guard:
            if self._timer is not threading.current_thread():
                return
            super().handle_death_penalty()
            if self._job_id is not None:
                PrivateLoopJob.wake(self._job_id)
            self._close()

    def _arm(self) -> None:
        """Start rq's timer, which raises the job's timeout at the budget (none for no budget)."""
        TimerDeathPenalty.setup_death_penalty(self)

    def _deliver(self, stop: type[JobStopped]) -> None:
        """Raise ``stop`` into the job's OS thread as rq's timer raises the timeout, and wake the job's loop.

        The loop of a job's coroutine waiting for its I/O runs no Python code until the wait ends;
        woken (:meth:`PrivateLoopJob.wake`), it runs at once and the stop lands.
        """
        thread_id = self._target_thread_id
        landed = (
            0
            if thread_id is None
            else ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), ctypes.py_object(stop))
        )
        if landed != 1:
            raise RuntimeError(f"the stop of job {stop.job_id} reached {landed} threads, not the job's one")
        PrivateLoopJob.wake(stop.job_id)

    def _disarm(self) -> None:
        """Cancel rq's timer, if it is armed."""
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()


class WatchedGeventDeathPenalty(WatchedDeathPenalty):
    """The ``gevent`` pool's penalty, which throws the job's timeout, and a stop of the job, into the job's greenlet.

    rq's timer cannot serve this pool: the patched process gives the timer a greenlet's identity
    to target, which is no thread, so the timer fails and the job runs on. The budget is a timer on
    the job's hub, and rq's stop-job listener is a greenlet on that hub, so both are thrown into the
    job's greenlet from the hub, once the job yields to it; a section that has ended by then is left
    alone. The exception reaches the job only when it yields, so it cannot end a job that holds the
    CPU or blocks in C code without yielding.
    """

    def __init__(
        self, timeout: float, exception: type[JobTimeoutException] = JobTimeoutException, **kwargs: Any
    ) -> None:
        """Hold the budget; its timer is started on the job's hub when the section opens."""
        super().__init__(timeout, exception, **kwargs)
        self._budget_timer: Any = None

    def setup_death_penalty(self) -> None:
        """Take the job's greenlet and hub, which the penalty throws into and from, then open the section."""
        import gevent

        self._greenlet: Any = gevent.getcurrent()
        self._hub: Any = gevent.get_hub()
        self._guarding = True
        super().setup_death_penalty()

    def cancel_death_penalty(self) -> None:
        """Mark the section ended, so a stop already queued on the hub leaves it alone, then end it."""
        self._guarding = False
        super().cancel_death_penalty()

    def _arm(self) -> None:
        """Start the budget's timer on the job's hub (none for no budget)."""
        if self._timeout <= 0:
            return
        timer = self._hub.loop.timer(self._timeout)
        self._budget_timer = timer
        timer.start(self._budget_fired, timer, update=True)

    def _budget_fired(self, timer: Any) -> None:
        """On the hub at the budget: close the section under the guard, then throw the job's timeout into it.

        The throw switches to the job's greenlet, whose section's end takes the guard, so it is made
        once the guard is released; nothing else runs on the hub in between. A section the stop or
        its own end has closed meanwhile has disarmed ``timer``, and is left alone.
        """
        with self._raise_guard:
            if self._budget_timer is not timer:
                return
            self._close()
        self._greenlet.throw(self._exception(f"Task exceeded maximum timeout value ({self._timeout} seconds)"))

    def _deliver(self, stop: type[JobStopped]) -> None:
        """Throw ``stop`` into the job's greenlet from the hub, if the section is still guarded then."""
        self._hub.loop.run_callback(self._throw_if_guarding, stop)

    def _disarm(self) -> None:
        """Stop and close the budget's timer, if it is armed."""
        timer, self._budget_timer = self._budget_timer, None
        if timer is not None:
            timer.stop()
            timer.close()

    def _throw_if_guarding(self, stop: type[JobStopped]) -> None:
        if self._guarding:
            self._greenlet.throw(stop)
