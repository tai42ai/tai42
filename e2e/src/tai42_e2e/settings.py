"""Harness-wide settings: coordinates for the externally provided Redis and
Postgres plus the boot/teardown knobs. Read from the ``TAI_E2E_`` env space."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HarnessSettings(BaseSettings):
    """Infra coordinates the harness itself connects to. These describe the
    Redis and Postgres that ``docker compose up -d`` (locally) or the CI
    ``services:`` block provide; the harness owns only logical isolation on top
    of them and never talks to Docker."""

    model_config = SettingsConfigDict(env_prefix="TAI_E2E_", extra="ignore")

    # Which plugin variant this pytest process runs under. One set per process,
    # selected here; ``tai42_e2e.variants.resolve_variants`` maps each to its
    # adapter and fails loudly on an unknown name.
    backend: str = "arq"  # arq | celery | rq
    identity: str = "redis"  # redis | fixture
    storage: str = "local"  # local | fixture

    # The RabbitMQ the celery backend isolates each stack on (a per-stack vhost).
    # Validated/used only when ``backend == "celery"``; the AMQP URL feeds the
    # stack broker URL, the management URL provisions and reaps the vhost.
    rabbitmq_url: str = "amqp://guest:guest@127.0.0.1:5672"
    rabbitmq_management_url: str = "http://guest:guest@127.0.0.1:15672"

    # The shared Redis. Every stack is pinned to a logical DB index under this
    # server; the harness never appends a DB index here itself.
    redis_url: str = "redis://127.0.0.1:6379"

    # The module-capable Redis (RediSearch + RedisJSON) the langgraph redis
    # checkpoint/store provider requires (the plain shared ``redis_url`` lacks the
    # modules). Unset by default: the agents-redis checkpoint suite skips with a compose
    # hint when absent. Each stack that needs it is pinned to its own logical DB here.
    checkpoint_redis_url: str | None = None

    # The shared Postgres admin connection. The harness connects here to create
    # the template DB and per-stack CREATE DATABASE / DROP DATABASE.
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    # The maintenance DB to connect to for CREATE/DROP DATABASE statements.
    pg_admin_db: str = "postgres"

    # Deadline for a stack to reach readiness (all app ports healthy, metrics
    # up, backend present in the census).
    boot_timeout: float = 30.0

    # Keep per-stack config + log dirs after teardown for debugging.
    keep_stacks: bool = Field(default=False)

    # Opt in to the monitoring suite (requires the compose ``monitoring``
    # profile to be up). When false, tests/monitoring is skipped at collection.
    monitoring: bool = Field(default=False)

    # Opt in to the marketplace suite (boots the tai42-marketplace sibling as
    # harness-managed processes against the shared Redis/Postgres). When false,
    # tests/marketplace is skipped at collection.
    marketplace: bool = Field(default=False)

    # Opt in to the fleet-consistency suite (boots the multi-worker / REPLICAS stacks
    # under the app worker bus). When false, tests/fleet is skipped at collection.
    fleet: bool = Field(default=False)

    @property
    def redis_host_port(self) -> tuple[str, int]:
        """The (host, port) of the shared Redis, parsed from ``redis_url``."""
        return _host_port(self.redis_url, "TAI_E2E_REDIS_URL")

    @property
    def checkpoint_redis_host_port(self) -> tuple[str, int]:
        """The (host, port) of the module-capable checkpoint Redis, parsed from
        ``checkpoint_redis_url``. Only read when the URL is set."""
        if self.checkpoint_redis_url is None:
            raise ValueError("checkpoint_redis_url is unset; the checkpoint Redis is not configured")
        return _host_port(self.checkpoint_redis_url, "TAI_E2E_CHECKPOINT_REDIS_URL")


def _host_port(url: str, env_var: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"{env_var} must include host and port: {url!r}")
    return parsed.hostname, parsed.port
