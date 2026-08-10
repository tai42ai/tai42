"""The arq worker runtime.

``start_arq_worker`` builds and runs an :class:`arq.Worker` registering the
three worker functions (``task_scheduler`` / ``callback_job`` /
``tool_execution``) with the ``recover_stalled_schedules`` startup watchdog. The
``main`` click command defines the CLI option surface; ``Backend.launch`` parses
its args through it.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click
from arq import Worker, func
from arq.constants import default_queue_name
from tai42_contract.app import tai42_app

from tai42_backend_arq.pool import RedisPoolManager
from tai42_backend_arq.scheduler import recover_stalled_schedules, task_scheduler
from tai42_backend_arq.settings import arq_settings, job_deserializer, job_serializer
from tai42_backend_arq.tasks import callback_job, tool_execution

logger = logging.getLogger(__name__)

# Upper bound on the wait for the app to become boot-ready before the worker
# consumes — a loud backstop for a boot that never completes. A worker that times
# out here refuses to run jobs against a half-built tool registry and fails loudly.
_APP_READY_TIMEOUT_SECONDS = 120


async def start_arq_worker(
    redis_url: str | None,
    burst: bool,
    keep_result: int,
    queue_name: str,
    max_jobs: int,
    job_timeout: int,
    poll_delay: float,
    max_tries: int,
    health_check_interval: int,
) -> None:
    settings = arq_settings()

    # The tool registry is (re)built by the boot self-resync running concurrently
    # on this loop; a worker consuming before it finished would run jobs against a
    # half-built registry and fail them permanently. Await the readiness latch
    # first; a timeout is a boot that never completed — fail loudly.
    try:
        await asyncio.wait_for(tai42_app.lifecycle.wait_until_ready(), timeout=_APP_READY_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise RuntimeError(
            f"arq worker: the app did not become ready within {_APP_READY_TIMEOUT_SECONDS}s "
            "(boot self-resync never completed); refusing to consume jobs against a half-built tool registry"
        ) from exc

    redis_settings = settings.make_redis_settings(redis_url)

    functions = [
        func(task_scheduler),
        func(callback_job),
        func(tool_execution),
    ]

    worker = Worker(
        job_serializer=job_serializer,
        job_deserializer=job_deserializer,
        functions=functions,
        queue_name=queue_name,
        redis_settings=redis_settings,
        burst=burst,
        keep_result=keep_result,
        max_jobs=max_jobs,
        job_timeout=job_timeout,
        poll_delay=poll_delay,
        max_tries=max_tries,
        health_check_interval=health_check_interval,
        # A graceful SIGTERM drains in-flight jobs for up to this many seconds
        # rather than cancelling them; arq cancels running jobs on signal when
        # this is 0 (its default), so it is required for warm-drain parity.
        job_completion_wait=settings.job_completion_wait,
        on_startup=recover_stalled_schedules,
        # Required for the scheduler and cancel/delete tools to abort jobs
        # through ``Job.abort``; without it abort requests are never processed.
        allow_abort_jobs=True,
    )

    try:
        await worker.async_run()
    except asyncio.CancelledError:
        logger.info("Worker cancelled, shutting down")
        raise
    finally:
        await RedisPoolManager.close()
        await worker.close()


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
def main(
    redis_url: str | None,
    burst: bool,
    keep_result: int,
    queue_name: str,
    max_jobs: int,
    job_timeout: int,
    poll_delay: float,
    max_tries: int,
    health_check_interval: int,
) -> None:
    try:
        asyncio.run(
            start_arq_worker(
                redis_url,
                burst,
                keep_result,
                queue_name,
                max_jobs,
                job_timeout,
                poll_delay,
                max_tries,
                health_check_interval,
            )
        )
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt")
        sys.exit(130)


if __name__ == "__main__":
    main()
