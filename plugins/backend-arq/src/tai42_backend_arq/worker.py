"""The arq worker runtime — this backend's whole vendor binding.

:class:`ArqWorkerRuntime` answers only "how arq starts, drains, and recycles":
it builds an :class:`arq.Worker` over the three worker functions
(``task_scheduler`` / ``callback_job`` / ``tool_execution``) with the
``recover_stalled_schedules`` startup watchdog, runs it on the serving loop, and
asks it to drain or to stop cold. Readiness gating, signal wiring and
drain-on-cancellation belong to
:class:`~tai42_kit.backend.ManagedBackend`, which drives this object.

The ``main`` click command defines the CLI option surface twice over: it is what
``launch`` parses its subcommand args through, and it is the direct entry point
for running a worker outside a host process.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Self

import click
from arq import Worker, func
from arq.constants import default_queue_name
from tai42_contract.backend.runtime import CONSUMING_RUNTIME, BackendRuntime, ExecutionMode

from tai42_backend_arq.pool import RedisPoolManager
from tai42_backend_arq.scheduler import recover_stalled_schedules, task_scheduler
from tai42_backend_arq.settings import arq_settings, job_deserializer, job_serializer
from tai42_backend_arq.tasks import callback_job, tool_execution

logger = logging.getLogger(__name__)


class ArqWorkerRuntime(BackendRuntime):
    """The arq worker: an on-loop runtime that executes enqueued work.

    ``pool_turnover_required`` stays False — arq runs every job as a coroutine in
    THIS process, so a registry mutation reaches the running jobs directly and
    there is no worker pool holding a stale snapshot to replace.
    """

    name = CONSUMING_RUNTIME
    mode = ExecutionMode.on_loop
    consumes_work = True

    def __init__(self, options: Mapping[str, Any], *, vendor_signals: bool = False) -> None:
        """``options`` is a parsed :func:`main` option set.

        ``vendor_signals`` lets arq install its OWN signal handlers, and is for
        the direct-CLI entry point only: there, no host chain exists to compose
        with and arq's handler is the only thing that would drain a SIGTERM. On
        the ``launch`` path it stays False — the host owns the process's signal
        disposition, and arq's install is last-writer-wins over it.
        """
        self._options = dict(options)
        self._vendor_signals = vendor_signals
        self._worker: Worker | None = None
        # arq schedules a fresh wait-then-cancel task on every warm-shutdown
        # call, so the second one would open a second completion window and cut
        # the first short. The host is entitled to request a drain twice (on the
        # signal and again on the cancellation path); this is what makes that
        # idempotent.
        self._draining = False

    # -- construction --------------------------------------------------------

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        # Parse strictly through the CLI's own option surface: an unknown or
        # malformed option aborts the launch loudly rather than starting a worker
        # configured differently than the operator asked for.
        return cls(main.make_context("arq-worker", list(args)).params)

    async def build(self) -> None:
        settings = arq_settings()
        options = self._options
        self._worker = Worker(
            job_serializer=job_serializer,
            job_deserializer=job_deserializer,
            functions=[func(task_scheduler), func(callback_job), func(tool_execution)],
            queue_name=options["queue_name"],
            redis_settings=settings.make_redis_settings(options["redis_url"]),
            burst=options["burst"],
            keep_result=options["keep_result"],
            max_jobs=options["max_jobs"],
            job_timeout=options["job_timeout"],
            poll_delay=options["poll_delay"],
            max_tries=options["max_tries"],
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
            handle_signals=self._vendor_signals,
        )

    @property
    def worker(self) -> Worker:
        """The built engine. Reading it before :meth:`build` is a bug in the
        driver, not a state to tolerate quietly."""
        if self._worker is None:
            raise RuntimeError("arq worker: build() has not run, so there is no worker to drive")
        return self._worker

    # -- running -------------------------------------------------------------

    async def run_on_loop(self) -> None:
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
        if self._draining:
            return
        self._draining = True
        # arq's warm handler: stop picking jobs, let the in-flight ones finish
        # inside ``job_completion_wait``, then end the run. Called directly
        # rather than through a signal, which is the host's to own.
        self.worker.handle_sig_wait_for_completion(signal.SIGTERM)

    def request_terminate(self) -> None:
        # arq's cold handler: cancel every running job now and end the run.
        self.worker.handle_sig(signal.SIGTERM)

    # -- teardown ------------------------------------------------------------

    async def aclose(self) -> None:
        # Runs on every exit path, including one where ``build`` itself failed —
        # the shared pool may exist even when the worker never did.
        await RedisPoolManager.close()
        if self._worker is not None:
            await self._worker.close()


async def _run_standalone(options: Mapping[str, Any]) -> None:
    """Run one worker outside a host process, for :func:`main`.

    No readiness gate and no composed signal chain: this process has no app
    booting alongside the worker to wait for, and nothing else on its loop to
    compose signals with, so arq keeps its own handlers.
    """
    runtime = ArqWorkerRuntime(options, vendor_signals=True)
    await runtime.build()
    try:
        await runtime.run_on_loop()
    finally:
        await runtime.aclose()


@click.command("tai42-backend-arq")
@click.option("--redis-url", default=None, help="Redis URL (defaults to ARQ_REDIS_URL)")
@click.option("--burst", is_flag=True, help="Run in burst mode")
@click.option("--keep-result", type=int, default=3600, help="Keep result seconds")
@click.option("--queue-name", default=default_queue_name, help="Queue name")
@click.option("--max-jobs", type=int, default=10, help="Max concurrent jobs")
@click.option("--job-timeout", type=int, default=300, help="Job timeout seconds")
@click.option("--poll-delay", type=float, default=0.5, help="Poll delay seconds")
@click.option("--max-tries", type=int, default=5, help="Max tries")
@click.option("--health-check-interval", type=int, default=60, help="Health check interval seconds")
def main(**options: Any) -> None:
    try:
        asyncio.run(_run_standalone(options))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt")
        sys.exit(130)


if __name__ == "__main__":
    main()
