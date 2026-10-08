"""Store-level backup export/import for the connector Postgres tables.

These helpers work below the network-gated service layer; no provider re-probe, no
OAuth replay. Two independent section pairs cover the two halves of connector state:

  * categories — the public, secret-free ``connector_category`` grouping rows, read
    and written at the SQL layer.
  * connections — the per-connection token records, each carrying its AES-GCM
    ciphertext verbatim (base64, NEVER decrypted), read and restored through the
    token store's own full-listing and restore methods.

KEK constraint: a restore is usable only under the SAME ``CONNECTORS_KEK``; under a
different KEK the ciphertext is intact but undecryptable and the load path fails loudly
on first use — never a working-looking-but-dead token.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Literal

from tai42_contract.backup import BackupSectionReport
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient, read_connection
from tai42_kit.db import component_store_settings

from tai42_skeleton.connectors.store.redis_pg import RedisPgConnectorTokenStore, StoredConnection
from tai42_skeleton.db import SKELETON_COMPONENT


def _empty_report() -> BackupSectionReport:
    # ``skipped_existing`` (rows left untouched under ``skip``) is this section's own
    # count, so it rides ``details``.
    return BackupSectionReport(details={"skipped_existing": 0})


# -- connector_category (public, secret-free) --------------------------------


async def export_connector_categories() -> dict[str, Any]:
    """Export the ``connector_category`` grouping rows as a faithful row copy.

    ``created_at`` is carried so the original creation time survives a round-trip.
    """
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        read_connection(pool) as conn,
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
) -> BackupSectionReport:
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
                report.details["skipped_existing"] += 1
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
    """Export every connection record, ``encrypted_blob`` base64-encoded AS-IS.

    Never decrypted, so the KEK boundary is never crossed. ``cache_version`` and timestamps
    are store-regenerated on restore and omitted.
    """
    rows = await RedisPgConnectorTokenStore().list_all_including_expired()
    return [
        {
            "connection_id": row.connection_id,
            "provider_id": row.provider_id,
            "alias": row.alias,
            "session_expires_at": None if row.session_expires_at is None else row.session_expires_at.isoformat(),
            "encrypted_blob_b64": base64.b64encode(row.encrypted_blob).decode("ascii"),
        }
        for row in rows
    ]


async def import_connector_connections(
    payload: list[dict[str, Any]], mode: Literal["skip", "overwrite"] = "skip"
) -> BackupSectionReport:
    """Re-insert each connection's ciphertext under its original connection id through the token store.

    Keyed by ``connection_id``: ``skip`` leaves an already-present connection untouched,
    ``overwrite`` rewrites it. A per-provider alias collision (the durable
    ``UNIQUE (provider_id, alias)``) is a per-row reported error while the rest restore; any
    other database error aborts the section. The store moves each restored connection's cache
    entry to the restored version after the commit and raises when that write-back fails.
    """
    records = [
        StoredConnection(
            connection_id=entry["connection_id"],
            provider_id=entry["provider_id"],
            alias=entry["alias"],
            encrypted_blob=base64.b64decode(entry["encrypted_blob_b64"]),
            session_expires_at=(
                None if entry.get("session_expires_at") is None else datetime.fromisoformat(entry["session_expires_at"])
            ),
        )
        for entry in payload
    ]
    results = await RedisPgConnectorTokenStore().restore(records, overwrite=mode == "overwrite")

    report = _empty_report()
    for record, result in zip(records, results, strict=True):
        if result.outcome == "skipped_existing":
            report.details["skipped_existing"] += 1
        elif result.outcome == "alias_in_use":
            report.errors.append(
                f"connection {record.connection_id!r}: alias {record.alias!r} is already in use "
                f"for provider {record.provider_id!r} by a different connection"
            )
            report.skipped += 1
        else:
            _count(report, result.outcome == "updated")
    return report


def _count(report: BackupSectionReport, existed: bool) -> None:
    """Bump the created/updated tally for one upserted row."""
    if existed:
        report.updated += 1
    else:
        report.created += 1
