"""tai42-accounts-postgres: the Postgres-backed accounts provider plugin.

Importing this package registers the ``"accounts-postgres"`` provider (see
:mod:`tai42_accounts_postgres.provider`). The ``/api/login/*`` and
``/api/auth/users*`` routes load separately through the manifest's
``routers_modules``.
"""

from __future__ import annotations

from tai42_accounts_postgres.provider import PostgresAccountsProvider

__all__ = [
    "PostgresAccountsProvider",
]
