"""The skeleton's PostgreSQL advisory-lock keys, declared in one place.

Session- and transaction-scoped advisory locks share one key space per database, and
every skeleton store binds the skeleton component's database, so each single-bigint key
here differs from every other one, the kit's migration-runner key included.
``PRESET_LOCK_NAMESPACE`` is the int32 first half of a two-key lock
(:func:`tai42_skeleton.db.locks.advisory_name_lock`), a key space PostgreSQL keeps apart
from the single-bigint one. A plugin declares its own keys.
"""

from __future__ import annotations

from typing import Final

from tai42_kit.db import MIGRATION_LOCK_KEY

# The fleet-wide session lock serializing marketplace install/update/uninstall.
MARKETPLACE_LOCK_KEY: Final = 0x7461695F6D6B7470  # "tai_mktp"

# The transaction lock serializing the first-principal insert and every
# last-admin-guarded principal mutation.
FIRST_PRINCIPAL_LOCK_KEY: Final = 0x4143_5052494E43  # "ACPRINC"

# The namespace every preset create claims its name in (``advisory_name_lock``).
PRESET_LOCK_NAMESPACE: Final = 0x70736574  # "pset"

__all__ = ["FIRST_PRINCIPAL_LOCK_KEY", "MARKETPLACE_LOCK_KEY", "MIGRATION_LOCK_KEY", "PRESET_LOCK_NAMESPACE"]
