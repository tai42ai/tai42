"""tai42-accounts-postgres: the Postgres-backed accounts provider plugin.

Importing this package registers the ``"accounts-postgres"`` provider (see
:mod:`tai42_accounts_postgres.provider`). The ``/api/login/*`` and
``/api/auth/users*`` routes load separately through the manifest's
``routers_modules``.
"""

from __future__ import annotations

# NOTE: the backup-section arming does NOT live here. The package __init__ must
# stay importable BEFORE ``tai42_app.bind()`` (tests and tooling import it cold),
# and the ``on_startup`` decorator touches the bound handle at import. The arming
# import lives in the manifest-loaded router modules, which the host only imports
# post-bind — the same reason routes_login's own on_startup hook is safe.
from tai42_accounts_postgres.provider import PostgresAccountsProvider

__all__ = [
    "PostgresAccountsProvider",
]
