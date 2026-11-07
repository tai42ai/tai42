"""The Celery task surface: ``tool_execution`` and ``callback_task``.

Both are :class:`AsyncTask` subclasses, whose jobs run on long-lived event loops: each task's own
(:mod:`~tai42_backend_celery.core.task_loops`, closed by ``close_loop``), or on a green pool the
host's loop (:class:`~tai42_kit.backend.shared_loop.SharedLoop`), handed to the jobs on the app
the worker runs by the launch (``share_loop``) and let go by ``close_shared_loop``;
:func:`~tai42_backend_celery.core.worker_signals.shut_down_jobs` runs both. The kit's
loop-bound resources (pooled clients, checkpoint savers, stores) are so reused across invocations.
``tool_execution`` runs the target tool inside :func:`prevent_celery_stream_capture`. A
dispatched tool runs once; retries are declared per tool with a ``ToolRetryPolicy`` and applied
by the platform's dispatch seam, never by Celery.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
from collections.abc import Coroutine, Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from celery import Task
from tai42_kit.backend import CallbackSchema, SharedLoop, callback_execution
from tai42_kit.backend.job_context import running_job
from tai42_kit.backend.job_run import run_dispatched_tool

from tai42_backend_celery.core import job_slots
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.settings import celery_settings
from tai42_backend_celery.core.task_loops import PrivateLoops

if TYPE_CHECKING:
    from tai42_backend_celery.core.app import HostedCelery

# The names the task surface registers its two tasks under.
TOOL_EXECUTION_TASK = "celery.tool_execution"
CALLBACK_TASK = "celery.callback_task"


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


class AsyncTask(Task):
    """A task whose jobs run their async work on long-lived event loops.

    A pool that gives each running job an OS thread (``prefork``, ``-P solo``, ``-P threads``) runs
    each job on a loop of the task's own that no other job is running
    (:class:`~tai42_backend_celery.core.task_loops.PrivateLoops`). A green pool (``-P gevent``,
    ``-P eventlet``) runs every job of the worker process, whatever its task, on the one loop its OS
    thread runs, the host's (:class:`~tai42_kit.backend.shared_loop.SharedLoop`), which the
    launch hands over on the app with :meth:`share_loop` before the worker starts. A task reads it
    from its own ``app``, so a config reload that re-imports this module leaves it in reach.
    """

    # Guards the lazy creation of a task's private loops; the loops guard themselves after.
    _loops_creation: ClassVar[threading.Lock] = threading.Lock()
    _task_loops: PrivateLoops | None = None

    @staticmethod
    def share_loop(app: HostedCelery, loop: asyncio.AbstractEventLoop) -> None:
        """Hand every job of ``app``'s tasks on a green pool ``loop``, the host's running loop.

        A later hand replaces it.
        """
        app.shared_loop = SharedLoop(loop)

    @property
    def loops(self) -> PrivateLoops | SharedLoop:
        """The event loops this task's jobs run on, for how this process's pool runs its jobs.

        Raises ``RuntimeError`` on a green pool when no loop was handed (:meth:`share_loop`): its jobs
        can run on no loop of their own while the host's runs on the same OS thread.
        """
        if job_slots.green_pool(self.app):
            shared = self.app.shared_loop
            if shared is None:
                raise RuntimeError(
                    "a green pool's jobs run on the host's event loop, and none was handed to them; the "
                    "launch hands it with AsyncTask.share_loop before the worker starts"
                )
            return shared
        with AsyncTask._loops_creation:
            if self._task_loops is None:
                self._task_loops = PrivateLoops()
            return self._task_loops

    def run_async(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        """Run ``coroutine`` to completion on the task's :attr:`loops`, as one job of the worker, and return its result.

        A job that runs in the worker process holds the fork gate for its whole run and is held to
        the job budget (:mod:`~tai42_backend_celery.core.job_slots`). The run is marked as a backend job
        (:func:`~tai42_kit.backend.job_context.running_job`), so a tool it runs that would wait for
        another job's result refuses.

        A job refused before ``coroutine`` started (no loop to run it on, a shared loop let go)
        closes ``coroutine``, as the loops close the job they refuse.
        """
        try:
            with job_slots.in_process_job(self.app), running_job():
                return self.loops.run(job_slots.in_job(self.app, coroutine, label=f"{self.name} {self.request.id}"))
        finally:
            # The loops close the job's wrapper; a wrapper closed before it started leaves its coroutine unclosed.
            if inspect.getcoroutinestate(coroutine) == inspect.CORO_CREATED:
                coroutine.close()

    def close_loop(self) -> None:
        """Close the task's own loops, releasing the kit's loop-bound resources on each one first.

        The kit keeps its pooled clients, checkpoint savers and stores per loop, so they are released
        on it before it goes away. Run as
        the process that ran the jobs stops (:func:`~tai42_backend_celery.core.worker_signals.shut_down_jobs`);
        the next job makes the task's loops anew. The shared loop is the host's, let go by
        :meth:`close_shared_loop`.
        """
        with AsyncTask._loops_creation:
            loops, self._task_loops = self._task_loops, None
        if loops is not None:
            loops.close()

    @staticmethod
    def close_shared_loop(app: HostedCelery) -> None:
        """Let the host's loop handed to ``app``'s jobs go, as the worker stops: a later green job is refused.

        Run once the pool has ended its jobs (:func:`~tai42_backend_celery.core.worker_signals.shut_down_jobs`).
        The loop and the kit resources bound to it are the host's, which its stop releases.
        """
        shared = app.shared_loop
        if shared is not None:
            shared.close()


async def run_tool(**kwargs: Any) -> Any:
    """Run the tool the job names (under the ``tool_name_arg`` kwarg) with the rest, as a dispatched job."""
    return await run_dispatched_tool(kwargs, settings=celery_settings())


@celery_app.task(name=CALLBACK_TASK, base=AsyncTask, bind=True)
def callback_task(self: AsyncTask, result: Any, callback: CallbackSchema) -> Any:
    """Run a :class:`CallbackSchema` over the parent task's result.

    Linked to ``tool_execution`` via ``apply_async(link=...)``: Celery runs it only after the
    parent succeeded, and ``result`` is the parent's return value, handed to the jq inputs as-is.
    """
    return self.run_async(callback_execution(result, callback, settings=celery_settings()))


@celery_app.task(name=TOOL_EXECUTION_TASK, base=AsyncTask, bind=True)
def tool_execution(self: AsyncTask, *args: Any, **kwargs: Any) -> Any:
    """Execute one app tool on the worker (the body of every dispatched task)."""
    with prevent_celery_stream_capture():
        return self.run_async(run_tool(**kwargs))
