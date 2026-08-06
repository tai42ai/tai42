"""Skeleton-side wiring for the kit migration framework.

The kit owns the runner (:mod:`tai42_kit.db`); this package owns the skeleton's
integration: the DDL-privileged migrator identity (:mod:`.settings`), chain
discovery for the skeleton and installed plugins (:mod:`.discovery`), and the
boot-time schema gate every schema-owning feature and plugin shares
(:mod:`.boot_gate`).
"""

from __future__ import annotations

from tai42_skeleton.db.boot_gate import (
    SchemaOutOfDateError,
    assert_chain_applied,
    assert_skeleton_schema_applied,
)
from tai42_skeleton.db.discovery import (
    SKELETON_COMPONENT,
    all_migration_entries,
    installed_plugin_entries,
    plugin_migration_entry,
    skeleton_entry,
    skeleton_migrations_dir,
)
from tai42_skeleton.db.settings import SchemaAdminSettings, schema_settings

__all__ = [
    "SKELETON_COMPONENT",
    "SchemaAdminSettings",
    "SchemaOutOfDateError",
    "all_migration_entries",
    "assert_chain_applied",
    "assert_skeleton_schema_applied",
    "installed_plugin_entries",
    "plugin_migration_entry",
    "schema_settings",
    "skeleton_entry",
    "skeleton_migrations_dir",
]
