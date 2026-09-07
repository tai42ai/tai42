"""Full-table re-encrypt sweep: rewrite every stored connector token blob under the
current ``CONNECTORS_KEK`` so a KEK rotation converges and the previous key can retire.

Postgres is the durable source of truth, so enumeration runs at the SQL layer, covering
EVERY row — including not-yet-purged expired sessions the serving ``list()`` hides — so
retiring the old key leaves no blob unreadable. A blob still under a previous ring key is
re-encrypted under the current key and written back through the store's compare-and-set
``put(expected_blob=...)``, so a concurrent refresh is never clobbered; a CAS miss re-reads
and retries a bounded number of times (the peer's write is already under the current key,
so the re-read usually converges). A blob no ring key can open is counted and named,
never swallowed; a missing/malformed KEK is a deployment fault for every row and
propagates.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.store.redis_pg import RedisPgConnectorTokenStore
from tai42_skeleton.db import SKELETON_COMPONENT

logger = logging.getLogger(__name__)

# Bounded per-row retries when a compare-and-set loses to a concurrent refresh.
_MAX_CAS_RETRIES = 3


async def reencrypt_connection_tokens() -> dict[str, Any]:
    """Re-encrypt every stored connector token blob under the current KEK.

    Returns a count report ``{scanned, reencrypted, skipped, failed, cas_retries,
    failed_connection_ids}``. Idempotent: a blob already under the current key is
    skipped. A missing/malformed ``CONNECTORS_KEK`` raises for every row, so it
    propagates rather than being logged as a per-blob failure.
    """
    rows = await _enumerate_rows()
    reencrypted = 0
    skipped = 0
    cas_retries = 0
    failed_ids: list[str] = []

    store = RedisPgConnectorTokenStore()
    for connection_id, blob, session_expires_at in rows:
        outcome, retries = await _converge_row(store, connection_id, blob, session_expires_at)
        cas_retries += retries
        if outcome == "reencrypted":
            reencrypted += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed_ids.append(connection_id)

    if failed_ids:
        logger.error(
            "connectors: re-encrypt sweep could not converge %d blob(s): %s",
            len(failed_ids),
            failed_ids,
        )

    return {
        "scanned": len(rows),
        "reencrypted": reencrypted,
        "skipped": skipped,
        "failed": len(failed_ids),
        "failed_connection_ids": failed_ids,
        "cas_retries": cas_retries,
    }


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
            plaintext, under_current = crypto.decrypt_reporting_key(current_blob, connection_id=connection_id)
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
        new_blob = crypto.encrypt(plaintext, connection_id=connection_id)
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
        reread = await _reread_row(connection_id)
        if reread is None:
            # The row was deleted (disconnect) between read and write — nothing to converge.
            return "skipped", retries
        current_blob, current_expiry = reread
    # Contention exhausted the retry budget — surfaced as a failure, never a silent give-up.
    logger.error("connectors: re-encrypt sweep exhausted CAS retries for %r", connection_id)
    return "failed", retries


async def _enumerate_rows() -> list[tuple[str, bytes, datetime | None]]:
    """Every connection row's ``(connection_id, encrypted_blob, session_expires_at)``,
    including expired-but-not-purged sessions, ordered by id (like the backup export)."""
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT connection_id, encrypted_blob, session_expires_at FROM connector_connections ORDER BY connection_id"
        )
        rows = await cur.fetchall()
    return [(str(connection_id), bytes(blob), session_expires_at) for connection_id, blob, session_expires_at in rows]


async def _reread_row(connection_id: str) -> tuple[bytes, datetime | None] | None:
    """Re-read one row's current ciphertext + expiry after a CAS miss, or ``None`` when
    the row no longer exists (a concurrent disconnect deleted it)."""
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT encrypted_blob, session_expires_at FROM connector_connections WHERE connection_id = %s",
            (uuid.UUID(connection_id),),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return bytes(row[0]), row[1]
