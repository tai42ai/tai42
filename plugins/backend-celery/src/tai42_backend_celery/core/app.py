"""The Celery application and the pool-child fork-safety hooks.

``celery_app`` is built at import time from :class:`CelerySettings` (RedBeat beat
scheduler; pool restarts and task events enabled). ``worker_process_init`` evicts
the monitoring vendor client in each forked child (its parent-owned
threads/sockets are dead there; the child rebuilds it on first use).
``worker_process_shutdown`` flushes buffered monitoring spans and closes each
task loop's pooled clients before the child exits.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from celery import Celery, signals
from celery_pydantic import pydantic_celery
from tai42_contract.app import tai42_app

from tai42_backend_celery.core.settings import celery_settings

logger = logging.getLogger(__name__)


# --- fork-safety signal hooks ------------------------------------------------


@signals.worker_process_init.connect
def on_worker_process_init(sender: Any = None, **kwargs: Any) -> None:
    """Runs in each freshly forked pool child before it takes work.

    Evicts the monitoring vendor client (its parent-owned exporter
    threads/sockets are dead here); the child rebuilds it on first use. On macOS
    a warning names the platform hazard: the system resolver is not fork-safe, so
    a child's first telemetry export may crash it — prefer the ``solo`` or
    ``threads`` pool there.
    """
    tai42_app.monitoring.active.writer.shutdown()
    logger.info("worker child %s: evicted the pre-fork monitoring client (fork-safe shutdown)", sender)
    if sys.platform == "darwin":
        logger.warning(
            "macOS prefork child: the system DNS resolver is not fork-safe, so telemetry export from this "
            "child may crash it; use the 'solo' or 'threads' worker pool on macOS if that happens"
        )


@signals.worker_process_shutdown.connect
def on_worker_process_shutdown(sender: Any = None, **kwargs: Any) -> None:
    """Runs in each pool child as it exits: flush buffered monitoring spans, then
    close each task loop's pooled clients."""
    try:
        tai42_app.monitoring.active.writer.flush()
    except Exception as e:
        logger.warning("Error flushing monitoring on worker child shutdown: %s", e)

    from tai42_backend_celery.core.tasks import callback_task, tool_execution

    for task in (tool_execution, callback_task):
        try:
            task.close_loop()
        except Exception as e:
            logger.warning("Error closing task loop clients on worker child shutdown: %s", e)


# --- app factory ---------------------------------------------------------------


def create_celery_app() -> Celery:
    settings = celery_settings()
    app = Celery(
        "TaiMCPCelery",
        broker=settings.broker_url,
        backend=settings.result_backend,
    )

    pydantic_celery(app)

    app.conf.update(
        beat_scheduler="redbeat.RedBeatScheduler",
        redbeat_redis_url=settings.redbeat_redis_url,
        redbeat_key_prefix=settings.redbeat_key_prefix,
        beat_max_loop_interval=settings.beat_max_loop_interval,
        timezone="UTC",
        worker_pool_restarts=True,
        worker_concurrency=settings.worker_concurrency,
        worker_send_task_events=True,  # worker emits task-* events
        task_send_sent_event=True,  # beat/client emits 'task-sent'
    )

    return app


celery_app = create_celery_app()
