"""Store-level backup export/import for the connector Postgres tables.

Postgres is the durable source of truth, so these helpers operate at the SQL layer
directly — NOT the network-gated service layer; no provider re-probe, no OAuth
replay. Two independent section pairs cover the two halves of connector state:

  * categories — the public, secret-free ``connector_category`` grouping rows.
  * connections — the per-connection token records, each carrying its AES-GCM
    ciphertext verbatim (base64, NEVER decrypted).

KEK constraint: a restore is usable only under the SAME ``CONNECTORS_KEK``; under a
different KEK the ciphertext is intact but undecryptable and the load path fails loudly
on first use — never a working-looking-but-dead token.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Any, Literal

from psycopg.errors import UniqueViolation
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.connectors.settings import connector_store_settings
from tai42_skeleton.connectors.store.redis_pg import _ALIAS_UNIQUE_CONSTRAINT, RedisPgConnectorTokenStore
from tai42_skeleton.db import SKELETON_COMPONENT

# The backup section report shape, built here rather than imported across the seam.
_SectionReport = dict[str, Any]


def _empty_report() -> _SectionReport:
    return {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}


# -- connector_category (public, secret-free) --------------------------------


async def export_connector_categories() -> dict[str, Any]:
    """Export the ``connector_category`` grouping rows as a faithful row copy;
    ``created_at`` is carried so the original creation time survives a round-trip."""
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT id, display_name, sort_order, created_at FROM connector_category ORDER BY sort_order, id"
        )
        categories = [
            {
                "id": category_id,
                "display_name": display_name,
                "sort_order": sort_order,
                "created_at": created_at.isoformat(),
            }
            for category_id, display_name, sort_order, created_at in await cur.fetchall()
        ]
    return {"categories": categories}


async def import_connector_categories(
    payload: dict[str, Any], mode: Literal["skip", "overwrite"] = "skip"
) -> _SectionReport:
    """Restore the ``connector_category`` grouping rows, keyed by ``id``.

    Under ``overwrite`` each row is an ``ON CONFLICT (id) DO UPDATE``; under ``skip`` an
    already-present id is left untouched. ``created_at`` is written on INSERT and immutable,
    so the ``DO UPDATE`` branch leaves the existing value untouched.
    """
    report = _empty_report()
    categories = payload.get("categories") or []

    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        # Current keys drive the created-vs-updated report counts only, not the write.
        await cur.execute("SELECT id FROM connector_category")
        existing_categories = {row[0] for row in await cur.fetchall()}

        for category in categories:
            if category["id"] in existing_categories and mode == "skip":
                report["skipped_existing"] += 1
                continue
            await cur.execute(
                "INSERT INTO connector_category (id, display_name, sort_order, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE "
                "SET display_name = EXCLUDED.display_name, sort_order = EXCLUDED.sort_order",
                (
                    category["id"],
                    category["display_name"],
                    category["sort_order"],
                    datetime.fromisoformat(category["created_at"]),
                ),
            )
            _count(report, category["id"] in existing_categories)

    return report


# -- connector_connections (encrypted token records, secret) -----------------


async def export_connector_connections() -> list[dict[str, Any]]:
    """Export every connection record, ``encrypted_blob`` base64-encoded AS-IS (never
    decrypted, so the KEK boundary is never crossed). ``cache_version`` and timestamps are
    store-regenerated on restore and omitted.
    """
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT connection_id, provider_id, alias, encrypted_blob, session_expires_at "
            "FROM connector_connections ORDER BY connection_id"
        )
        rows = await cur.fetchall()
    return [
        {
            "connection_id": str(connection_id),
            "provider_id": provider_id,
            "alias": alias,
            "session_expires_at": None if session_expires_at is None else session_expires_at.isoformat(),
            "encrypted_blob_b64": base64.b64encode(bytes(encrypted_blob)).decode("ascii"),
        }
        for connection_id, provider_id, alias, encrypted_blob, session_expires_at in rows
    ]


async def import_connector_connections(
    payload: list[dict[str, Any]], mode: Literal["skip", "overwrite"] = "skip"
) -> _SectionReport:
    """Re-insert each connection's ciphertext under its original connection id.

    Keyed by ``connection_id``: ``skip`` leaves an already-present connection untouched,
    ``overwrite`` upserts and drops the stale cache key. Each row runs in its own savepoint
    so a per-provider alias collision (the durable ``UNIQUE (provider_id, alias)``) isolates
    to a per-row error while the rest restore; any other DB error aborts the section.
    Restored cache keys are invalidated after commit — see :func:`_invalidate_connection_cache`.
    """
    report = _empty_report()
    restored_ids: list[str] = []
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT connection_id FROM connector_connections")
        existing = {str(row[0]) for row in await cur.fetchall()}

        for entry in payload:
            connection_id = entry["connection_id"]
            if connection_id in existing and mode == "skip":
                # Left untouched — stored ciphertext and warm cache stand, no invalidation needed.
                report["skipped_existing"] += 1
                continue
            conn_uuid = uuid.UUID(connection_id)
            blob = base64.b64decode(entry["encrypted_blob_b64"])
            raw_expiry = entry.get("session_expires_at")
            session_expires_at = None if raw_expiry is None else datetime.fromisoformat(raw_expiry)
            try:
                async with conn.transaction():
                    await cur.execute(
                        "INSERT INTO connector_connections "
                        "(connection_id, provider_id, alias, encrypted_blob, session_expires_at) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (connection_id) DO UPDATE "
                        "SET provider_id = EXCLUDED.provider_id, "
                        "    alias = EXCLUDED.alias, "
                        "    encrypted_blob = EXCLUDED.encrypted_blob, "
                        "    session_expires_at = EXCLUDED.session_expires_at, "
                        "    cache_version = connector_connections.cache_version + 1, "
                        "    updated_at = now()",
                        (conn_uuid, entry["provider_id"], entry["alias"], blob, session_expires_at),
                    )
            except UniqueViolation as exc:
                if getattr(exc.diag, "constraint_name", None) == _ALIAS_UNIQUE_CONSTRAINT:
                    report["errors"].append(
                        f"connection {connection_id!r}: alias {entry['alias']!r} is already in use "
                        f"for provider {entry['provider_id']!r} by a different connection"
                    )
                    report["skipped"] += 1
                    continue
                raise
            _count(report, connection_id in existing)
            # Invalidate on the canonical UUID the row is stored under (what get() is
            # handed), not the raw backup string — else a non-canonical id drops the wrong key.
            restored_ids.append(str(conn_uuid))

    # Postgres is durable; drop stale cache entries so the next read repopulates.
    await _invalidate_connection_cache(restored_ids)
    return report


async def _invalidate_connection_cache(connection_ids: list[str]) -> None:
    """Drop each restored connection's Redis cache key after the durable write.

    :meth:`RedisPgConnectorTokenStore.get` serves a warm cache HIT WITHOUT a read-side
    version check (the ``cache_version`` fence guards only the cache-MISS repopulate), so a
    restore into a warm cache would keep serving the stale token until the key expired.
    Keyed through the store's own ``_rec_key`` so the keyspace stays single-sourced; a
    failed ``DEL`` raises loudly.
    """
    if not connection_ids:
        return
    store = RedisPgConnectorTokenStore()
    async with client_ctx(RedisClient, connector_store_settings().redis) as client:
        for connection_id in connection_ids:
            await client.delete(store._rec_key(connection_id))


def _count(report: _SectionReport, existed: bool) -> None:
    """Bump the created/updated tally for one upserted row."""
    if existed:
        report["updated"] += 1
    else:
        report["created"] += 1
