"""RQ backend settings.

A ``TaiBaseSettings`` subclass reading the ``RQ_`` env group, exposed through the
cached ``rq_settings`` accessor. The dispatch surface the host and this backend
must agree on (``manifest_key`` / ``task_timeout`` / ``tool_name_arg``) is
INHERITED from ``BackendDispatchSettings`` rather than mirrored here, so its
names, defaults and reload classes are declared once for both sides while this
group keeps its own ``RQ_`` prefix.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from pydantic_settings import SettingsConfigDict
from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.settings import DefaultNamespaceMixin, TaiBaseSettings, settings_cache


class RqSettings(BackendDispatchSettings, DefaultNamespaceMixin, TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="RQ_")

    # ``redis_url`` falls back to the shared ``TAI_DEFAULT_REDIS_URL`` when
    # ``RQ_REDIS_URL`` is unset; a set ``RQ_REDIS_URL`` always wins.
    tai_default_fields: ClassVar[Mapping[str, str]] = {"redis_url": "redis_url"}

    redis_url: str = "redis://localhost:6379/0"
    # Key prefix RQ uses for all of its Redis structures.
    rq_prefix: str = "rq:"

    @property
    def rq_scheduler_zset(self) -> str:
        return f"{self.rq_prefix}scheduler:scheduled_jobs"

    def rq_job_key(self, name: str) -> str:
        return f"{self.rq_prefix}job:{name}"

    def rq_job_dependencies(self, name: str) -> str:
        return f"{self.rq_prefix}job::{name}:dependencies"

    def rq_result_key(self, name: str) -> str:
        return f"{self.rq_prefix}results:{name}"

    def rq_worker_key(self, name: str) -> str:
        return f"{self.rq_prefix}worker:{name}"

    @property
    def rq_workers_key(self) -> str:
        return f"{self.rq_prefix}workers"

    @property
    def rq_queues_key(self) -> str:
        return f"{self.rq_prefix}queues"

    def rq_queue_key(self, queue_name: str) -> str:
        return f"{self.rq_prefix}queue:{queue_name}"

    def rq_scheduled_registry_key(self, queue_name: str) -> str:
        """Zset of one queue's ETA/countdown jobs (RQ's scheduled-job registry)."""
        return f"{self.rq_prefix}scheduled:{queue_name}"


@settings_cache
def rq_settings() -> RqSettings:
    return RqSettings()
