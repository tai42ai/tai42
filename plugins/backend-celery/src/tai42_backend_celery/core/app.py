"""The Celery application, carrying the state of the worker or beat process that runs it.

``celery_app`` is built at import time from :class:`CelerySettings` (the RedBeat beat
scheduler that reports the beat's progress; pool restarts and task events enabled). Its worker
application is :class:`HostedWorker`, which leaves the process's signals to the host. Every
Redis connection it makes — the broker, the result backend and the RedBeat store — carries the kit's connection
behaviour (retry, socket timeouts); see :mod:`tai42_backend_celery.core.redis_seam`.
The worker's fork guard (:mod:`tai42_kit.backend.fork_safety`) evicts the inherited monitoring client
in every child.

The state the runtime's build sets for the process that runs the app lives on the app itself
(:class:`HostedCelery`), and the runtime hands celery that object: a config reload re-imports this
package while the process runs, and every step, signal receiver, task and beat celery runs still
reads the state the build set. The worker's celery signal receivers are connected by the runtime's
build too (:mod:`~tai42_backend_celery.core.worker_signals`).
"""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any

from celery import Celery
from celery.apps.worker import Worker
from celery.platforms import IS_macOS
from celery_pydantic import pydantic_celery
from tai42_kit.clients.impl.redis import redis_from_url_kwargs

from tai42_backend_celery.core.redis_seam import KitAMQP, KitLoader, configure_redis_seam
from tai42_backend_celery.core.settings import celery_settings

if TYPE_CHECKING:
    from tai42_contract.backend.runtime import ProgressReporter
    from tai42_kit.backend import SharedLoop

    from tai42_backend_celery.core.embedded_beat import EmbeddedBeat
    from tai42_backend_celery.core.job_slots import JobWatch
    from tai42_backend_celery.core.liveness_step import WorkerProgress


# --- app factory ---------------------------------------------------------------


class HostedWorker(Worker):
    """Celery's worker application, run by a host that owns the process's signals.

    The host routes its stop signals to the worker's warm and cold stop through its own composed
    chain (:class:`~tai42_backend_celery.core.backend.CeleryWorkerRuntime`), on every pool. So the
    worker installs no signal handler of its own: neither its stop handlers nor its ``SIGHUP``
    restart, its ``SIGUSR1`` stack dump or its ``SIGUSR2`` remote debugger.
    """

    def install_platform_tweaks(self, worker: Any) -> None:
        """Apply Celery's macOS proxy-detection workaround, and install no signal handler."""
        if IS_macOS:
            self.macOS_proxy_detection_workaround()


class HostedCelery(Celery):
    """A Celery application whose worker application is :class:`HostedWorker`, carrying its process's state.

    The runtime that runs the app sets each piece in its build, and celery hands the app to every
    reader (a step as its parent's ``app``, a task as its ``app``, the beat's scheduler as its
    ``app``); each is ``None`` until set:

    - ``beat_reporter``: the progress reporter of the beat this app runs, the standalone beat's or
      the one a worker embeds (:func:`~tai42_backend_celery.core.beat_scheduler.register`).
    - ``worker_progress``: the worker's progress reporter and job budget, which its progress step
      reads (:func:`~tai42_backend_celery.core.liveness_step.register`).
    - ``embedded_beat``: the liveness file, grace and service of the worker's embedded beat
      (:func:`~tai42_backend_celery.core.embedded_beat.register`).
    - ``job_watch``: the worker process's watch over its in-process jobs, set as its consumer
      starts (:func:`~tai42_backend_celery.core.job_slots.watch`).
    - ``shared_loop``: the host's loop a green pool's jobs run on
      (:meth:`~tai42_backend_celery.core.tasks.AsyncTask.share_loop`).
    """

    beat_reporter: ProgressReporter | None = None
    worker_progress: WorkerProgress | None = None
    embedded_beat: EmbeddedBeat | None = None
    job_watch: JobWatch | None = None
    shared_loop: SharedLoop | None = None

    @cached_property
    def Worker(self) -> type[HostedWorker]:  # noqa: N802 - Celery's own attribute name
        """The worker application, bound to this app as Celery binds its own."""
        return self.subclass_with_self(HostedWorker)


def create_celery_app() -> HostedCelery:
    """Build and configure the Celery app from the ``CELERY_*`` settings.

    The loader and the AMQP facet select the kit's Redis result backend and broker
    transport for whatever Redis URL celery resolves; RedBeat's client takes the
    ``CELERY_REDBEAT_*`` connection through ``redbeat_redis_options``. Raises
    :class:`~tai42_backend_celery.core.redis_seam.UnsupportedRedisUrlError` for a Redis
    URL form the kit's connection cannot carry.
    """
    settings = celery_settings()
    app = HostedCelery(
        "TaiMCPCelery",
        broker=settings.broker_url,
        backend=settings.result_backend,
        loader=KitLoader,
        amqp=KitAMQP,
    )

    pydantic_celery(app)

    app.conf.update(
        beat_scheduler="tai42_backend_celery.core.beat_scheduler.ProgressReportingScheduler",
        redbeat_redis_url=settings.redbeat.redis_url,
        redbeat_key_prefix=settings.redbeat_key_prefix,
        beat_max_loop_interval=settings.beat_max_loop_interval,
        timezone="UTC",
        # A task sent with no queue (a beat-fired schedule, an unlabelled dispatch) goes to the
        # settings queue, which a worker started without a label consumes.
        task_default_queue=settings.queue_name,
        worker_pool_restarts=True,
        worker_concurrency=settings.worker_concurrency,
        worker_send_task_events=True,  # worker emits task-* events
        task_send_sent_event=True,  # beat/client emits 'task-sent'
        # The dispatch job budget: the prefork pool kills a task (dispatched or beat-fired)
        # that runs past it. Bound when the worker starts.
        task_time_limit=settings.task_timeout,
    )
    configure_redis_seam(app)
    # Built once the seam has checked the RedBeat URL: every key but the URL reaches
    # RedBeat's ``Redis.from_url``, the kit connection class for the URL's scheme
    # included; RedBeat decodes responses on ``redis://`` itself, and ``rediss://`` /
    # ``unix://`` match it.
    app.conf.redbeat_redis_options = {
        key: value
        for key, value in redis_from_url_kwargs(
            {**settings.redbeat.client_kwargs(), "decode_responses": True}, sync=True
        ).items()
        if key != "url"
    }

    return app


celery_app = create_celery_app()
