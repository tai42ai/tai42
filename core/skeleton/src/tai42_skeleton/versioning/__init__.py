"""The versioned-document store package + its construction point.

:func:`versioned_store` builds the active concrete
:class:`~tai42_contract.versioning.VersionedStore`. The contract facet
``tai42_app.versioning.store`` forwards TO this builder, so this function is the
single construction point and must not call the facet back (that would loop).
"""

from __future__ import annotations

from tai42_contract.access_control.identity import ReadinessTarget
from tai42_contract.versioning import VersionedStore
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_configured, component_store_settings

from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.versioning.store import PostgresVersionedStore


def versioned_store() -> PostgresVersionedStore:
    """Return the active generic versioned-document store.

    Typed as the concrete :class:`PostgresVersionedStore` (not the
    ``VersionedStore`` protocol) so the concrete-only batched
    ``list_active_bodies`` accessor resolves through the ``_versioned_store``
    reference; every protocol-typed surface accepts the concrete subtype.
    """
    return PostgresVersionedStore()


def readiness_targets() -> list[ReadinessTarget]:
    """The versioned-document store's database, when it is configured."""
    if not component_store_configured(SKELETON_COMPONENT):
        return []
    return [ReadinessTarget("versioning", PostgresClient, component_store_settings(SKELETON_COMPONENT))]


__all__ = ["PostgresVersionedStore", "VersionedStore", "readiness_targets", "versioned_store"]
