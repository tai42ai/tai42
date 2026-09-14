"""The encrypted compare-and-set write shared by every connection read-modify-write,
and the lost-race error a CAS miss raises."""

from __future__ import annotations

from tai42_contract.connectors.errors import ConnectorError
from tai42_contract.connectors.models import ConnectionRecord

from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.service import connection_service as _svc
from tai42_skeleton.connectors.store.persistence import session_expires_at_for


class ConcurrentConnectionUpdateError(ConnectorError):
    """A read-modify-write lost its compare-and-set: a concurrent writer
    rotated the stored record between this operation's load and its persist.
    Nothing was written — the caller should re-read the connection and retry
    the operation against its current state."""

    def __init__(self, connection_id: str):
        super().__init__(
            f"connection {connection_id} was modified concurrently; "
            f"nothing was written — re-read the connection and retry"
        )
        self.connection_id = connection_id


async def _persist(
    record: ConnectionRecord,
    *,
    create_only: bool = False,
    expected_blob: bytes | None = None,
) -> None:
    """Encrypt the record and write it through the token store.

    ``expected_blob`` is the ciphertext the read-modify-write loaded from; the
    store commits only if the stored blob still equals it (atomic
    compare-and-set). A CAS miss means a concurrent writer rotated the record —
    possibly including a rotated refresh token — so overwriting would corrupt
    it; raise :class:`ConcurrentConnectionUpdateError` instead.
    """
    blob = crypto.encrypt(
        record.to_storage_json().encode("utf-8"),
        connection_id=record.connection_id,
    )
    # provider_id/alias back the durable UNIQUE (provider_id, alias) constraint;
    # a create-only insert that collides raises AliasInUseError from the store (the
    # authority for per-provider alias uniqueness).
    committed = await _svc.token_store().put(
        record.connection_id,
        blob,
        create_only=create_only,
        expected_blob=expected_blob,
        session_expires_at=session_expires_at_for(record),
        provider_id=record.provider_id,
        alias=record.alias,
    )
    if expected_blob is not None and not committed:
        raise ConcurrentConnectionUpdateError(record.connection_id)
