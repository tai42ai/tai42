"""Boot-time schema gate — refuse to serve on an out-of-date database.

A schema-owning feature or plugin asserts, once at startup, that its migration
chain is fully applied before the app accepts traffic: a pending migration or a
checksum mismatch is a loud refusal naming ``tai db migrate``, never a
half-migrated serve. The check reads the ``tai_schema_history`` table through the
feature's OWN store connection (the runner grants it ``SELECT`` to ``PUBLIC``), so
a lesser-privileged runtime role can verify chain state.

The refusal itself is the shared kit primitive
:func:`~tai42_kit.db.assert_chain_applied` (so the skeleton and a table-owning
plugin like accounts-postgres, which cannot import ``tai42_skeleton``, use the same
code); this module supplies the skeleton's feature collection and the ``tai db
migrate`` remediation wording. It fires ONLY for a CONFIGURED store: an OFF feature
owns no live schema and must not demand a migrated one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from tai42_kit.clients import PostgresConnectionSettings
from tai42_kit.db import MigrationEntry, SchemaOutOfDateError, assert_chain_applied

from tai42_skeleton.db.discovery import skeleton_entry

logger = logging.getLogger(__name__)

# The skeleton's user-facing fix for a pending/diverged chain. Kit's shared gate
# primitive appends it verbatim; the wording is stable and part of the contract.
_REMEDIATION = "Run 'tai db migrate' to apply the pending migrations, then restart."

# Re-exported so importers of the gate keep a single ``SchemaOutOfDateError`` symbol
# regardless of which module they reach for; the class itself now lives in kit.
__all__ = ["SchemaOutOfDateError", "assert_chain_applied", "assert_skeleton_schema_applied"]


def _skeleton_schema_features() -> list[tuple[Callable[[], bool], Callable[[], PostgresConnectionSettings]]]:
    """The five table-owning skeleton features as (store-configured predicate,
    store-settings factory) pairs.

    Each predicate reads the SAME fresh pydantic-settings its feature gates on, so
    the boot gate tracks the live config; each factory yields the connection that
    feature's store uses, so the chain is verified on the exact database that
    feature will read and write. Built lazily (a function, not a module constant) so
    importing this module pulls in no feature-settings singletons at import time.
    """
    from tai42_skeleton.access_control.store_settings import (
        AccessControlStorePgSettings,
        access_control_store_configured,
    )
    from tai42_skeleton.connectors.settings import ConnectorStorePgSettings, connectors_store_configured
    from tai42_skeleton.marketplace.settings import MarketplaceStorePgSettings, marketplace_store_configured
    from tai42_skeleton.tool_meta.settings import ToolMetaStorePgSettings, tool_meta_store_configured
    from tai42_skeleton.versioning import versioned_store_configured
    from tai42_skeleton.versioning.settings import VersioningStorePgSettings

    return [
        (connectors_store_configured, ConnectorStorePgSettings),
        (versioned_store_configured, VersioningStorePgSettings),
        (tool_meta_store_configured, ToolMetaStorePgSettings),
        (marketplace_store_configured, MarketplaceStorePgSettings),
        (access_control_store_configured, AccessControlStorePgSettings),
    ]


async def assert_skeleton_schema_applied() -> None:
    """Startup gate: assert the ``skeleton`` chain is applied wherever a skeleton
    feature's store is configured.

    Collects the connection of every CONFIGURED table-owning skeleton feature,
    de-duplicates by resolved DSN (all five share one ``skeleton`` chain, and by
    default one database), and verifies the chain on each. A deployment with no
    schema-owning feature configured owns no skeleton tables, so the gate is a
    no-op. Registered ahead of the feature startup handlers so a pending-migration
    database refuses with the ``tai db migrate`` fix rather than failing deeper on a
    missing table.
    """
    features = _skeleton_schema_features()
    entries: list[MigrationEntry] = []
    seen_dsn: set[str] = set()
    for configured, settings_factory in features:
        if not configured():
            continue
        settings = settings_factory()
        dsn = settings.pg_dsn
        if dsn in seen_dsn:
            continue
        seen_dsn.add(dsn)
        entries.append(skeleton_entry(settings))
    if not entries:
        logger.info("schema gate: no schema-owning skeleton feature is configured — skipping the migration check")
        return
    await assert_chain_applied(entries, remediation=_REMEDIATION)
