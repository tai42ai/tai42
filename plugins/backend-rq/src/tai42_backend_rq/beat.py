"""The rq beat: rq-scheduler's ``Scheduler`` on a connection built by the kit seam.

:func:`run_beat` takes the rq-scheduler options that are not about the connection, with
rq-scheduler's own names, defaults and meanings, and the URL override ``-u/--url``. The
connection is the one the rq worker uses (:func:`~tai42_backend_rq.worker.rq_redis_connection`),
so the URL resolves from ``RQ_REDIS_URL`` with the ``TAI_DEFAULT_REDIS_URL`` fallback, and
the ``RQ_*`` connection retry and socket timeouts reach every scheduler command. The
scheduler's loop has no reconnect of its own: a Redis error the retry does not ride out
ends the beat. Every moved job lands on the plugin's queue class, which bounds it by the
dispatch job budget. Each pass of the loop reports through the progress reporter
:func:`run_beat` is handed (:class:`ProgressReportingScheduler`).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import click
from rq_scheduler.scheduler import Scheduler
from rq_scheduler.utils import setup_loghandlers
from tai42_contract.backend.runtime import ProgressReporter

from tai42_backend_rq.queue import TASK_BUDGET_QUEUE_PATH
from tai42_backend_rq.worker import rq_redis_connection

# The promise of a scheduler whose interval is not positive: its loop passes without sleeping,
# so its next pass is due as soon as the current one's Redis commands return.
_SPIN_PROMISE_SECONDS = 1.0


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
def beat_command(**options: Any) -> None:
    """Run the rq scheduler: move due scheduled jobs onto their queue.

    The command is the beat's option surface: :func:`run_beat` parses through it and runs the
    scheduler with the reporter it is handed.
    """


class ProgressReportingScheduler(Scheduler):
    """rq-scheduler's scheduler, reporting each pass of its run loop as this process's progress.

    rq-scheduler's ``run`` calls ``heartbeat`` at the top of every pass, then sleeps at most its
    ``interval`` before the next one, so each heartbeat promises the next pass within that
    interval. A non-positive interval makes the loop pass without sleeping; the promise is then
    :data:`_SPIN_PROMISE_SECONDS`.
    """

    def __init__(self, *args: Any, report_progress: ProgressReporter, interval: float, **kwargs: Any) -> None:
        """Build rq-scheduler's scheduler, reporting each pass through ``report_progress``."""
        # rq-scheduler is untyped, so its ``interval=60`` default reads as ``int``; its own CLI
        # passes the float the ``--interval`` option parses.
        super().__init__(*args, interval=interval, **kwargs)  # pyright: ignore[reportArgumentType]
        self._report_progress = report_progress
        self._next_pass_within = interval if interval > 0 else _SPIN_PROMISE_SECONDS

    def heartbeat(self) -> None:
        """Report this pass, then refresh rq-scheduler's own registration key."""
        self._report_progress(next_within=self._next_pass_within)
        super().heartbeat()


def _run_scheduler(options: Mapping[str, Any], report_progress: ProgressReporter) -> None:
    """Run the scheduler in the foreground with the parsed ``options``, reporting each pass."""
    path: str = options["path"]
    pid: str | None = options["pid"]
    if path:
        sys.path = path.split(":") + sys.path
    if pid:
        with open(pid, "w") as pid_file:
            pid_file.write(str(os.getpid()))

    connection = rq_redis_connection(options["url"])
    try:
        setup_loghandlers("DEBUG" if options["verbose"] else "WARNING" if options["quiet"] else "INFO")
        scheduler = ProgressReportingScheduler(
            connection=connection,
            interval=options["interval"],
            job_class=options["job_class"],
            queue_class=TASK_BUDGET_QUEUE_PATH,
            report_progress=report_progress,
        )
        scheduler.run(burst=options["burst"])
    finally:
        connection.close()


def run_beat(args: Sequence[str], *, report_progress: ProgressReporter) -> None:
    """Parse ``args`` strictly through the beat's options and run the scheduler in the foreground.

    Each pass of the scheduler's loop reports through ``report_progress``. An unknown option
    raises :class:`click.UsageError` before anything connects.
    """
    context = beat_command.make_context("rq-scheduler", list(args))
    with context:
        _run_scheduler(context.params, report_progress)
