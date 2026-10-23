"""RQ backend settings.

A ``TaiBaseSettings`` subclass reading the ``RQ_`` env group, exposed through the
cached ``rq_settings`` accessor. Its Redis connection is :class:`RqRedisSettings`, the
kit's connection settings under ``RQ_``, and :func:`rq_client_kwargs` is the one set of
client kwargs every Redis connection of this backend is built from. The dispatch surface the host and this backend
must agree on (``manifest_key`` / ``task_timeout`` / ``tool_name_arg``) is
INHERITED from ``BackendDispatchSettings`` rather than mirrored here, so its
names, defaults and reload classes are declared once for both sides while this
group keeps its own ``RQ_`` prefix.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.clients import RedisConnectionSettingsBase
from tai42_kit.settings import TaiBaseSettings, settings_cache


class RqRedisSettings(RedisConnectionSettingsBase):
    """The RQ backend's Redis connection, composed from the kit connection shape under ``RQ_``.

    ``RQ_REDIS_URL`` / ``RQ_REDIS_MAX_CONNECTIONS`` fall back to the shared
    ``TAI_DEFAULT_*`` namespace when unset; the retry and the socket timeouts
    (``RQ_RETRY_ATTEMPTS``, ``RQ_RETRY_ON_TIMEOUT``, ``RQ_SOCKET_TIMEOUT``,
    ``RQ_SOCKET_CONNECT_TIMEOUT``) are read from ``RQ_*`` alone.
    """

    model_config = SettingsConfigDict(env_prefix="RQ_")

    redis_url: str | None = "redis://localhost:6379/0"


class RqSettings(BackendDispatchSettings, TaiBaseSettings):
    """RQ backend settings from the ``RQ_`` env group, with RQ's Redis key shapes."""

    model_config = SettingsConfigDict(env_prefix="RQ_")

    # The Redis connection every side uses, composed from the kit (a field, not a base).
    redis: RqRedisSettings = Field(default_factory=RqRedisSettings)
    # Key prefix RQ uses for all of its Redis structures.
    rq_prefix: str = "rq:"
    # The RQ queue name every enqueue, worker, and scheduler binds. RQ's own
    # default is ``default``; co-tenant deployments on one logical DB diverge here
    # so each worker consumes only its own jobs (the queue key is
    # ``<rq_prefix>queue:<queue_name>``).
    queue_name: str = "default"

    @property
    def rq_scheduler_zset(self) -> str:
        """The zset key RQ's scheduler holds its scheduled jobs in."""
        return f"{self.rq_prefix}scheduler:scheduled_jobs"

    def rq_job_key(self, name: str) -> str:
        """The hash key for job ``name``."""
        return f"{self.rq_prefix}job:{name}"

    def rq_job_dependencies(self, name: str) -> str:
        """The dependency-set key for job ``name``."""
        return f"{self.rq_prefix}job::{name}:dependencies"

    def rq_result_key(self, name: str) -> str:
        """The result key for job ``name``."""
        return f"{self.rq_prefix}results:{name}"

    def rq_worker_key(self, name: str) -> str:
        """The registration key for worker ``name``."""
        return f"{self.rq_prefix}worker:{name}"

    @property
    def rq_workers_key(self) -> str:
        """The set key listing every registered worker."""
        return f"{self.rq_prefix}workers"

    @property
    def rq_queues_key(self) -> str:
        """The set key listing every known queue."""
        return f"{self.rq_prefix}queues"

    def rq_queue_key(self, queue_name: str) -> str:
        """The list key for queue ``queue_name``."""
        return f"{self.rq_prefix}queue:{queue_name}"

    def rq_scheduled_registry_key(self, queue_name: str) -> str:
        """Zset of one queue's ETA/countdown jobs (RQ's scheduled-job registry)."""
        return f"{self.rq_prefix}scheduled:{queue_name}"


@settings_cache
def rq_settings() -> RqSettings:
    """The cached :class:`RqSettings`."""
    return RqSettings()


def rq_client_kwargs() -> dict[str, Any]:
    """The kit Redis client kwargs for this backend's connection settings, read as bytes.

    The URL, the connection cap, the socket timeouts and the connection retry, all from
    :class:`RqRedisSettings`. Responses stay bytes, the shape RQ reads.
    """
    return {**rq_settings().redis.client_kwargs(), "decode_responses": False}
