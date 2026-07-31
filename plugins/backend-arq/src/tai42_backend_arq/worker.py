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

from tai42_backend_arq.pool import RedisPoolManager
from tai42_backend_arq.scheduler import recover_stalled_schedules, task_scheduler
from tai42_backend_arq.settings import arq_settings, job_deserializer, job_serializer
from tai42_backend_arq.tasks import callback_job, tool_execution

logger = logging.getLogger(__name__)


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
    redis_settings = arq_settings().make_redis_settings(redis_url)

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
