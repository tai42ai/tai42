"""Database schema tooling shared across the TAI ecosystem.

The Flyway-model migration runner (:mod:`tai42_kit.db.migrations`) is the single
way schema chains are applied and inspected: plain-SQL files ordered by a
zero-padded numeric prefix, one transaction per file, recorded in a per-database
``tai_schema_history`` table under a session-scoped advisory lock. It reuses the
pooled :class:`~tai42_kit.clients.impl.postgres.PostgresClient` and is async like
the rest of kit; using it requires the ``postgres`` extra.

:func:`~tai42_kit.db.assert_chain_applied` is the shared, CLI-agnostic boot-gate
primitive every schema-owning component reuses: it turns the non-raising
:func:`~tai42_kit.db.migration_status` verdict into a loud
:class:`~tai42_kit.db.SchemaOutOfDateError` with a caller-supplied remediation.
"""

from tai42_kit.db.migrations import (
    AppliedMigration,
    ChecksumMismatch,
    ChecksumMismatchError,
    ComponentStatus,
    MigrationDiscoveryError,
    MigrationEntry,
    MigrationError,
    MigrationScript,
    SchemaOutOfDateError,
    apply_migrations,
    assert_chain_applied,
    discover_migrations,
    migration_status,
)

__all__ = [
    "AppliedMigration",
    "ChecksumMismatch",
    "ChecksumMismatchError",
    "ComponentStatus",
    "MigrationDiscoveryError",
    "MigrationEntry",
    "MigrationError",
    "MigrationScript",
    "SchemaOutOfDateError",
    "apply_migrations",
    "assert_chain_applied",
    "discover_migrations",
    "migration_status",
]
