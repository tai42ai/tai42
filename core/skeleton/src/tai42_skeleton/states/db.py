"""Component identity, settings, and the migration chain of the platform state store.

The store is a first-class platform component: its own migration chain under the kit
DB registry component ``states`` (env ``TAI_DB_BINDING_STATES``), which — like the
``skeleton`` component — DEFAULTS to the ``default`` database when the binding is
unset, so an ordinary deployment serves the store out of the box (a deployment that
wants records in a separate database points the binding elsewhere). The store is on
whenever its bound database is configured (:func:`states_store_configured`); the boot-time
schema gate (:mod:`tai42_skeleton.db.boot_gate`) then demands the chain on that database.
"""

from __future__ import annotations

import importlib.resources
from importlib.resources.abc import Traversable

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict
from tai42_contract.access_control.identity import ReadinessTarget
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import (
    MigrationEntry,
    component_migrator_settings,
    component_store_configured,
    component_store_settings,
)
from tai42_kit.settings import TaiBaseSettings, settings_cache

# The kit DB component and its identity in ``tai_schema_history``. Fixed once and
# forever — the chain records its rows under this exact name.
STATES_COMPONENT = "states"

# The chain's packaged directory, relative to the ``tai42_skeleton`` import root.
_STATES_MIGRATIONS_SUBPATH = ("states", "sql", "migrations")


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

    # The pending-save outbox: a run's writes and deferred calls are saved before the reply and
    # applied after it. The recovery sweep passes every ``outbox_sweep_seconds``; a deferred
    # call's claim is leased for ``outbox_claim_lease_seconds`` (heartbeat at a third); a
    # transient failure is retried up to ``outbox_max_attempts`` times with an exponential
    # backoff from ``outbox_retry_base_seconds`` capped at ``outbox_retry_cap_seconds``; a
    # reader waits at most ``outbox_drain_timeout_seconds`` for a subject's pending save,
    # polling a running deferred call every ``outbox_drain_poll_seconds``; shutdown awaits
    # in-flight applies for ``outbox_shutdown_grace_seconds``.
    outbox_sweep_seconds: float = Field(default=5.0, gt=0)
    outbox_claim_lease_seconds: float = Field(default=60.0, gt=0)
    outbox_max_attempts: int = Field(default=5, gt=0)
    outbox_retry_base_seconds: float = Field(default=1.0, gt=0)
    outbox_retry_cap_seconds: float = Field(default=60.0, gt=0)
    outbox_drain_poll_seconds: float = Field(default=0.05, gt=0)
    outbox_drain_timeout_seconds: float = Field(default=30.0, gt=0)
    outbox_shutdown_grace_seconds: float = Field(default=20.0, ge=0)

    @model_validator(mode="after")
    def _outbox_pairs(self) -> StatesSettings:
        if self.outbox_drain_poll_seconds >= self.outbox_drain_timeout_seconds:
            raise ValueError("STATES_OUTBOX_DRAIN_POLL_SECONDS must be below STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS")
        if self.outbox_retry_base_seconds > self.outbox_retry_cap_seconds:
            raise ValueError("STATES_OUTBOX_RETRY_BASE_SECONDS must not exceed STATES_OUTBOX_RETRY_CAP_SECONDS")
        return self


@settings_cache
def states_settings() -> StatesSettings:
    """The cached :class:`StatesSettings` for this process, re-read on every settings reset."""
    return StatesSettings()


def states_migrations_dir() -> Traversable:
    """The packaged directory holding the state store's chain SQL files."""
    root = importlib.resources.files("tai42_skeleton")
    return root.joinpath(*_STATES_MIGRATIONS_SUBPATH)


def states_store_configured() -> bool:
    """Whether the ``states`` component's bound database is configured.

    The gate every facet method and route honors (false ⇒ 501 ``states-not-configured``).
    """
    return component_store_configured(STATES_COMPONENT)


def readiness_targets() -> list[ReadinessTarget]:
    """The state store's own bound database, when it is configured.

    A row of its own whichever database the binding names; when that is the database the
    other skeleton stores use, ``/ready`` pings the shared pool once.
    """
    if not states_store_configured():
        return []
    return [ReadinessTarget("states", PostgresClient, component_store_settings(STATES_COMPONENT))]


def states_entry() -> MigrationEntry:
    """The state store's chain as a runner entry against the component's bound MIGRATOR identity.

    The MIGRATOR is the DDL-privileged identity — this is the entry ``tai db migrate`` applies
    (mirrors ``skeleton_entry``).
    """
    return MigrationEntry(
        component=STATES_COMPONENT,
        migrations_dir=states_migrations_dir(),
        settings=component_migrator_settings(STATES_COMPONENT),
    )


__all__ = [
    "STATES_COMPONENT",
    "StatesSettings",
    "readiness_targets",
    "states_entry",
    "states_migrations_dir",
    "states_settings",
    "states_store_configured",
]
