"""Kit Redis connections built from a URL, as the connection tests use them."""

from __future__ import annotations

from typing import Any

from tai42_kit.clients.impl import redis as redis_mod
from tai42_kit.clients.impl import redis_connections as connections_mod

# A URL no test dials through these helpers alone: a connection built on it runs its retry, never its connect.
UNDIALLED = "redis://127.0.0.1:6379/0"


def kit_options(retry_attempts: int, *, sync: bool) -> dict[str, Any]:
    """The kit's connection options for a budget of ``retry_attempts``, labelled ``L``."""
    return redis_mod.redis_connection_options({"retry_attempts": retry_attempts}, sync=sync, label="L")


def sync_kit_connection(url: str, **kwargs: Any) -> Any:
    """A connection of the kit sync pool for ``url``, not yet connected."""
    return connections_mod.KitSyncConnectionPool.from_url(url, **kwargs).make_connection()


def async_kit_connection(url: str, **kwargs: Any) -> Any:
    """A connection of the kit async client's pool for ``url``, not yet connected."""
    return redis_mod.async_redis_from_url(url, **kwargs).connection_pool.make_connection()
