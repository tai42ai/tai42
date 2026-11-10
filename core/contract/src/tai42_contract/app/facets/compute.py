"""``AppCompute`` — CPU-bound work in worker processes (``app.compute``)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class AppCompute(Protocol):
    """CPU-bound work off the event loop (``app.compute``).

    A job runs in a worker process of the platform process's compute pool, so a job that holds
    the interpreter lock for seconds leaves the caller's event loop serving every other request.
    A job is a picklable callable (a module-level function) with picklable arguments, and it
    returns a picklable result. A worker imports the job's module afresh in a process with no app
    bound, so that module imports without reaching ``tai42_app`` (no tool registration beside the
    job, no module-level settings read); a job whose module does reach it fails in the worker, and
    that import error is raised in the caller like any exception the job raises.
    """

    async def run[T, *Ts](self, fn: Callable[[*Ts], T], /, *args: *Ts, timeout: float | None = None) -> T:
        """Run ``fn(*args)`` in a worker process and return its result.

        An exception ``fn`` raises is raised here as itself, with the worker's traceback as its
        cause; an exception that cannot be rebuilt in the caller raises ``RuntimeError`` carrying
        its type and message. ``timeout`` (seconds, ``None`` for none) bounds the job once a worker
        takes it up: past it the worker is terminated at once and ``TimeoutError`` is raised, and
        nothing the job itself started is cleaned up; cancelling the awaiting task terminates the
        worker the same way. A pool that cannot run the job (not started, stopped, broken) and a
        worker that ends mid-job raise ``RuntimeError`` that classifies as an unavailable
        platform transport (``ErrorKind.UNAVAILABLE``,
        :func:`~tai42_contract.errors.is_transport_error` true); an argument or a result that
        cannot be pickled raises the pickling error. A job never runs on the caller's loop
        instead.
        """
        ...
