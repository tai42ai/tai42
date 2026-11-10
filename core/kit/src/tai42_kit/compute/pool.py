"""``ComputePool`` — CPU-bound jobs in worker processes, awaited from an event loop.

A job runs in a separate interpreter (start method ``spawn``), so a job that holds its GIL
leaves the caller's event loop free. A job is a picklable callable (a module-level function)
and picklable arguments; it crosses to the worker and back as a :mod:`~tai42_kit.compute.job`
payload, so what the job raises, importing its module included, comes back as its own outcome.

A job's ``timeout`` counts from the moment a worker takes it up (time spent waiting for a free
worker is not counted). When it runs out, the worker is terminated at once, whether the job runs
Python code or sits inside a C call, a fresh worker replaces it, and the caller gets
``TimeoutError``. Cancelling the awaiting task terminates the job's worker the same way.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import signal
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from pebble import ProcessExpired, ProcessFuture, ProcessPool

from tai42_kit.compute.errors import ComputePoolUnavailableError, ComputeWorkerLostError
from tai42_kit.compute.job import JobOutcome, outcome_value, pack, run_job

_STOPPED_BEFORE_THE_JOB_FINISHED = "the compute pool stopped before the job finished"


class ComputePool:
    """A pool of ``workers`` worker processes running picklable jobs.

    ``start`` launches the workers and ``stop`` ends them; a pool runs once. ``run`` on a pool
    that is not running raises :class:`~tai42_kit.compute.errors.ComputePoolUnavailableError`:
    a job never runs anywhere but in a worker.
    """

    def __init__(self, workers: int) -> None:
        """Size the pool at ``workers`` processes (at least one); nothing starts until :meth:`start`."""
        if workers < 1:
            raise ValueError(f"a compute pool needs at least one worker, got {workers}")
        self._workers = workers
        self._lock = threading.Lock()
        self._pool: ProcessPool | None = None
        self._stopped = False
        self._in_flight: set[Future[Any]] = set()
        self._in_flight_at_stop: frozenset[Future[Any]] = frozenset()

    @property
    def workers(self) -> int:
        """The number of worker processes the pool runs."""
        return self._workers

    @property
    def broken(self) -> bool:
        """Whether the library reports the running pool failed, as it does once an idle worker died.

        A broken pool refuses every job until it is stopped. A pool not started or stopped is not broken.
        """
        with self._lock:
            pool = self._pool
        # ``active`` is false once the library put a running pool into its error state.
        return pool is not None and not pool.active

    def start(self) -> None:
        """Launch the worker processes.

        Raises ``RuntimeError`` when the pool is already running and
        :class:`~tai42_kit.compute.errors.ComputePoolUnavailableError` when it was stopped.
        """
        with self._lock:
            if self._stopped:
                raise ComputePoolUnavailableError("the compute pool was stopped; a stopped pool does not start again")
            if self._pool is not None:
                raise RuntimeError("the compute pool is already running")
            pool = ProcessPool(
                max_workers=self._workers,
                initializer=_terminate_on_sigterm,
                context=multiprocessing.get_context("spawn"),
            )
            # ``active`` starts a created pool's workers and its manager threads.
            if not pool.active:
                raise ComputePoolUnavailableError("the compute pool's workers did not start")
            self._pool = pool

    def stop(self) -> None:
        """End the worker processes; a job still pending or running raises in its caller.

        Stopping a pool that never started only marks it stopped.
        """
        with self._lock:
            pool, self._pool = self._pool, None
            self._stopped = True
            self._in_flight_at_stop = frozenset(self._in_flight)
        if pool is None:
            return
        pool.stop()
        pool.join()
        # Joined: no pool thread settles a future any more.
        for future in self._in_flight_at_stop:
            if not future.done():
                future.set_exception(ComputePoolUnavailableError(_STOPPED_BEFORE_THE_JOB_FINISHED))

    async def run[T, *Ts](self, fn: Callable[[*Ts], T], /, *args: *Ts, timeout: float | None = None) -> T:
        """Run ``fn(*args)`` in a worker process and return its result.

        An exception ``fn`` raises is raised here as itself, the worker's traceback chained as its
        cause (:class:`~tai42_kit.compute.errors.WorkerTraceback`); one that cannot be rebuilt here
        raises :class:`~tai42_kit.compute.errors.ComputeJobFailedError` carrying its type and
        message, with the same cause. A job whose module fails to import in the worker raises
        that import error the same way. ``timeout`` (seconds, ``None`` for none) bounds the
        job once a worker takes it up: past it the worker is terminated at once and
        ``TimeoutError`` is raised; nothing the job itself started is cleaned up. A worker that
        ends while running the job raises
        :class:`~tai42_kit.compute.errors.ComputeWorkerLostError`; a pool that is not running, or
        broke, raises :class:`~tai42_kit.compute.errors.ComputePoolUnavailableError`. An argument
        or a result that cannot be pickled raises the pickling error.
        """
        future = self._schedule(fn, args, timeout)
        try:
            outcome = await asyncio.wrap_future(future)
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                raise TimeoutError(
                    f"compute job {_job_name(fn)} exceeded its {timeout}s timeout; its worker was terminated"
                ) from exc
            if isinstance(exc, ProcessExpired):
                raise ComputeWorkerLostError(
                    f"the worker running compute job {_job_name(fn)} ended with exit code {exc.exitcode}"
                ) from exc
            if isinstance(exc, BrokenProcessPool):
                if future in self._in_flight_at_stop:
                    # The library's own shutdown closes the channel a job's result travels on, and a
                    # manager thread that reads it then reports the pool broken.
                    raise ComputePoolUnavailableError(_STOPPED_BEFORE_THE_JOB_FINISHED) from exc
                raise ComputePoolUnavailableError(f"the compute pool broke while running {_job_name(fn)}") from exc
            raise
        return outcome_value(outcome, _job_name(fn))

    def _schedule(
        self, fn: Callable[..., object], args: tuple[object, ...], timeout: float | None
    ) -> ProcessFuture[JobOutcome]:
        payload = pack(fn, args)
        with self._lock:
            pool = self._pool
            if pool is None:
                state = "was stopped" if self._stopped else "is not started"
                raise ComputePoolUnavailableError(f"the compute pool {state}; {_job_name(fn)} cannot run")
            try:
                future = pool.schedule(run_job, args=(payload,), timeout=timeout)
            except RuntimeError as exc:
                # The library refuses a job once its pool broke (a manager thread died).
                raise ComputePoolUnavailableError(f"the compute pool cannot run {_job_name(fn)}: {exc}") from exc
            self._in_flight.add(future)
        future.add_done_callback(self._settled)
        return future

    def _settled(self, future: Future[Any]) -> None:
        with self._lock:
            self._in_flight.discard(future)


def _terminate_on_sigterm() -> None:
    """Let ``SIGTERM`` end the worker process at once, whatever it runs.

    The library's own ``SIGTERM`` handler is a Python handler, which runs only between bytecodes:
    a job inside a C call would hold it off until the library's kill grace runs out. The cleanup
    the library's handler runs (``multiprocessing``'s exit function) concerns ``multiprocessing``
    children, which a worker has none of; a job's own resources are the job author's.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _job_name(fn: Callable[..., object]) -> str:
    return f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', repr(fn))}"
