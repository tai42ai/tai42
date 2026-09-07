"""Component identity, settings, and the boot-time migration gate for the platform
state store.

The store is a first-class platform component: its own migration chain under the kit
DB registry component ``states`` (env ``TAI_DB_BINDING_STATES``), which — like the
``skeleton`` component — DEFAULTS to the ``default`` database when the binding is
unset, so an ordinary deployment serves the store out of the box (a deployment that
wants records in a separate database points the binding elsewhere). The gate keys on
:func:`~tai42_kit.db.component_store_configured` (true whenever the bound database is
configured), so it demands the chain exactly where the store reads and writes.
"""

from __future__ import annotations

import importlib.resources
import logging
from importlib.resources.abc import Traversable

from pydantic_settings import SettingsConfigDict
from tai42_kit.db import (
    MigrationEntry,
    assert_chain_applied,
    component_migrator_settings,
    component_store_configured,
    component_store_settings,
)
from tai42_kit.settings import TaiBaseSettings

logger = logging.getLogger(__name__)

# The kit DB component and its identity in ``tai_schema_history``. Fixed once and
# forever — the chain records its rows under this exact name, so the gate reads them.
STATES_COMPONENT = "states"

# The chain's packaged directory, relative to the ``tai42_skeleton`` import root.
_STATES_MIGRATIONS_SUBPATH = ("states", "sql", "migrations")

# The user-facing fix for a pending/diverged chain, appended verbatim by kit's shared
# gate primitive.
_REMEDIATION = "Run 'tai db migrate' to apply the pending migrations, then restart."


class StatesSettings(TaiBaseSettings):
    """The state store's own settings group (env prefix ``STATES_``)."""

    model_config = SettingsConfigDict(env_prefix="STATES_")

    # Op-ledger retention: rows older than this are pruned opportunistically on the
    # write that inserts new ones, bounding the idempotency ledger.
    op_retention_days: int = 30

    # Default RECORD retention window in days: a record whose ``updated_at`` is older
    # than this is eligible for the explicit prune sweep. Unset (``None``) — the safe,
    # opt-in default — keeps records forever; a per-state ``retention_days`` on a
    # declaration overrides it. Nothing is ever deleted until a retention is configured
    # here or on the state, so bounding user memory is always a deliberate choice.
    default_retention_days: int | None = None


def states_settings() -> StatesSettings:
    """Load the state-store settings FRESH (never cached) so a config reload re-evaluates."""
    return StatesSettings()


def states_migrations_dir() -> Traversable:
    """The packaged directory holding the state store's chain SQL files."""
    root = importlib.resources.files("tai42_skeleton")
    return root.joinpath(*_STATES_MIGRATIONS_SUBPATH)


def states_store_configured() -> bool:
    """Whether the ``states`` component's bound database is configured — the gate every
    facet method and route honors (false ⇒ 501 ``states-not-configured``)."""
    return component_store_configured(STATES_COMPONENT)


def states_entry() -> MigrationEntry:
    """The state store's chain as a runner entry against the component's bound MIGRATOR
    (DDL-privileged) identity — the entry ``tai db migrate`` applies (mirrors
    ``skeleton_entry``)."""
    return MigrationEntry(
        component=STATES_COMPONENT,
        migrations_dir=states_migrations_dir(),
        settings=component_migrator_settings(STATES_COMPONENT),
    )


async def assert_states_schema_applied() -> None:
    """Boot gate: assert the ``states`` chain is applied when the component's database is
    configured.

    Verifies the chain on the store's RUNTIME connection (``component_store_settings`` —
    the exact database the store reads and writes, with SELECT on ``tai_schema_history``),
    not the migrator identity. A deployment with no configured database for the component
    owns no state tables, so the gate is a no-op; otherwise a pending/diverged chain
    refuses loudly naming ``tai db migrate``, never a runtime relation-missing surprise on
    the first write."""
    if not states_store_configured():
        logger.info("states schema gate: the states database is not configured — skipping the migration check")
        return
    entry = MigrationEntry(
        component=STATES_COMPONENT,
        migrations_dir=states_migrations_dir(),
        settings=component_store_settings(STATES_COMPONENT),
    )
    await assert_chain_applied([entry], remediation=_REMEDIATION)


__all__ = [
    "STATES_COMPONENT",
    "StatesSettings",
    "assert_states_schema_applied",
    "states_entry",
    "states_migrations_dir",
    "states_settings",
    "states_store_configured",
]
