"""tai42-accounts-postgres: the Postgres-backed accounts provider plugin.

Importing this package registers the ``"accounts-postgres"`` provider (see
:mod:`tai42_accounts_postgres.provider`). The ``/api/login/*`` and
``/api/auth/users*`` routes load separately through the manifest's
``routers_modules``.
"""

from __future__ import annotations

# Importing this module arms the ``on_startup`` hook that registers the plugin's
# ``accounts`` backup section on the host's AppBackup registry (import side effect,
# the same pattern as the provider registration below).
from tai42_accounts_postgres import backup as _backup  # noqa: F401
from tai42_accounts_postgres.provider import PostgresAccountsProvider

__all__ = [
    "PostgresAccountsProvider",
]
