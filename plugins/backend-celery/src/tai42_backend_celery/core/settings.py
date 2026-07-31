"""Celery backend settings — the ``CELERY_`` env group.

``tool_name_arg`` / ``task_timeout`` / ``manifest_key`` mirror the host's base
backend settings (same names and defaults) so both sides agree without importing
each other; ``manifest_key`` names the env var the live manifest JSON is written
into so preforked children inherit it. ``redbeat_schedule_key`` yields the exact
``redbeat::schedule`` zset key RedBeat maintains.
"""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class CelerySettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="CELERY_")

    broker_url: str = "amqp://localhost:5672//"
    result_backend: str = "redis://localhost:6379/0"
    redbeat_redis_url: str = "redis://localhost:6379/0"
    redbeat_key_prefix: str = "redbeat:"
    beat_max_loop_interval: int = 60

    # Prefork pool size, capped at ONE child so a bus op can re-fork the WHOLE
    # pool within the turnover's confirmation budget: Celery's ``pool_restart`` is
    # soft (a busy child cannot recycle until its task completes), so a larger
    # pool under load can leave children un-recycled. Live-reloading a larger pool
    # would need a non-forking pool or a full worker respawn on reload.
    worker_concurrency: int = 1

    # Shared backend-settings surface (host-agreed names and defaults).
    tool_name_arg: str = "backend_tool_name"
    task_timeout: int = 300
    manifest_key: str = "MANIFEST_KEY"

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
    return CelerySettings()
