"""The arq worker runtime — this backend's whole vendor binding.

:class:`ArqWorkerRuntime` answers only "how arq starts, drains, and recycles":
it builds an :class:`arq.Worker` over the three worker functions
(``task_scheduler`` / ``callback_job`` / ``tool_execution``) with the
``recover_stalled_schedules`` startup watchdog, runs it on the serving loop, and asks
it to drain or to stop cold. The two that run manifest code (``callback_job`` runs the
callback, ``tool_execution`` the tool) hold the process's fork-gate job span for their
whole run; ``task_scheduler`` runs platform code only and waits for the target it
enqueued, so it holds none: a span there would wait on a job that waits behind the
rebuild. Each
pass of arq's poll loop reports the worker's progress.

The worker forks no child of its own, but code it runs may fork (a tool that starts a
``multiprocessing`` child, a ``subprocess`` with a ``preexec_fn``). The build installs the kit's
fork safety (:mod:`tai42_kit.backend.fork_safety`) over every fork of the process that runs the
interpreter's fork hooks: each child resets its inherited copy of the fork gate and evicts the
monitoring writer it inherited. A fork from a job's own context nests in that job's span; a
fork on the loop's thread enters its span at once, as the kit's guard never blocks a loop; a
fork on another thread outside any job span waits out a running re-import and reports no
progress meanwhile: arq's poll loop runs on the event loop, which that wait does not hold up,
so the loop's own passes are the only progress the worker reports.

Readiness gating, signal wiring and drain-on-cancellation belong to
:class:`~tai42_kit.backend.ManagedBackend`, which drives this object.

A job runs at most once. arq picks a job up again after its run was cut (a worker died
inside it, or a cold stop cancelled it); that pickup ends the job failed with
:class:`JobRunCutError`, without entering its body. The schedule driver's later pickups
only move its schedule on.

The :func:`worker_options` click command is the worker's option surface:
:meth:`ArqWorkerRuntime.from_args` parses ``launch``'s subcommand args through it and
never runs it. A worker is started with ``tai backend worker``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Self

import click
from arq import Worker, func
from arq.worker import Function
from tai42_contract.backend.runtime import (
    CONSUMING_RUNTIME,
    BackendRuntime,
    ExecutionMode,
    ProgressLiveness,
    ProgressReporter,
)
from tai42_kit.backend import fork_safety
from tai42_kit.backend.job_context import running_job
from tai42_kit.backend.queue_labels import derived_queue, queue_label_option
from tai42_kit.clients.impl.redis import async_redis_from_url, redis_from_url_kwargs
from tai42_kit.fork_gate import JOB_SPAN_WAIT_SECONDS, fork_gate

from tai42_backend_arq.pool import arq_redis_over, enqueue_client_kwargs
from tai42_backend_arq.scheduler import recover_stalled_schedules, task_scheduler
from tai42_backend_arq.settings import arq_settings, job_deserializer, job_serializer
from tai42_backend_arq.tasks import callback_job, tool_execution

logger = logging.getLogger(__name__)

# The promise of a poll loop that sleeps nothing between passes (a non-positive
# ``--poll-delay``): arq's own floor for the poll delay, the one ``heart_beat``
# clamps its cron window to.
_SPIN_PROMISE_SECONDS = 0.5


class JobRunCutError(RuntimeError):
    """The failure of a job picked up again after its run was cut, which is not run again.

    arq takes a job up again after a worker died inside it (once its in-progress key
    expires) or after a cold stop cancelled it. A job runs at most once, so that pickup
    ends the job with this error, stored as its failed result for the caller to read.
    """

    def __init__(self) -> None:
        """The one message every cut run carries."""
        super().__init__("the run was cut (its worker died or was stopped mid-run) and is not run again")


def _run_once(job_function: Function) -> Function:
    """``job_function`` refusing every pickup of a job but the first with :class:`JobRunCutError`.

    arq counts each pickup of a job as a try (``ctx["job_try"]``) and refuses one past
    ``max_tries`` with its own ``JobExecutionFailed``; this function puts no bound there and
    refuses the later pickup itself, before the body or its job span is entered, so the
    failure the job stores names what happened to it.
    """
    body = job_function.coroutine

    @functools.wraps(body)
    async def job(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        if ctx["job_try"] > 1:
            raise JobRunCutError
        return await body(ctx, *args, **kwargs)

    return dataclasses.replace(job_function, coroutine=job, max_tries=sys.maxsize)


def _in_job_span(function: Callable[..., Awaitable[Any]]) -> Function:
    """``function`` as an arq job function that runs inside the fork gate's job span, marked as a job.

    A job runs as a coroutine on the loop a config reload rebuilds on, and a sync step it
    hands to ``asyncio.to_thread`` goes on running while that loop re-imports the manifest
    modules. The span holds the job out of the re-import for its whole run: a rebuild waits
    for the running jobs, and a job that arrives while one is pending waits for it, off the
    loop and up to ``JOB_SPAN_WAIT_SECONDS``, inside arq's job timeout. Only the job
    functions that run manifest code take it (see the module docstring).

    The body runs marked as a backend job (:func:`~tai42_kit.backend.job_context.running_job`),
    so a tool it runs that would wait for another job's result refuses. The body runs its tool
    inside :func:`~tai42_kit.backend.job_run.dispatched_job`, which admits that run on the
    serving generation, so a reload's retire drains it before it closes that generation's
    loop-bound resources. Only that run is admitted: the rest of the job runs unadmitted, and a
    callback with no tool runs no ``dispatched_job`` at all, so a retire drains none of it.
    """

    @functools.wraps(function)
    async def job(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        async with fork_gate.job_span_async(timeout=JOB_SPAN_WAIT_SECONDS):
            with running_job():
                return await function(ctx, *args, **kwargs)

    return func(job)


class _ProgressReportingWorker(Worker):
    """arq's worker, reporting each pass of its poll loop as this process's progress.

    arq's ``main`` loops ``async for _ in poll(poll_delay_s)`` and ends every pass with
    ``heart_beat``, sleeping at most ``poll_delay_s`` before the next one; jobs run as
    tasks on the same loop, so the pass keeps coming while they run. A job that blocks
    the loop stops every pass, which is the stall the report exists to show.
    """

    def __init__(self, *args: Any, report_progress: ProgressReporter, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._report_progress = report_progress

    async def heart_beat(self) -> None:
        """Report this pass, then run arq's own health record and cron."""
        # Before arq's own: a Redis-blocked health record must not hide a pass that happened.
        self._report_progress(next_within=self.poll_delay_s if self.poll_delay_s > 0 else _SPIN_PROMISE_SECONDS)
        await super().heart_beat()


class ArqWorkerRuntime(BackendRuntime):
    """The arq worker: an on-loop runtime that executes enqueued work.

    ``pool_turnover_required`` stays False — arq runs every job as a coroutine in
    THIS process, so a registry mutation reaches the running jobs directly and
    there is no worker pool holding a stale snapshot to replace.
    """

    name = CONSUMING_RUNTIME
    mode = ExecutionMode.on_loop
    consumes_work = True
    liveness = ProgressLiveness()

    def __init__(self, options: Mapping[str, Any]) -> None:
        """``options`` is a parsed :func:`worker_options` option set."""
        self._options = dict(options)
        self._worker: _ProgressReportingWorker | None = None
        # arq schedules a fresh wait-then-cancel task on every warm-shutdown
        # call, so the second one would open a second completion window and cut
        # the first short. The host is entitled to request a drain twice (on the
        # signal and again on the cancellation path); this is what makes that
        # idempotent.
        self._draining = False

    # -- construction --------------------------------------------------------

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        """Build a runtime from raw CLI ``args``, parsed strictly through the worker's option surface."""
        # Parse strictly through the CLI's own option surface: an unknown or
        # malformed option aborts the launch loudly rather than starting a worker
        # configured differently than the operator asked for.
        return cls(worker_options.make_context("arq-worker", list(args)).params)

    async def build(self) -> None:
        """Construct the arq ``Worker`` from settings and the parsed options, and guard the process's forks.

        The kit's fork safety is installed last (see the module docstring): a fork that waits on a
        re-import waits for up to ``JOB_SPAN_WAIT_SECONDS``.
        """
        settings = arq_settings()
        options = self._options
        queue_name = derived_queue(settings.queue_name, options["queue"])
        self._worker = _ProgressReportingWorker(
            report_progress=self.report_progress,
            job_serializer=job_serializer,
            job_deserializer=job_deserializer,
            functions=[
                # The schedule driver alone takes later pickups of its job: each one only moves
                # its schedule on and never fires the target again (see ``task_scheduler``), so
                # arq puts no bound on them.
                func(task_scheduler, max_tries=sys.maxsize),
                _run_once(_in_job_span(callback_job)),
                _run_once(_in_job_span(tool_execution)),
            ],
            # No ``--queue`` label consumes the settings queue (``ARQ_QUEUE_NAME``); a
            # label consumes ``<ARQ_QUEUE_NAME>.<label>``, the queue tasks sent with it land on.
            queue_name=queue_name,
            # A ready pool from the kit seam, so the connect and every command carry the
            # kit's logged connection retry (``ARQ_RETRY_ATTEMPTS``) and the socket timeouts;
            # arq runs no connect loop of its own over it. The pool is the worker's own and
            # ``Worker.close`` closes it.
            redis_pool=arq_redis_over(
                async_redis_from_url(
                    **redis_from_url_kwargs(
                        {**enqueue_client_kwargs(), "url": options["redis_url"] or settings.redis.redis_url}
                    )
                ),
                queue_name=queue_name,
            ),
            burst=options["burst"],
            keep_result=options["keep_result"],
            max_jobs=options["max_jobs"],
            # An unset ``--job-timeout`` falls to the dispatch job budget (``ARQ_TASK_TIMEOUT``),
            # the one limit every backend reads; an explicit flag still wins.
            job_timeout=options["job_timeout"] or settings.task_timeout,
            poll_delay=options["poll_delay"],
            # The at-most-once floor for a job function that declares no tries of its own:
            # arq refuses its later pickups with its own ``JobExecutionFailed``. Each function
            # above declares its own (``_run_once`` and the schedule driver).
            max_tries=1,
            health_check_interval=options["health_check_interval"],
            # A graceful shutdown drains in-flight jobs for up to this many
            # seconds rather than cancelling them; arq cancels running jobs
            # immediately when this is 0 (its default), so it is required for
            # warm-drain parity with the other backends.
            job_completion_wait=settings.job_completion_wait,
            on_startup=recover_stalled_schedules,
            # Required for the scheduler and cancel/delete tools to abort jobs
            # through ``Job.abort``; without it abort requests are never processed.
            allow_abort_jobs=True,
            # The host owns the process's signal disposition; arq's own install would be
            # last-writer-wins over it.
            handle_signals=False,
        )
        fork_safety.guard_forks(fork_gate, timeout=JOB_SPAN_WAIT_SECONDS)

    @property
    def worker(self) -> _ProgressReportingWorker:
        """The built engine.

        Reading it before :meth:`build` is a bug in the driver, not a state to tolerate quietly.
        """
        if self._worker is None:
            raise RuntimeError("arq worker: build() has not run, so there is no worker to drive")
        return self._worker

    # -- running -------------------------------------------------------------

    async def run_on_loop(self) -> None:
        """Run the worker on the current loop until it drains or is cancelled."""
        try:
            await self.worker.async_run()
        except asyncio.CancelledError:
            # Both endings arrive here: an outer cancellation, and a COMPLETED
            # drain — arq finishes its warm shutdown by cancelling the worker's
            # own main task, so the run body leaves cancelled even when every job
            # finished. Which one it was is told by the drain request that
            # preceded it, so this stays one line at INFO.
            logger.info("Worker cancelled, shutting down")
            raise

    # -- stopping ------------------------------------------------------------

    def request_drain(self) -> None:
        """Request a warm drain: stop picking jobs and let in-flight ones finish (idempotent)."""
        if self._draining:
            return
        self._draining = True
        # arq's warm handler: stop picking jobs, let the in-flight ones finish
        # inside ``job_completion_wait``, then end the run. Called directly
        # rather than through a signal, which is the host's to own.
        self.worker.handle_sig_wait_for_completion(signal.SIGTERM)

    def request_terminate(self) -> None:
        """Request a cold stop: cancel every running job now and end the run."""
        # arq's cold handler: cancel every running job now and end the run.
        self.worker.handle_sig(signal.SIGTERM)

    # -- teardown ------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the worker on every exit path, including one where ``build`` itself failed."""
        if self._worker is not None:
            await self._worker.close()


@click.command("arq-worker")
@click.option("--redis-url", default=None, help="Redis URL (defaults to ARQ_REDIS_URL)")
@click.option("--burst", is_flag=True, help="Run in burst mode")
@click.option("--keep-result", type=int, default=3600, help="Keep result seconds")
@click.option(
    "--queue",
    default=None,
    callback=queue_label_option,
    help="Queue label: consume <ARQ_QUEUE_NAME>.<label> instead of ARQ_QUEUE_NAME",
)
@click.option("--max-jobs", type=int, default=10, help="Max concurrent jobs")
@click.option("--job-timeout", type=int, default=None, help="Job timeout seconds (defaults to ARQ_TASK_TIMEOUT)")
@click.option("--poll-delay", type=float, default=0.5, help="Poll delay seconds")
@click.option("--health-check-interval", type=int, default=60, help="Health check interval seconds")
def worker_options(**options: Any) -> None:
    """The arq worker's option surface.

    :meth:`ArqWorkerRuntime.from_args` parses through it with ``make_context`` and never
    invokes it; ``tai backend worker`` runs the worker.
    """
