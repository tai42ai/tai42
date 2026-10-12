"""Full-table re-encrypt sweep: rewrite every sealed connector blob under the current KEK.

Rewriting under the current ``CONNECTORS_KEK`` lets a KEK rotation converge so the previous
key can retire. Two tables hold sealed blobs and both are swept, each through its own store:
the connection token records and the stored OAuth client credentials.

Enumeration reads each store's full durable listing, covering EVERY row — including
not-yet-purged expired sessions the serving ``list()`` hides, and dead or expired client rows —
so retiring the old key leaves no blob unreadable. A blob still under a previous ring key is
re-encrypted under the current key and written back through the store's compare-and-set, so a
concurrent writer is never clobbered; a CAS miss re-reads and retries a bounded number of times
(the peer's write is already under the current key, so the re-read usually converges). A blob
no ring key can open is counted and named, never swallowed; a missing/malformed KEK is a
deployment fault for every row and propagates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidTag

from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.store import client_store
from tai42_skeleton.connectors.store.client_store import ConnectorClientStore, client_aad
from tai42_skeleton.connectors.store.redis_pg import RedisPgConnectorTokenStore

logger = logging.getLogger(__name__)

# Bounded per-row retries when a compare-and-set loses to a concurrent refresh.
_MAX_CAS_RETRIES = 3


async def reencrypt_connection_tokens() -> dict[str, Any]:
    """Re-encrypt every stored connector token blob and client credential blob under the current KEK.

    Returns a count report ``{scanned, reencrypted, skipped, failed, failed_connection_ids,
    clients_scanned, clients_reencrypted, clients_skipped, clients_failed,
    failed_client_row_ids, cas_retries}`` (``cas_retries`` counts both tables). Idempotent: a
    blob already under the current key is skipped. A missing/malformed ``CONNECTORS_KEK``
    raises for every row, so it propagates rather than being logged as a per-blob failure.
    """
    store = RedisPgConnectorTokenStore()
    rows = await store.list_all_including_expired()
    reencrypted = 0
    skipped = 0
    cas_retries = 0
    failed_ids: list[str] = []

    for row in rows:
        outcome, retries = await _converge_row(store, row.connection_id, row.encrypted_blob, row.session_expires_at)
        cas_retries += retries
        if outcome == "reencrypted":
            reencrypted += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed_ids.append(row.connection_id)

    if failed_ids:
        logger.error(
            "connectors: re-encrypt sweep could not converge %d blob(s): %s",
            len(failed_ids),
            failed_ids,
        )

    clients = await _reencrypt_client_rows(client_store.active_client_store())
    return {
        "scanned": len(rows),
        "reencrypted": reencrypted,
        "skipped": skipped,
        "failed": len(failed_ids),
        "failed_connection_ids": failed_ids,
        "clients_scanned": clients.scanned,
        "clients_reencrypted": clients.reencrypted,
        "clients_skipped": clients.skipped,
        "clients_failed": len(clients.failed_ids),
        "failed_client_row_ids": clients.failed_ids,
        "cas_retries": cas_retries + clients.cas_retries,
    }


@dataclass
class _ClientSweep:
    scanned: int = 0
    reencrypted: int = 0
    skipped: int = 0
    cas_retries: int = 0
    failed_ids: list[str] = field(default_factory=list)


async def _reencrypt_client_rows(store: ConnectorClientStore) -> _ClientSweep:
    """Bring every stored client row under the current key through the client store's own methods."""
    sweep = _ClientSweep()
    for row in await store.enumerate_rows():
        sweep.scanned += 1
        outcome, retries = await _converge_client_row(store, row.client_row_id, row.encrypted_blob)
        sweep.cas_retries += retries
        if outcome == "reencrypted":
            sweep.reencrypted += 1
        elif outcome == "skipped":
            sweep.skipped += 1
        else:
            sweep.failed_ids.append(row.client_row_id)
    if sweep.failed_ids:
        logger.error(
            "connectors: re-encrypt sweep could not converge %d client row(s): %s",
            len(sweep.failed_ids),
            sweep.failed_ids,
        )
    return sweep


async def _converge_client_row(store: ConnectorClientStore, client_row_id: str, blob: bytes) -> tuple[str, int]:
    """Bring one client row under the current key via a bounded compare-and-set loop.

    Returns ``(outcome, cas_retries)`` with ``outcome`` in ``{"reencrypted", "skipped", "failed"}``.
    A missing/malformed KEK propagates.
    """
    aad = client_aad(client_row_id)
    current_blob = blob
    retries = 0
    for _ in range(_MAX_CAS_RETRIES + 1):
        try:
            plaintext, under_current = crypto.decrypt_reporting_key(current_blob, aad=aad)
        except crypto.ConnectorEncryptionConfigError:
            raise
        except (InvalidTag, ValueError, TypeError):
            logger.warning("connectors: re-encrypt sweep cannot decrypt client row %r", client_row_id, exc_info=True)
            return "failed", retries
        if under_current:
            return "skipped", retries
        new_blob = crypto.encrypt(plaintext, aad=aad)
        if await store.rewrite_blob(client_row_id, new_blob, expected_blob=current_blob):
            return "reencrypted", retries
        retries += 1
        reread = await store.get_row(client_row_id)
        if reread is None:
            # The row was deleted between read and write — nothing to converge.
            return "skipped", retries
        current_blob = reread.encrypted_blob
    logger.error("connectors: re-encrypt sweep exhausted CAS retries for client row %r", client_row_id)
    return "failed", retries


async def _converge_row(
    store: RedisPgConnectorTokenStore,
    connection_id: str,
    blob: bytes,
    session_expires_at: datetime | None,
) -> tuple[str, int]:
    """Bring one row under the current key via a bounded compare-and-set loop.

    Returns ``(outcome, cas_retries)`` with ``outcome`` in
    ``{"reencrypted", "skipped", "failed"}``. A missing/malformed KEK propagates.
    """
    current_blob = blob
    current_expiry = session_expires_at
    retries = 0
    for _ in range(_MAX_CAS_RETRIES + 1):
        try:
            plaintext, under_current = crypto.decrypt_reporting_key(current_blob, aad=connection_id)
        except crypto.ConnectorEncryptionConfigError:
            # Missing/malformed KEK is not a per-blob fault — it fails every row. Abort loudly.
            raise
        except (InvalidTag, ValueError, TypeError):
            # No ring key opens this blob (or it is malformed): a true per-row failure,
            # surfaced in the report, never swallowed.
            logger.warning("connectors: re-encrypt sweep cannot decrypt %r", connection_id, exc_info=True)
            return "failed", retries
        if under_current:
            # Already under the current key — nothing to rewrite.
            return "skipped", retries
        new_blob = crypto.encrypt(plaintext, aad=connection_id)
        committed = await store.put(
            connection_id,
            new_blob,
            expected_blob=current_blob,
            session_expires_at=current_expiry,
        )
        if committed:
            return "reencrypted", retries
        # CAS miss: a concurrent refresh (or a peer sweep) rotated the blob first. Re-read
        # and retry — the peer's write is under the current key, so the re-read converges.
        retries += 1
        reread = await store.reread(connection_id)
        if reread is None:
            # The row was deleted (disconnect) between read and write — nothing to converge.
            return "skipped", retries
        current_blob, current_expiry = reread.encrypted_blob, reread.session_expires_at
    # Contention exhausted the retry budget — surfaced as a failure, never a silent give-up.
    logger.error("connectors: re-encrypt sweep exhausted CAS retries for %r", connection_id)
    return "failed", retries
