"""The celery worker process's signal receivers: the pool child's platform warning and the shutdown of the jobs' work.

The worker runtime's build connects them (:func:`connect`) for the app it runs, never at import:
celery's signals are process globals that a config reload's re-import of this package does not
reset, so a receiver connected at import would be connected once more by every reload. Each
receiver is connected under a ``dispatch_uid`` naming it and the app it serves, so a second
connect for the same app replaces nothing and adds nothing.

On macOS, ``worker_process_init`` warns in each forked pool child that the system resolver is
not fork-safe. A process that ran jobs shuts their work down once, as it stops
(:func:`shut_down_jobs`): it closes every loop the jobs ran on, with the kit resources bound to it
(pooled clients, checkpoint savers, stores), then flushes buffered monitoring spans. A prefork
child does it at its exit (``worker_process_shutdown``, which only the prefork pool sends); the
worker process of a pool that runs its jobs in it (``solo``, ``threads``, ``gevent``,
``eventlet``) does it once a warm stop has stopped the pool (``worker_shutdown``).
"""

from __future__ import annotations

import logging
import sys
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from celery import signals
from tai42_contract.app import tai42_app

from tai42_backend_celery.core import job_slots
from tai42_backend_celery.core.tasks import CALLBACK_TASK, TOOL_EXECUTION_TASK, AsyncTask

if TYPE_CHECKING:
    from tai42_backend_celery.core.app import HostedCelery

logger = logging.getLogger(__name__)


def on_worker_process_init(sender: Any = None, **kwargs: Any) -> None:
    """Runs in each freshly forked pool child before it takes work.

    On macOS a warning names the platform hazard: the system resolver is not fork-safe, so
    a child's first telemetry export may crash it — prefer the ``solo`` or ``threads`` pool there.
    """
    if sys.platform == "darwin":
        logger.warning(
            "macOS prefork child: the system DNS resolver is not fork-safe, so telemetry export from this "
            "child may crash it; use the 'solo' or 'threads' worker pool on macOS if that happens"
        )


def shut_down_jobs(app: HostedCelery) -> None:
    """Close every loop ``app``'s jobs ran on in this process, with its kit resources, then flush monitoring spans.

    The loops are each task's own; on a green pool the host's loop, which the jobs shared, is let
    go, and the host's stop closes it and releases its kit resources. The flush runs last so the
    spans written while the loops close (a cancelled background task's, a resource's at its release)
    are exported too: a prefork child calls ``os._exit`` right after, so nothing flushes after it.
    Every step runs even when an earlier one fails; the failures are then raised together.
    """
    tool_execution = cast("AsyncTask", app.tasks[TOOL_EXECUTION_TASK])
    callback_task = cast("AsyncTask", app.tasks[CALLBACK_TASK])
    failures: list[Exception] = []
    for step in (
        tool_execution.close_loop,
        callback_task.close_loop,
        partial(AsyncTask.close_shared_loop, app),
        tai42_app.monitoring.active.writer.flush,
    ):
        try:
            step()
        except Exception as failure:
            failures.append(failure)
    if failures:
        raise ExceptionGroup("shutting down the jobs' work failed", failures)


def connect(app: HostedCelery) -> None:
    """Connect the worker process's receivers for ``app``, the app the worker runs; a second call adds none.

    The shutdown receivers close over ``app``, so they shut down the work of the tasks it holds
    whichever copy of this package is imported when the worker stops. Every receiver is held
    strongly: nothing else holds a closure, and a reload's re-import drops this module's own.
    """

    def on_worker_process_shutdown(sender: Any = None, **kwargs: Any) -> None:
        """Shut down the jobs' work in a prefork child at its exit (:func:`shut_down_jobs`); celery logs a failure."""
        shut_down_jobs(app)

    def on_worker_shutdown(sender: Any = None, **kwargs: Any) -> None:
        """Shut down the jobs' work in the worker process, when its pool runs them there, as the worker stops.

        celery sends this in the worker process once a warm stop has stopped the pool. A prefork
        worker's jobs ran in its children, which shut their work down at their own exit, so it does
        nothing there. A failure raises, and celery logs it with its traceback.
        """
        if job_slots.in_process_pool(app):
            shut_down_jobs(app)

    signals.worker_process_init.connect(on_worker_process_init, weak=False, dispatch_uid="tai42-celery-pool-child-init")
    signals.worker_process_shutdown.connect(
        on_worker_process_shutdown, weak=False, dispatch_uid=f"tai42-celery-pool-child-shutdown-{id(app)}"
    )
    signals.worker_shutdown.connect(
        on_worker_shutdown, weak=False, dispatch_uid=f"tai42-celery-worker-shutdown-{id(app)}"
    )
