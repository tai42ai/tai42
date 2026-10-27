"""The Celery task surface: ``tool_execution`` and ``callback_task``.

Both are :class:`AsyncTask` subclasses, each owning its long-lived private event
loops, one per job running at once, so pooled per-loop clients are reused across
invocations (closed by ``close_loop`` at worker-child shutdown). ``tool_execution`` runs the target tool
inside :func:`prevent_celery_stream_capture`. A dispatched tool runs once; retries
are declared per tool with a ``ToolRetryPolicy`` and applied by the platform's
dispatch seam, never by Celery.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Coroutine, Generator, Iterator
from contextlib import contextmanager
from typing import Any

from celery import Task
from tai42_contract.app import tai42_app
from tai42_contract.backend import SCHEDULE_CADENCE_KEY, SCHEDULE_NAME_KEY
from tai42_kit.backend import CallbackSchema, backend_fire, callback_execution
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG, bind_worker_secret_capability

from tai42_backend_celery.core import job_slots
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.settings import celery_settings

# Task options every dispatching extension exposes; ``callback_kwargs`` chains a
# follow-up tool via a Celery ``link`` signature.
CELERY_TASK_OPTS: dict[str, Any] = {
    "queue": str | None,
    "countdown": int | None,
    "priority": int | None,
    "retry": bool | None,
    "routing_key": str | None,
    "expires": str | float | None,
    "eta": str | None,
    "callback_kwargs": CallbackSchema | None,
}

CELERY_SCHEDULE_OPTS: dict[str, Any] = {
    SCHEDULE_NAME_KEY: str,
    SCHEDULE_CADENCE_KEY: dict[str, Any],
}


@contextmanager
def prevent_celery_stream_capture() -> Generator[None]:
    """Temporarily restore ``sys.stdout``/``sys.stderr`` to the real OS file descriptors.

    Bypasses Celery's ``LoggingProxy`` so subprocesses spawned by the tool (e.g. stdio MCP
    servers) inherit valid file descriptors.
    """
    captured_stdout = sys.stdout
    captured_stderr = sys.stderr
    try:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        yield
    finally:
        sys.stdout = captured_stdout
        sys.stderr = captured_stderr


class _TaskLoops:
    """The private event loops of one task: each runs one job at a time, and a free one serves the next job."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._free: list[asyncio.AbstractEventLoop] = []
        self._open: list[asyncio.AbstractEventLoop] = []

    @contextmanager
    def take(self) -> Iterator[asyncio.AbstractEventLoop]:
        """Hold a loop no other job is running, made when no open one is free, as this thread's loop."""
        with self._lock:
            loop: asyncio.AbstractEventLoop | None = None
            while loop is None and self._free:
                loop = self._free.pop()
                if loop.is_closed():
                    self._open.remove(loop)
                    loop = None
            if loop is None:
                loop = asyncio.new_event_loop()
                self._open.append(loop)
        asyncio.set_event_loop(loop)
        try:
            yield loop
        finally:
            with self._lock:
                if loop in self._open:
                    self._free.append(loop)

    @property
    def count(self) -> int:
        """How many loops are open."""
        with self._lock:
            return len(self._open)

    def close(self) -> None:
        """Close every loop, shutting down its per-loop pooled clients first.

        Every loop is closed even when another's shutdown fails; the failures are then raised together.
        """
        with self._lock:
            loops, self._open, self._free = self._open, [], []
        failures: list[Exception] = []
        for loop in loops:
            try:
                try:
                    loop.run_until_complete(tai42_app.clients.shutdown_clients())
                finally:
                    loop.close()
            except Exception as failure:
                failures.append(failure)
        if failures:
            raise ExceptionGroup("closing the task's event loops failed", failures)


class AsyncTask(Task):
    """A task whose jobs run their async work on the task's private event loops.

    A pool that runs several jobs at once (``-P threads``, ``-P gevent``) runs each on a loop no
    other job is running; a loop a job has left serves the next, so the loops number at most the
    jobs that ran at once.
    """

    # Guards the lazy creation of each task's loops; the loops guard themselves after.
    _loops_creation: threading.Lock = threading.Lock()
    _task_loops: _TaskLoops | None = None

    @property
    def loops(self) -> _TaskLoops:
        """This task's private event loops."""
        with AsyncTask._loops_creation:
            if self._task_loops is None:
                self._task_loops = _TaskLoops()
            return self._task_loops

    def run_async(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coroutine`` to completion on a loop of the task's own, as one job of the worker, and return its result.

        On a pool that cannot end a job at its budget the job counts against the worker's slots
        (:mod:`~tai42_backend_celery.core.job_slots`).
        """
        with self.loops.take() as loop:
            return loop.run_until_complete(job_slots.in_job(coroutine, label=f"{self.name} {self.request.id}"))

    def close_loop(self) -> None:
        """Close the task's loops, shutting down each one's per-loop pooled clients first.

        Client pools are keyed per loop, so they must be closed on it before it goes away. Run at
        worker-child shutdown.
        """
        self.loops.close()


async def run_tool(**kwargs: Any) -> Any:
    """Pop the dispatch-injected tool name and run that tool with the rest."""
    tool_name = kwargs.pop(celery_settings().tool_name_arg)
    # A worker executes a dequeued task with no live caller holding a
    # connection, so the turn budget does not apply, and no HTTP request bound
    # the secret-read capability — the worker binds the one the job carried (a job
    # without it raises ``KeyError``).
    secret_capability = kwargs.pop(WORKER_SECRET_CAPABILITY_ARG)
    # ``backend_fire`` pops the reserved ``backend_schedule_*`` job kwargs and either binds the stamped
    # firing identity and drives the schedule door (a contract-bearing schedule, or a task that
    # forwarded the ambient subject/identity), or runs the tool plainly when the job carries no door
    # signal. A park comes back as the re-park sentinel it returns.
    detached_token = mark_detached_run()
    try:
        with bind_worker_secret_capability(secret_capability):
            return await backend_fire(tool_name, kwargs)
    finally:
        reset_detached_run(detached_token)


@celery_app.task(name="celery.callback_task", base=AsyncTask, bind=True)
def callback_task(self: AsyncTask, result: Any, callback: CallbackSchema) -> Any:
    """Run a :class:`CallbackSchema` over the parent task's result.

    Linked to ``tool_execution`` via ``apply_async(link=...)``, so ``result`` is the parent's
    return value, handed to the jq inputs as-is.
    """
    return self.run_async(callback_execution(result, callback))


@celery_app.task(name="celery.tool_execution", base=AsyncTask, bind=True)
def tool_execution(self: AsyncTask, *args: Any, **kwargs: Any) -> Any:
    """Execute one app tool on the worker (the body of every dispatched task)."""
    with prevent_celery_stream_capture():
        return self.run_async(run_tool(**kwargs))
