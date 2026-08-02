"""Store-level backup export/import for the connector Postgres tables.

Postgres is the durable source of truth for every connector table, so these
helpers operate at the SQL layer directly — NOT the network-gated service layer.
No provider is re-probed and no OAuth flow is replayed; a backup is a faithful
row-level copy of what the tables already hold.

Two independent section pairs back the two halves of the connector state:

  * :func:`export_connector_categories` / :func:`import_connector_categories` —
    the public, secret-free ``connector_category`` grouping rows (the UI's
    provider grouping labels + sort order). Each row's ``created_at`` is exported
    and restored so the original creation time survives a round-trip; import
    upserts each row idempotently (``ON CONFLICT (id) DO UPDATE``).

  * :func:`export_connector_connections` / :func:`import_connector_connections`
    — the per-connection token records. Each entry carries the AES-GCM
    ciphertext verbatim (base64-encoded, NEVER decrypted); the ``encrypted_blob``
    bytes cross the backup as-is. Import re-inserts the ciphertext and its
    ``session_expires_at`` under the original ``connection_id``
    (``ON CONFLICT (connection_id) DO UPDATE``). ``cache_version`` and the
    timestamps are store-regenerated and omitted. After the durable Postgres
    write each restored ``connection_id``'s Redis cache key is invalidated
    (``DEL``) so the next ``get`` repopulates from Postgres:
    :meth:`RedisPgConnectorTokenStore.get` serves a warm cache HIT WITHOUT a
    read-side version check, so restoring into a running deployment with a warm
    cache would otherwise keep serving the pre-import (stale) token for an
    already-cached connection until its key expired. A failed invalidation raises
    loudly.

KEK constraint: the blobs are ciphertext under ``CONNECTORS_KEK``. A restore is
usable only if the SAME ``CONNECTORS_KEK`` is present in the target deployment;
under a different KEK the ciphertext is intact but undecryptable, and the record
load path (``connectors.store.persistence``) fails loudly on the first use of a
connection — the restore never silently produces a working-looking-but-dead
token.
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

from tai42_skeleton.connectors.settings import connector_store_settings
from tai42_skeleton.connectors.store.redis_pg import _ALIAS_UNIQUE_CONSTRAINT, RedisPgConnectorTokenStore

# The report shape every importer returns, matching the backup section contract
# (``connectors.store.backup`` is a separate subsystem from ``backup.sections``,
# so it builds its own report rather than importing across the seam).
_SectionReport = dict[str, Any]


def _empty_report() -> _SectionReport:
    return {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}


# -- connector_category (public, secret-free) --------------------------------


async def export_connector_categories() -> dict[str, Any]:
    """Export the ``connector_category`` grouping rows.

    Every row is read raw. Each row's ``created_at`` is exported so the original
    creation time survives a round-trip; the export is a faithful row copy.
    """
    async with (
        client_ctx(PostgresClient, connector_store_settings().pg) as pool,
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

    Under ``overwrite`` each row is an ``ON CONFLICT (id) DO UPDATE`` (idempotent), so a
    re-import over identical rows is a no-op change reported as ``updated``. Under
    ``skip`` an already-present id is left untouched (``skipped_existing``) and only new
    ids are inserted. The backed-up ``created_at`` is written on the INSERT so the
    original creation time survives a restore into a fresh table; it is immutable, so
    the ``DO UPDATE`` branch leaves the existing value untouched.
    """
    report = _empty_report()
    categories = payload.get("categories") or []

    async with (
        client_ctx(PostgresClient, connector_store_settings().pg) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        # Read the current keys first so each upsert is classified created vs
        # updated (the same read-existing-then-upsert pattern the other sections
        # use); this drives only the report counts, not the write itself.
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
    """Export every connection record with its ciphertext carried verbatim.

    The ``encrypted_blob`` bytes are base64-encoded AS-IS — never decrypted — so
    the KEK boundary is never crossed. ``cache_version`` and the timestamps are
    store-regenerated on restore and therefore omitted.
    """
    async with (
        client_ctx(PostgresClient, connector_store_settings().pg) as pool,
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

    Keyed by ``connection_id``: under ``skip`` an already-present connection is left
    untouched (``skipped_existing``, its cached token undisturbed) and only new ones are
    inserted; under ``overwrite`` the ciphertext is upserted and the stale cache key
    dropped.

    Each row runs inside its own savepoint (``conn.transaction()``) so a
    per-provider alias collision isolates to that row: an ``(provider_id, alias)``
    already held by a DIFFERENT ``connection_id`` trips the durable
    ``UNIQUE (provider_id, alias)`` constraint, which is caught and reported as a
    per-row error (never silently dropped) while the remaining rows still
    restore. Any other database error raises loudly and aborts the section.

    After the Postgres writes commit, each restored ``connection_id``'s Redis
    cache key is invalidated so a warm cache in a running deployment cannot keep
    serving the pre-import (stale) token — see :func:`_invalidate_connection_cache`.
    """
    report = _empty_report()
    restored_ids: list[str] = []
    async with (
        client_ctx(PostgresClient, connector_store_settings().pg) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT connection_id FROM connector_connections")
        existing = {str(row[0]) for row in await cur.fetchall()}

        for entry in payload:
            connection_id = entry["connection_id"]
            if connection_id in existing and mode == "skip":
                # An existing connection is left untouched — its stored ciphertext and
                # warm cache entry stand, so no cache invalidation is needed either.
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
            # Invalidate on the canonical UUID the row is stored under (and that
            # get() is always handed), not the raw backup string — a non-canonical
            # id in a foreign backup would otherwise drop the wrong cache key.
            restored_ids.append(str(conn_uuid))

    # Postgres is now durable; drop the stale cache entries so the next read
    # repopulates the restored token from Postgres (raises loudly on failure).
    await _invalidate_connection_cache(restored_ids)
    return report


async def _invalidate_connection_cache(connection_ids: list[str]) -> None:
    """Drop each restored connection's Redis cache key after the durable Postgres
    write so the next ``get`` repopulates from Postgres.

    :meth:`RedisPgConnectorTokenStore.get` returns a cached blob on a cache HIT
    WITHOUT a read-side version check (the ``cache_version`` fence guards only the
    cache-MISS repopulate). A restore into a running deployment with a warm cache
    would therefore keep serving the pre-import (stale) token for any
    already-cached ``connection_id`` until its key expired. Deleting the key
    forces a version-fenced repopulate from the freshly restored row.

    The key is built through the store's own ``_rec_key`` helper so the cache
    keyspace stays single-sourced. A failed ``DEL`` raises loudly — the restore
    must not silently leave a stale token cached.
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
