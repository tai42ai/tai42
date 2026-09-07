"""Skeleton-side wiring for the kit migration framework.

The kit owns the runner (:mod:`tai42_kit.db`) and the central database registry;
this package owns the skeleton's integration: chain discovery for the skeleton and
installed plugins (:mod:`.discovery`), the boot-time schema gate every schema-owning
feature and plugin shares (:mod:`.boot_gate`), and the fleet-wide advisory lock a
read-then-write takes to stay atomic across processes (:mod:`.locks`).
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
from tai42_skeleton.db.locks import advisory_name_lock
from tai42_skeleton.db.not_configured import not_configured_message

__all__ = [
    "SKELETON_COMPONENT",
    "SchemaOutOfDateError",
    "advisory_name_lock",
    "all_migration_entries",
    "assert_chain_applied",
    "assert_skeleton_schema_applied",
    "installed_plugin_entries",
    "not_configured_message",
    "plugin_migration_entry",
    "skeleton_entry",
    "skeleton_migrations_dir",
]
