"""Pooled clients + the generic client facade.

Each client subclasses ``PooledClient`` (per-loop pooling, satisfies the contract
``BaseClient`` Protocol) and is reached by class through ``client_ctx``. The
driver each impl needs is declared by an extra (``tai42-kit[curl]`` /
``[postgres]``); import a driver submodule only when its driver is installed.
"""

from tai42_contract.errors import ClientDisconnectedError

from tai42_kit.clients.base import PooledClient, shutdown_all_clients
from tai42_kit.clients.facade import client_ctx
from tai42_kit.clients.settings import (
    ClientSettings,
    PostgresConnectionSettings,
    RedisConnectionSettings,
)

__all__ = [
    "ClientDisconnectedError",
    "ClientSettings",
    "PooledClient",
    "PostgresConnectionSettings",
    "RedisConnectionSettings",
    "client_ctx",
    "shutdown_all_clients",
]
