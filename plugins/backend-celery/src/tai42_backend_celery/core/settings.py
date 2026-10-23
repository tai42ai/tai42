"""Celery backend settings — the ``CELERY_`` env group.

The RedBeat store's Redis connection is :class:`CeleryRedbeatRedisSettings`, the kit's
connection settings under ``CELERY_REDBEAT_``. The broker and the result backend take
their identity from their own URL strings (``broker_url`` / ``result_backend``, which may
name a non-Redis service); when such a URL is Redis, its connection behaviour (retry,
socket timeouts) comes from :class:`CeleryBrokerRedisOptions` (``CELERY_BROKER_*``) and
:class:`CeleryResultBackendRedisOptions` (``CELERY_RESULT_BACKEND_*``).

``tool_name_arg`` / ``task_timeout`` / ``manifest_key`` come from the shared
:class:`~tai42_kit.backend.BackendDispatchSettings` mixin, so this side and the
host declare the dispatch surface once and cannot drift on its names, defaults or
reload classes; ``manifest_key`` names the env var the live manifest JSON is
written into so preforked children inherit it. ``redbeat_schedule_key`` yields the
exact ``redbeat::schedule`` zset key RedBeat maintains.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.clients import RedisConnectionOptions, RedisConnectionSettingsBase
from tai42_kit.settings import DefaultNamespaceMixin, TaiBaseSettings, settings_cache


class CeleryRedbeatRedisSettings(RedisConnectionSettingsBase):
    """The RedBeat store's Redis connection, composed from the kit connection shape under ``CELERY_REDBEAT_``.

    ``CELERY_REDBEAT_REDIS_URL`` / ``CELERY_REDBEAT_REDIS_MAX_CONNECTIONS`` fall back to
    the shared ``TAI_DEFAULT_*`` namespace when unset; the retry and the socket timeouts
    (``CELERY_REDBEAT_RETRY_ATTEMPTS``, ``CELERY_REDBEAT_RETRY_ON_TIMEOUT``,
    ``CELERY_REDBEAT_SOCKET_TIMEOUT``, ``CELERY_REDBEAT_SOCKET_CONNECT_TIMEOUT``) are read
    from ``CELERY_REDBEAT_*`` alone. The beat scheduler and the schedule tools both connect
    with it.
    """

    model_config = SettingsConfigDict(env_prefix="CELERY_REDBEAT_")

    redis_url: str | None = "redis://localhost:6379/0"


class CeleryResultBackendRedisOptions(RedisConnectionOptions):
    """Connection behaviour of a Redis result backend (``CELERY_RESULT_BACKEND_*``).

    Read only when the result backend URL is Redis: the URL is the backend's whole
    identity, and these options are its retry and socket timeouts. A knob left unset keeps
    celery's own default.
    """

    model_config = SettingsConfigDict(env_prefix="CELERY_RESULT_BACKEND_")


class CeleryBrokerRedisOptions(RedisConnectionOptions):
    """Connection behaviour of a Redis broker (``CELERY_BROKER_*``), its read and its write side alike.

    Read only when a broker URL is Redis: the URL is the broker's whole identity, and these
    options are its retry and socket timeouts. A knob left unset keeps kombu's own default.
    The reads kombu polls (a worker's task and fanout reads, a control command's reply
    read) are never retried: a drop there raises.
    """

    model_config = SettingsConfigDict(env_prefix="CELERY_BROKER_")


class CelerySettings(BackendDispatchSettings, DefaultNamespaceMixin, TaiBaseSettings):
    """The Celery backend's ``CELERY_`` settings: broker, result backend, RedBeat store, and pool size."""

    model_config = SettingsConfigDict(env_prefix="CELERY_")

    # The broker and the result backend fall back to the shared
    # ``TAI_DEFAULT_REDIS_URL`` when their ``CELERY_*`` var is unset; a set
    # ``CELERY_*`` value always wins. With the default set and
    # ``CELERY_BROKER_URL`` unset the broker resolves to Redis rather than the
    # AMQP class default — Redis is a first-class Celery broker; an operator on
    # AMQP sets ``CELERY_BROKER_URL`` explicitly.
    tai_default_fields: ClassVar[Mapping[str, str]] = {
        "broker_url": "redis_url",
        "result_backend": "redis_url",
    }

    broker_url: str = "amqp://localhost:5672//"
    result_backend: str = "redis://localhost:6379/0"
    # The connection behaviour of a Redis broker / result backend URL.
    broker_redis: CeleryBrokerRedisOptions = Field(default_factory=CeleryBrokerRedisOptions)
    result_backend_redis: CeleryResultBackendRedisOptions = Field(default_factory=CeleryResultBackendRedisOptions)
    # The RedBeat store's Redis connection (a field, not a base).
    redbeat: CeleryRedbeatRedisSettings = Field(default_factory=CeleryRedbeatRedisSettings)
    redbeat_key_prefix: str = "redbeat:"
    beat_max_loop_interval: int = 60

    # Prefork pool size, capped at ONE child so a bus op can re-fork the WHOLE
    # pool within the turnover's confirmation budget: Celery's ``pool_restart`` is
    # soft (a busy child cannot recycle until its task completes), so a larger
    # pool under load can leave children un-recycled. Live-reloading a larger pool
    # would need a non-forking pool or a full worker respawn on reload.
    worker_concurrency: int = 1

    @property
    def redbeat_schedule_key(self) -> str:
        """The RedBeat schedule zset key (``redbeat::schedule`` by default)."""
        return f"{self.redbeat_key_prefix}:schedule"

    def redbeat_task_key(self, name: str) -> str:
        """The RedBeat entry hash key for ``name`` (idempotent on prefixed input)."""
        if name.startswith(self.redbeat_key_prefix):
            return name
        return f"{self.redbeat_key_prefix}{name}"


@settings_cache
def celery_settings() -> CelerySettings:
    """The cached :class:`CelerySettings` for this process."""
    return CelerySettings()
