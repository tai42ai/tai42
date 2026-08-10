"""The versioned-document store package + its construction point.

:func:`versioned_store` builds the active concrete
:class:`~tai42_contract.versioning.VersionedStore`. The contract facet
``tai42_app.versioning.store`` forwards TO this builder, so this function is the
single construction point and must not call the facet back (that would loop).
"""

from __future__ import annotations

from tai42_contract.versioning import VersionedStore

from tai42_skeleton.versioning.store import PostgresVersionedStore


def versioned_store() -> PostgresVersionedStore:
    """Return the active generic versioned-document store.

    Typed as the concrete :class:`PostgresVersionedStore` (not the
    ``VersionedStore`` protocol) so the concrete-only batched
    ``list_active_bodies`` accessor resolves through the ``_versioned_store``
    reference; every protocol-typed surface accepts the concrete subtype."""
    return PostgresVersionedStore()


__all__ = ["PostgresVersionedStore", "VersionedStore", "versioned_store"]
