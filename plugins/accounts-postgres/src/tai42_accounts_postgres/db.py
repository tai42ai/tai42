"""Schema identity and boot-time migration gate for the plugin's own chain.

The plugin owns a migration chain — ``migrations/0001_baseline.sql`` and any
later files — applied by the shared runner (``tai db migrate``), recorded in the
per-database ``tai_schema_history`` table under the component name
``tai42-accounts-postgres`` (its distribution name, the same identity the
marketplace installer and the ``tai db`` CLI stamp its rows with).

At startup the provider asserts, once, that this chain is fully applied before
serving — a pending migration or a checksum mismatch is a loud refusal naming
``tai db migrate``, never a half-migrated serve. The gate reads the history table
through the plugin's OWN store connection (the runner grants ``SELECT`` on it to
``PUBLIC``) and fires ONLY when the store is configured: an unconfigured accounts
store owns no live schema and must not demand a migrated one.

The check is built on the shared kit primitive
:func:`~tai42_kit.db.assert_chain_applied` rather than the skeleton's boot-gate
module: this plugin is contract-facing and never imports ``tai42_skeleton``, and
kit is on its allowed import path.
"""

from __future__ import annotations

import importlib.resources
import logging

from tai42_kit.db import MigrationEntry, SchemaOutOfDateError, assert_chain_applied

from tai42_accounts_postgres.settings import AccountsPgSettings, accounts_settings

logger = logging.getLogger(__name__)

# The chain's identity in ``tai_schema_history`` — the plugin's distribution name
# (``spec.package``), so the boot gate reads the exact rows the installer and CLI
# write. Fixed once and forever; it never changes across releases.
COMPONENT = "tai42-accounts-postgres"

# The packaged directory holding the chain, resolved through the plugin's import
# root — the same ``migrations`` directory declared in ``tai-plugin.yml``.
_MIGRATIONS_SUBPATH = "migrations"

# The plugin's user-facing fix for a pending/diverged chain, appended verbatim by
# kit's shared gate primitive. Kept byte-identical to the skeleton's wording.
_REMEDIATION = "Run 'tai db migrate' to apply the pending migrations, then restart."

# Re-exported: ``SchemaOutOfDateError`` now lives in kit, but the plugin's provider
# and tests import it from here as the accounts gate's error identity.
__all__ = [
    "COMPONENT",
    "SchemaOutOfDateError",
    "accounts_migration_entry",
    "accounts_store_configured",
    "assert_accounts_schema_applied",
]


def accounts_store_configured() -> bool:
    """Whether this deployment configures the accounts store's Postgres at all.

    Resolved through the SAME pydantic-settings the store connects with (its own
    ``TAI_ACCOUNTS_PG_*`` env or the shared ``TAI_DEFAULT_PG_PASSWORD``), read
    fresh — not the cached singleton — so a config reload re-evaluates. The store
    carries no baked-in credential, so a supplied password is the signal a real
    store is wired up; a bare env-var read would miss the ``TAI_DEFAULT_*``
    fallback.
    """
    settings = AccountsPgSettings()
    return bool(settings.pg_password and settings.pg_password.get_secret_value())


def accounts_migration_entry() -> MigrationEntry:
    """The plugin's chain as a runner entry against its own store connection."""
    migrations_dir = importlib.resources.files("tai42_accounts_postgres").joinpath(_MIGRATIONS_SUBPATH)
    return MigrationEntry(component=COMPONENT, migrations_dir=migrations_dir, settings=accounts_settings().pg)


async def assert_accounts_schema_applied() -> None:
    """Startup gate: assert the accounts chain is applied when the store is
    configured.

    A deployment that does not configure the accounts store (no resolved password)
    owns no accounts tables, so the gate is a no-op. Otherwise the chain is
    verified on the exact database the store reads and writes; a pending migration
    or a checksum mismatch refuses with the ``tai db migrate`` fix.
    """
    if not accounts_store_configured():
        logger.info("schema gate: the accounts store is not configured — skipping the migration check")
        return
    await assert_chain_applied([accounts_migration_entry()], remediation=_REMEDIATION)
