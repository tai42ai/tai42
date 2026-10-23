"""The rq beat: rq-scheduler's ``Scheduler`` on a connection built by the kit seam.

:func:`run_beat` takes the rq-scheduler options that are not about the connection, with
rq-scheduler's own names, defaults and meanings, and the URL override ``-u/--url``. The
connection is the one the rq worker uses (:func:`~tai42_backend_rq.worker.rq_redis_connection`),
so the URL resolves from ``RQ_REDIS_URL`` with the ``TAI_DEFAULT_REDIS_URL`` fallback, and
the ``RQ_*`` connection retry and socket timeouts reach every scheduler command. The
scheduler's loop has no reconnect of its own: a Redis error the retry does not ride out
ends the beat. Every moved job lands on the plugin's queue class, which bounds it by the
dispatch job budget.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

import click
from rq_scheduler.scheduler import Scheduler
from rq_scheduler.utils import setup_loghandlers

from tai42_backend_rq.queue import TASK_BUDGET_QUEUE_PATH
from tai42_backend_rq.worker import rq_redis_connection


@click.command("rq-scheduler")
@click.option("-b", "--burst", is_flag=True, default=False, help="Run in burst mode (quit after all work is done)")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Show more output")
@click.option("--quiet", is_flag=True, default=False, help="Show less output")
@click.option("-u", "--url", default=None, help="Redis URL (default: the RQ_REDIS_URL setting)")
@click.option(
    "-i",
    "--interval",
    type=float,
    default=60.0,
    help="How often the scheduler checks for new jobs to add to the queue (in seconds, can be floating-point)",
)
@click.option("--path", default=".", help="Specify the import path.")
@click.option("--pid", default=None, metavar="FILE", help="A filename to use for the PID file.")
@click.option("-j", "--job-class", default=None, help="Custom RQ Job class")
def beat_command(
    burst: bool,
    verbose: bool,
    quiet: bool,
    url: str | None,
    interval: float,
    path: str,
    pid: str | None,
    job_class: str | None,
) -> None:
    """Run the rq scheduler: move due scheduled jobs onto their queue."""
    if path:
        sys.path = path.split(":") + sys.path
    if pid:
        with open(pid, "w") as pid_file:
            pid_file.write(str(os.getpid()))

    connection = rq_redis_connection(url)
    try:
        setup_loghandlers("DEBUG" if verbose else "WARNING" if quiet else "INFO")
        scheduler = Scheduler(
            connection=connection,
            # rq-scheduler is untyped, so its ``interval=60`` default reads as ``int``; its own
            # CLI passes the float this option parses.
            interval=interval,  # pyright: ignore[reportArgumentType]
            job_class=job_class,
            queue_class=TASK_BUDGET_QUEUE_PATH,
        )
        scheduler.run(burst=burst)
    finally:
        connection.close()


def run_beat(args: Sequence[str]) -> None:
    """Parse ``args`` strictly through the beat's options and run the scheduler in the foreground.

    An unknown option raises :class:`click.UsageError` before anything connects.
    """
    context = beat_command.make_context("rq-scheduler", list(args))
    with context:
        beat_command.invoke(context)
