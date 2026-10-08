"""Store-level backup export/import for the versioned-document Postgres tables.

Postgres is the durable source of truth, so these helpers operate at the SQL layer
directly — NOT through any typed view. The store is body-opaque and
``kind``-discriminated, so ONE section covers EVERY kind at once (presets, AC
policies, authored agents, any future kind).

The export is a faithful row copy of BOTH ``versioned_documents`` (soft-deleted
ghosts included, so history survives) and ``versioned_document_versions``. Each
row's synthetic ``id`` is carried verbatim so the ``document_id`` foreign key
survives the round-trip; the opaque ``body`` JSONB is never inspected.

Registered ``secret=True`` (default-OFF in the export UI): the opaque bodies are
secret-bearing — the ``settings_profile`` kind stores env values verbatim,
secrets included (``SettingsProfileBody.env``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from tai42_contract.backup import BackupSectionReport
from tai42_contract.states.errors import StatesError
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import Json, PostgresClient, read_connection
from tai42_kit.db import component_store_settings

from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.tools.state_binding import is_binding_refusal
from tai42_skeleton.versioning.restore_checks import restore_check


def _empty_report() -> BackupSectionReport:
    # ``skipped_existing`` (rows left untouched under ``skip``) is this section's own
    # count, so it rides ``details``.
    return BackupSectionReport(details={"skipped_existing": 0})


async def export_versioned_documents() -> dict[str, Any]:
    """Export every document row and every version row verbatim.

    Each keeps its synthetic ``id`` (so the version-to-document link survives) and ``created_at`` is
    serialized. The ``body`` JSONB is carried as-is.
    """
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        read_connection(pool) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT id, kind, name, active_version, is_active, created_at FROM versioned_documents ORDER BY id"
        )
        documents = [
            {
                "id": doc_id,
                "kind": kind,
                "name": name,
                "active_version": active_version,
                "is_active": is_active,
                "created_at": created_at.isoformat(),
            }
            for doc_id, kind, name, active_version, is_active, created_at in await cur.fetchall()
        ]
        await cur.execute(
            "SELECT id, document_id, version, body, tags, created_at FROM versioned_document_versions ORDER BY id"
        )
        versions = [
            {
                "id": version_id,
                "document_id": document_id,
                "version": version,
                "body": body,
                "tags": list(tags or []),
                "created_at": created_at.isoformat(),
            }
            for version_id, document_id, version, body, tags, created_at in await cur.fetchall()
        ]
    return {"documents": documents, "versions": versions}


async def _restore_check_refusal(document: dict[str, Any], bodies: dict[tuple[Any, Any], Any]) -> str | None:
    """The refusal message of the document kind's restore check over its active body, or ``None`` if it passes.

    A kind with no registered check, and a soft-deleted document (history, never live), pass
    unchecked. An active document whose active version row is absent from the backup cannot be
    judged and is refused. A check's ``ValueError`` or states binding refusal is the document's
    refusal; any other error propagates and fails the section.
    """
    check = restore_check(document["kind"])
    if check is None or not document["is_active"]:
        return None
    body = bodies.get((document["id"], document["active_version"]))
    if body is None:
        return f"its active version {document['active_version']} is not in the backup"
    try:
        await check(document["name"], body)
    except ValueError as exc:
        return str(exc)
    except StatesError as exc:
        if not is_binding_refusal(exc):
            raise
        return str(exc)
    return None


async def import_versioned_documents(
    payload: dict[str, Any], mode: Literal["skip", "overwrite"] = "skip"
) -> BackupSectionReport:
    """Restore document + version rows under their original ids.

    Keyed by row ``id`` (documents counted; versions follow their document): under
    ``overwrite`` every row is an ``ON CONFLICT (id) DO UPDATE`` (idempotent); under
    ``skip`` an already-present id is left untouched (documents counted
    ``skipped_existing``) while new rows still restore. Before a document is written, its
    kind's restore check (:mod:`tai42_skeleton.versioning.restore_checks`) runs over its
    active version body: a refused document and its version rows are skipped and reported
    as ``"<kind> <name>: <reason>"``, and a check's store or transport failure fails the
    section. The check reads its stores on connections of its own while this import holds
    one, so a restore needs two connections of the database's pool at once. Documents are
    written before versions so the ``document_id`` foreign key is satisfied; afterward both
    ``BIGSERIAL`` sequences are advanced past the largest restored id so a later
    insert cannot collide with a restored row.
    """
    report = _empty_report()
    documents = payload.get("documents") or []
    versions = payload.get("versions") or []

    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        # Pre-existing ids classify created vs updated; drives the counts only.
        await cur.execute("SELECT id FROM versioned_documents")
        existing = {row[0] for row in await cur.fetchall()}
        await cur.execute("SELECT id FROM versioned_document_versions")
        existing_versions = {row[0] for row in await cur.fetchall()}

        bodies = {(version["document_id"], version["version"]): version["body"] for version in versions}
        refused: set[Any] = set()
        for document in documents:
            if document["id"] in existing and mode == "skip":
                report.details["skipped_existing"] += 1
                continue
            refusal = await _restore_check_refusal(document, bodies)
            if refusal is not None:
                report.errors.append(f"{document['kind']} {document['name']!r}: {refusal}")
                report.skipped += 1
                refused.add(document["id"])
                continue
            await cur.execute(
                "INSERT INTO versioned_documents (id, kind, name, active_version, is_active, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE "
                "SET kind = EXCLUDED.kind, "
                "    name = EXCLUDED.name, "
                "    active_version = EXCLUDED.active_version, "
                "    is_active = EXCLUDED.is_active, "
                "    created_at = EXCLUDED.created_at",
                (
                    document["id"],
                    document["kind"],
                    document["name"],
                    document["active_version"],
                    document["is_active"],
                    datetime.fromisoformat(document["created_at"]),
                ),
            )
            if document["id"] in existing:
                report.updated += 1
            else:
                report.created += 1

        for version in versions:
            if version["document_id"] in refused:
                # A refused document restores none of its history.
                continue
            if version["id"] in existing_versions and mode == "skip":
                # An existing version row is immutable history; leave it as it stands.
                continue
            await cur.execute(
                "INSERT INTO versioned_document_versions (id, document_id, version, body, tags, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE "
                "SET document_id = EXCLUDED.document_id, "
                "    version = EXCLUDED.version, "
                "    body = EXCLUDED.body, "
                "    tags = EXCLUDED.tags, "
                "    created_at = EXCLUDED.created_at",
                (
                    version["id"],
                    version["document_id"],
                    version["version"],
                    Json(version["body"]),
                    list(version["tags"] or []),
                    datetime.fromisoformat(version["created_at"]),
                ),
            )

        # Advance the serial sequences past the restored ids. Guarded on non-empty
        # so ``setval`` never sees a NULL ``MAX(id)``.
        if documents:
            await cur.execute(
                "SELECT setval(pg_get_serial_sequence('versioned_documents', 'id'), "
                "(SELECT MAX(id) FROM versioned_documents))"
            )
        if versions:
            await cur.execute(
                "SELECT setval(pg_get_serial_sequence('versioned_document_versions', 'id'), "
                "(SELECT MAX(id) FROM versioned_document_versions))"
            )

    return report
