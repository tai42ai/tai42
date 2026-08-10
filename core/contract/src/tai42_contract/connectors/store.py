"""Abstract base class for per-connection encrypted-blob persistence.

The store treats the blob as opaque bytes; the AES-GCM boundary lives in the
implementation's crypto layer.

Writes are serialized by a per-connection lock held by the caller
(lock-on-write). A slow write-back (e.g. a refresh that retries past the lock's
TTL) can still race a peer on another replica, so ``put`` additionally offers an
atomic compare-and-set (``expected_blob``) the caller uses to commit only when no
peer has rotated the stored blob meanwhile.

Every method is ``async``; implementations off-load any blocking I/O inside the
method so callers ``await`` directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class ConnectorTokenStore(ABC):
    """Per-connection encrypted-blob persistence.

    Single-namespace: each deployment owns its store, so ``connection_id``
    (a uuid4) is unique on its own and keys every record.
    """

    @abstractmethod
    async def get(self, connection_id: str, *, include_expired: bool = False) -> bytes | None:
        """Return the stored blob, or ``None`` if the record is missing.

        By default a record whose ``session_expires_at`` has passed is treated as
        missing (returns ``None``): token-serving reads and list projections must
        never surface an expired session. ``include_expired=True`` lifts that
        filter and returns the still-stored blob of an expired record — used ONLY
        by the cleanup path (disconnect), so an expired connection remains
        purgeable (revoke upstream + delete blob + strip manifest entries) instead
        of stranding its ciphertext and managed entries forever.

        Implementations MUST raise on every failure that is NOT a clean
        "the record does not exist" — never return ``None`` to mask a
        transient backend error.
        """

    @abstractmethod
    async def put(
        self,
        connection_id: str,
        blob: bytes,
        *,
        create_only: bool = False,
        expected_blob: bytes | None = None,
        session_expires_at: datetime | None = None,
        provider_id: str | None = None,
        alias: str | None = None,
    ) -> bool:
        """Write the blob. Returns ``True`` when the write committed, ``False``
        when an ``expected_blob`` compare-and-set lost (see below).

        ``create_only=True`` requires the record to be new — an existing record
        at this key raises :class:`ConnectorError` (dup-connection guard).

        ``expected_blob`` is an atomic compare-and-set guard. When provided, the
        store commits ONLY if the currently-stored blob equals ``expected_blob``;
        the comparison and the write happen atomically inside the store (no
        re-read-then-write window), so two replicas racing the same record cannot
        both win. On a match the write commits and ``put`` returns ``True``; on a
        mismatch (a peer rotated the stored blob first, or the record is gone) the
        write does NOT happen and ``put`` returns ``False`` so the caller learns
        it lost and can re-read the peer's record instead of clobbering it.
        ``expected_blob`` and ``create_only`` are mutually exclusive — a
        create-or-nothing write and a replace-this-exact-blob write are different
        intents; passing both raises :class:`ValueError`. When ``expected_blob``
        is ``None`` this is a plain upsert that always commits (returns ``True``).

        ``session_expires_at`` is the effective session/refresh-token expiry the
        caller computed (provider expiry capped by ``CONNECTORS_MAX_SESSION_TTL``).
        The store holds opaque ciphertext and cannot read the per-record expiry,
        so the caller passes it in; the redis-pg store uses it as the Redis TTL
        and persists it as a column so a cache miss repopulates with the
        remaining lifetime. ``None`` means no caller-supplied expiry.

        ``provider_id`` and ``alias`` are the connection's non-secret identity,
        persisted alongside the ciphertext to enforce durable per-provider alias
        uniqueness (a ``UNIQUE(provider_id, alias)`` constraint). They are
        required whenever ``expected_blob`` is ``None`` — a ``create_only`` insert
        or any plain upsert, whether it ends up inserting or updating. A
        ``create_only`` insert that collides on the constraint raises
        ``AliasInUseError``. They are not required for an ``expected_blob``
        compare-and-set replace of an existing record, whose identity never
        changes (``None`` is accepted there).
        """

    @abstractmethod
    async def delete(self, connection_id: str) -> None:
        """Hard-delete the stored blob. Idempotent."""

    @abstractmethod
    async def list(self) -> list[str]:
        """Return all ``connection_id`` values currently stored."""
