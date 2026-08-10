"""Boot-time schema gate — refuse to serve on an out-of-date database.

The skeleton asserts, once at startup, that its migration chain is fully applied
before the app accepts traffic: a pending migration or a checksum mismatch is a
loud refusal naming ``tai db migrate``, never a half-migrated serve. The check
reads the ``tai_schema_history`` table through the skeleton's OWN store connection
(the runner grants it ``SELECT`` to ``PUBLIC``), so a lesser-privileged runtime
role can verify chain state.

The refusal itself is the shared kit primitive
:func:`~tai42_kit.db.assert_chain_applied` (so the skeleton and a table-owning
plugin like accounts-postgres, which cannot import ``tai42_skeleton``, use the same
code); this module supplies the ``tai db migrate`` remediation wording. It fires
ONLY when the skeleton database is configured: an all-off deployment owns no live
skeleton schema and must not demand a migrated one.
"""

from __future__ import annotations

import logging

from tai42_kit.db import (
    MigrationEntry,
    SchemaOutOfDateError,
    assert_chain_applied,
    component_store_configured,
    component_store_settings,
)

from tai42_skeleton.db.discovery import SKELETON_COMPONENT, skeleton_migrations_dir

logger = logging.getLogger(__name__)

# The skeleton's user-facing fix for a pending/diverged chain. Kit's shared gate
# primitive appends it verbatim; the wording is stable and part of the contract.
_REMEDIATION = "Run 'tai db migrate' to apply the pending migrations, then restart."

# Re-exported so importers of the gate keep a single ``SchemaOutOfDateError`` symbol
# regardless of which module they reach for; the class itself now lives in kit.
__all__ = ["SchemaOutOfDateError", "assert_chain_applied", "assert_skeleton_schema_applied"]


async def assert_skeleton_schema_applied() -> None:
    """Startup gate: assert the ``skeleton`` chain is applied when the skeleton
    database is configured.

    Verifies the chain on the skeleton's runtime store connection — the exact
    database its features read and write. A deployment with no skeleton database
    configured owns no skeleton tables, so the gate is a no-op. Registered ahead of
    the feature startup handlers so a pending-migration database refuses with the
    ``tai db migrate`` fix rather than failing deeper on a missing table.
    """
    if not component_store_configured(SKELETON_COMPONENT):
        logger.info("schema gate: the skeleton database is not configured — skipping the migration check")
        return
    entry = MigrationEntry(
        component=SKELETON_COMPONENT,
        migrations_dir=skeleton_migrations_dir(),
        settings=component_store_settings(SKELETON_COMPONENT),
    )
    await assert_chain_applied([entry], remediation=_REMEDIATION)
