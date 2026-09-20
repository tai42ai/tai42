"""Backend variant adapters (arq, rq, celery)."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from tai42_e2e.rabbitx import RabbitAdmin, broker_url_for
from tai42_e2e.topology import Infra, InfraUnavailableError, StackResources
from tai42_e2e.variants.census import BrokerLease

if TYPE_CHECKING:
    from tai42_e2e.settings import HarnessSettings


class BackendVariant(abc.ABC):
    """One Backend plugin: the manifest ``backend_module`` string, the feature
    env pointing it at a stack's isolated resources, and its process model. The
    live-fleet census is NOT here — it reads the app-owned bus (:func:`bus_census`),
    the same for every backend."""

    name: str
    module: str
    # The Backend class + defining module the ``/api/backend`` identity door reports
    # for this plugin (the door names the REGISTERED class, which lives in the
    # plugin's implementation module, not necessarily its package root).
    provider_class: str
    provider_module: str

    @abc.abstractmethod
    def feature_env(self, res: StackResources) -> dict[str, str]:
        """The backend plugin's env group, pointed at this stack's resources."""

    def infra_check(self, settings: HarnessSettings) -> None:
        """Extra reachability beyond the shared Redis + Postgres. The default is
        a no-op (Redis-only backends); overridden where a backend needs its own
        broker, raising :class:`InfraUnavailableError` with the compose hint."""
        return

    def allocate_broker(self, infra: Infra, stack_id: str) -> BrokerLease | None:
        """Reserve this stack's isolated broker resource. The default backend
        rides on the shared Redis and leases nothing (``None``)."""
        return None

    @abc.abstractmethod
    def extra_backend_processes(self) -> list[list[str]]:
        """The extra ``tai backend <args>`` invocations a ``run_backend`` stack
        must spawn alongside the worker — each one a separate process the boot
        engine gives its own ProcessHandle, log capture, and teardown leak-reap.

        Empty (``[]``) is the explicit "the worker hosts everything" answer, not a
        default escape hatch: arq's recurring scheduler is a self-rescheduling
        queue job the worker runs, whereas celery's RedBeat and rq's rq-scheduler
        each need their own long-lived process before a ``schedule_task`` schedule
        can ever fire."""

    @abc.abstractmethod
    def task_timeout_env(self, seconds: int) -> dict[str, str]:
        """Env that bounds a backend-execution (``sync_task``) result wait to
        ~``seconds``. The worker-crash spec sets it low so a job orphaned by a
        SIGKILLed worker surfaces a bounded, observable terminal instead of
        blocking on the multi-minute production default."""

    # The tool-run status an in-flight job reaches when the ``tai backend worker`` process
    # group is SIGKILLed under it. The invariant is always a bounded, loud terminal, never
    # an eternal ``running``, but the concrete outcome is a property of each backend's
    # process model, so each variant declares its own and the spec asserts exactly that.
    crashed_run_terminal: str


class ArqVariant(BackendVariant):
    name = "arq"
    module = "tai42_backend_arq"
    provider_class = "ArqBackend"
    provider_module = "tai42_backend_arq.backend"
    # The worker process itself executes the job, so killing its process group
    # orphans the job: no in-worker monitor survives to fail it, the sync_task result
    # wait times out at ``task_timeout``, and the run is recorded failed.
    crashed_run_terminal = "failed"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        # The logical DB isolates co-tenant stacks, but a DB index re-leased while a
        # leaked worker still consumes it would let that orphan dequeue this stack's
        # ``tool_execution`` jobs off the shared default queue key. Namespacing the
        # queue by the same per-stack token as the bus keeps each worker on its own.
        return {
            "ARQ_REDIS_URL": res.redis_url,
            "ARQ_QUEUE_NAME": f"{res.bus_namespace}:arq:queue",
        }

    def extra_backend_processes(self) -> list[list[str]]:
        # arq's recurring scheduler is the self-rescheduling ``task_scheduler``
        # queue job the worker runs, so a schedule fires without any extra process.
        return []

    def task_timeout_env(self, seconds: int) -> dict[str, str]:
        return {"ARQ_TASK_TIMEOUT": str(seconds)}


class RqVariant(BackendVariant):
    name = "rq"
    module = "tai42_backend_rq"
    provider_class = "RqBackend"
    provider_module = "tai42_backend_rq.backend"
    # RQ runs each job in a work-horse child in its OWN process group (``os.setpgrp``),
    # outside the worker master's group: killing the master leaves the horse running, it
    # finishes the job and writes the result, and the run is recorded succeeded.
    crashed_run_terminal = "succeeded"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        # Same reasoning as arq: namespace the RQ queue by the per-stack token so a
        # leaked worker on a re-leased DB index cannot consume this stack's jobs.
        return {
            "RQ_REDIS_URL": res.redis_url,
            "RQ_QUEUE_NAME": f"{res.bus_namespace}:default",
        }

    def extra_backend_processes(self) -> list[list[str]]:
        # ``schedule_task`` recurring jobs only reach the queue when the ``rqscheduler``
        # daemon moves them (the worker's own ``with_scheduler`` covers only one-shot
        # ``enqueue_at`` jobs). ``tai backend beat`` runs that daemon; ``-i 1`` polls once
        # a second so a short-interval schedule fires promptly.
        return [["beat", "-i", "1"]]

    def task_timeout_env(self, seconds: int) -> dict[str, str]:
        return {"RQ_TASK_TIMEOUT": str(seconds)}


class CeleryVariant(BackendVariant):
    name = "celery"
    module = "tai42_backend_celery"
    provider_class = "CeleryBackend"
    provider_module = "tai42_backend_celery.core.backend"
    # The prefork child executing the job lives in the worker's process group, so
    # killing the group takes the job with it: the sync_task result wait times out at
    # ``task_timeout`` and the run is recorded failed.
    crashed_run_terminal = "failed"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        broker_url = self._require_broker(res)
        # The task queue rides a per-stack RabbitMQ vhost (``allocate_broker``), so
        # tool_execution is already isolated and reaped with the lease — no queue
        # rename is needed. Result records key on unique task ids. The one shared
        # Redis structure is RedBeat's schedule store on the logical DB, so its key
        # prefix is namespaced by the per-stack token to stop a leaked beat on a
        # re-leased DB from firing this stack's schedules.
        return {
            "CELERY_BROKER_URL": broker_url,
            "CELERY_RESULT_BACKEND": res.redis_url,
            "CELERY_REDBEAT_REDIS_URL": res.redis_url,
            "CELERY_REDBEAT_KEY_PREFIX": f"{res.bus_namespace}:redbeat:",
        }

    def infra_check(self, settings: HarnessSettings) -> None:
        admin = RabbitAdmin(settings.rabbitmq_management_url)
        try:
            admin.check_reachable()
        except Exception as exc:
            raise InfraUnavailableError(
                f"RabbitMQ not reachable ({exc}). Start it with `docker compose --profile celery up -d`."
            ) from exc

    def allocate_broker(self, infra: Infra, stack_id: str) -> BrokerLease | None:
        settings = infra.settings
        admin = RabbitAdmin(settings.rabbitmq_management_url)
        vhost = f"tai42_e2e_{stack_id}"
        admin.create_vhost(vhost)
        return BrokerLease(broker_url=broker_url_for(settings.rabbitmq_url, vhost), admin=admin, vhost=vhost)

    def extra_backend_processes(self) -> list[list[str]]:
        # RedBeat schedules only fire while a beat process runs; ``tai backend
        # beat`` starts it (RedBeat reads CELERY_REDBEAT_REDIS_URL from the env).
        # ``--max-interval 2`` bounds the beat loop so a schedule created after
        # beat starts is picked up within ~2s rather than the 60s conf default.
        return [["beat", "--max-interval", "2"]]

    def task_timeout_env(self, seconds: int) -> dict[str, str]:
        return {"CELERY_TASK_TIMEOUT": str(seconds)}

    @staticmethod
    def _require_broker(res: StackResources) -> str:
        if res.broker_url is None:
            raise RuntimeError("celery variant requires a per-stack broker_url; allocate_broker must run first")
        return res.broker_url


BACKENDS: dict[str, BackendVariant] = {"arq": ArqVariant(), "celery": CeleryVariant(), "rq": RqVariant()}
