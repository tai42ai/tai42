"""The generic versioned-document store contract.

A ``kind``-discriminated, body-opaque persistence primitive: append-only version
rows over an opaque JSONB ``body``, an active-version pointer, and rollback. The
store knows NOTHING about presets/policies/agents — those are typed VIEWS layered
on top. Identity is ``(kind, name)`` throughout; every write is one transaction.

This package holds the :class:`VersionedStore` Protocol plus its record models
(:class:`DocumentRecord`, :class:`DocumentVersion`) and typed errors. It is
reached from the assembled facade as ``app.versioning.store`` (see
:class:`~tai42_contract.app.facets.AppVersioning`).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from tai42_contract.versioning.errors import (
    DocumentExistsError,
    DocumentNotFoundError,
    DocumentStoreError,
    DocumentVersionNotFoundError,
)
from tai42_contract.versioning.models import DocumentRecord, DocumentVersion


@runtime_checkable
class VersionedStoreTransaction(Protocol):
    """Opaque unit-of-work handle yielded by :meth:`VersionedStore.transaction`.

    A pure durability token exposing NO operations of its own: a caller only passes it
    back to a write method's ``tx=`` parameter so that write joins the open
    transaction. The concrete store owns the handle's real shape (its pooled
    connection); the token carries no audit or domain awareness."""


@runtime_checkable
class VersionedStore(Protocol):
    """Kind-discriminated, body-opaque version store. Identity is ``(kind, name)``.

    The ``body`` is opaque ``dict``/JSONB — the store never inspects it; the caller
    (a typed view) owns its shape. Every method raises loudly on a missing,
    duplicate, or invalid target — never a silent default. ``tags`` is the generic
    per-version grouping label (see :class:`DocumentVersion`), distinct from any
    categorization a ``kind``'s body may carry.

    A write method (:meth:`create`, :meth:`save_version`, :meth:`delete`,
    :meth:`rollback`) accepts an optional ``tx`` — the handle from an open
    :meth:`transaction`. Passed, the write runs WITHIN that unit of work and commits
    or rolls back with it; omitted, the write is self-contained and atomic on its own.
    """

    def transaction(self) -> AbstractAsyncContextManager[VersionedStoreTransaction]:
        """Open a single-backend unit of work: ``async with store.transaction() as tx``.

        Every store write performed with the yielded ``tx`` (passed as ``tx=`` to the
        write methods) commits or rolls back TOGETHER — a clean exit commits, an
        exception rolls the whole scope back. A pure durability primitive over the one
        backend: it carries no audit or domain awareness, and yields an opaque handle
        the caller only threads back into the write methods."""
        ...

    async def create(
        self,
        kind: str,
        name: str,
        body: dict[str, Any],
        tags: list[str] | None = None,
        *,
        tx: VersionedStoreTransaction | None = None,
    ) -> DocumentRecord:
        """Insert a new document at ``active_version = 1`` plus its version 1, one
        transaction. Raise :class:`DocumentExistsError` if ``(kind, name)`` is
        already a live document. Runs within ``tx`` when one is supplied."""
        ...

    async def save_version(
        self,
        kind: str,
        name: str,
        body: dict[str, Any],
        tags: list[str] | None = None,
        *,
        tx: VersionedStoreTransaction | None = None,
    ) -> DocumentVersion:
        """Append a new version (``new_version = max(version) + 1``, NOT
        ``active_version + 1``) and bump the active pointer to it, one transaction
        (a partial failure rolls the whole save back — no orphan version, no
        half-bumped pointer). Raise :class:`DocumentNotFoundError` if absent. Runs
        within ``tx`` when one is supplied."""
        ...

    async def list(self, kind: str) -> list[DocumentRecord]:
        """List the active (non-soft-deleted) documents of ``kind``."""
        ...

    async def get(self, kind: str, name: str) -> DocumentRecord:
        """Fetch the active record for ``(kind, name)``. Raise
        :class:`DocumentNotFoundError` if absent."""
        ...

    async def get_active_body(
        self,
        kind: str,
        name: str,
        *,
        tx: VersionedStoreTransaction | None = None,
        for_update: bool = False,
    ) -> dict[str, Any]:
        """Return the ``body`` of the active version for ``(kind, name)``. Raise
        :class:`DocumentNotFoundError` if absent.

        Passed a ``tx``, the read rides that transaction's connection so a
        read-modify-write can lock and mutate the row on one connection (no second pooled
        connection while the transaction is open). With ``for_update=True`` it takes a
        ``SELECT ... FOR UPDATE`` row lock on the active document so concurrent writers
        serialize — the lock is meaningful only inside a transaction, so ``for_update``
        REQUIRES a ``tx`` and raises loudly without one. Omitting both preserves the
        stand-alone read."""
        ...

    async def list_versions(self, kind: str, name: str) -> list[DocumentVersion]:
        """List every version of ``(kind, name)``, each carrying the
        :attr:`DocumentVersion.is_current` signal derived from ``active_version``.
        Raise :class:`DocumentNotFoundError` if the document is absent."""
        ...

    async def get_version(
        self, kind: str, name: str, version: int, *, tx: VersionedStoreTransaction | None = None
    ) -> DocumentVersion:
        """Fetch one version. Raise :class:`DocumentVersionNotFoundError` if that
        version does not exist. Passed a ``tx``, the read rides that transaction's
        connection rather than opening a second pooled one."""
        ...

    async def rollback(
        self, kind: str, name: str, version: int, *, tx: VersionedStoreTransaction | None = None
    ) -> DocumentRecord:
        """Re-point ``active_version`` to ``version`` (NO data copy). Raise
        :class:`DocumentVersionNotFoundError` if that version does not exist. Runs
        within ``tx`` when one is supplied."""
        ...

    async def soft_delete(self, kind: str, name: str) -> None:
        """Set ``is_active = False``, keeping the version history (audit). The
        document drops out of ``list``/``get`` but its rows survive."""
        ...

    async def delete(self, kind: str, name: str, *, tx: VersionedStoreTransaction | None = None) -> None:
        """HARD delete, DISTINCT from :meth:`soft_delete`: remove ONLY the ACTIVE
        document row for ``(kind, name)`` AND its version rows, so nothing of the
        active document survives; a soft-deleted ghost of the same name is left
        untouched. Raise :class:`DocumentNotFoundError` if there is no active row.
        Used to roll a never-succeeded create fully back and to remove a conflicted
        record without leaving the ghost + spurious history a soft delete would. Runs
        within ``tx`` when one is supplied."""
        ...

    async def rename(self, kind: str, name: str, new_name: str) -> DocumentRecord:
        """Re-key the ACTIVE document ``(kind, name)`` to ``(kind, new_name)`` in one
        atomic write. The whole version history, every per-version ``tags`` label, and
        the ``active_version`` pointer move untouched — versions key on the document,
        not the name, so nothing is copied. The ``body`` is never inspected (the store
        stays body-opaque). A soft-deleted ghost named ``new_name`` does NOT block (the
        same partial-unique identity rule :meth:`create` follows), and a soft-deleted
        ghost of ``name`` is left untouched as audit history. Raise
        :class:`DocumentNotFoundError` if ``(kind, name)`` has no active row; raise
        :class:`DocumentExistsError` (carrying ``new_name``) if ``(kind, new_name)`` is
        already a live document."""
        ...


__all__ = [
    "DocumentExistsError",
    "DocumentNotFoundError",
    "DocumentRecord",
    "DocumentStoreError",
    "DocumentVersion",
    "DocumentVersionNotFoundError",
    "VersionedStore",
    "VersionedStoreTransaction",
]
