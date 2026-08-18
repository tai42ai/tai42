"""Store-level backup export/import for the tool-metadata Postgres tables.

Postgres is the durable source of truth, so these helpers operate at the SQL layer
directly — NOT through the typed store. The export is a faithful row copy of BOTH
``tool_folders`` (each ``id`` carried verbatim so the self-referential ``parent_id``
link survives) and ``tool_meta`` (the per-tool overlay rows). Import restores folders
before overlay rows (for the ``folder_id`` FK) and in topological order with their
real ``parent_id`` (see :func:`import_tool_meta`).

Registered ``secret=False`` (default-ON in the export UI): the overlay holds
organizational metadata only — no credentials.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.db import SKELETON_COMPONENT

# The report shape every importer returns, matching the backup section contract.
_SectionReport = dict[str, Any]


def _empty_report() -> _SectionReport:
    return {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}


def _topological(folders: list[dict[str, Any]], existing_ids: set[str]) -> list[dict[str, Any]]:
    """Order ``folders`` so each is emitted only after its parent — roots first,
    then folders whose parent is already placed, pre-existing in the DB, or absent
    from the payload (a reference to a folder already restored). ``existing_ids`` are
    the folder ids already present in the DB before this restore. A payload the store
    can never emit — a parent cycle — makes no progress; those rows are appended in
    payload order so the FK surfaces the broken data loudly rather than looping."""
    by_id = {folder["id"]: folder for folder in folders}
    placed: set[str] = set()
    ordered: list[dict[str, Any]] = []
    remaining = list(folders)
    while remaining:
        ready = [
            folder
            for folder in remaining
            if folder["parent_id"] is None
            or folder["parent_id"] in placed
            or folder["parent_id"] in existing_ids
            or folder["parent_id"] not in by_id
        ]
        if not ready:
            ready = remaining
        for folder in ready:
            ordered.append(folder)
            placed.add(folder["id"])
        remaining = [folder for folder in remaining if folder["id"] not in placed]
    return ordered


async def export_tool_meta() -> dict[str, Any]:
    """Export every folder row and every overlay row verbatim.

    Each folder keeps its synthetic ``id`` and ``parent_id`` so the tree survives
    the round-trip; ``created_at`` is serialized so the original timestamps restore
    intact.
    """
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT id, name, parent_id, created_at FROM tool_folders ORDER BY id")
        folders = [
            {
                "id": str(folder_id),
                "name": name,
                "parent_id": None if parent_id is None else str(parent_id),
                "created_at": created_at.isoformat(),
            }
            for folder_id, name, parent_id, created_at in await cur.fetchall()
        ]
        await cur.execute(
            "SELECT tool_name, display_name, folder_id, tags, hidden, badges, created_at "
            "FROM tool_meta ORDER BY tool_name"
        )
        rows = [
            {
                "tool_name": tool_name,
                "display_name": display_name,
                "folder_id": None if folder_id is None else str(folder_id),
                "tags": list(tags or []),
                "hidden": hidden,
                "badges": list(badges or []),
                "created_at": created_at.isoformat(),
            }
            for tool_name, display_name, folder_id, tags, hidden, badges, created_at in await cur.fetchall()
        ]
    return {"folders": folders, "rows": rows}


async def import_tool_meta(payload: dict[str, Any], mode: Literal["skip", "overwrite"] = "skip") -> _SectionReport:
    """Restore folder + overlay rows under their original keys.

    Folders are keyed by ``id``, overlay rows by ``tool_name``: under ``skip`` an
    already-present key is left untouched (``skipped_existing``); under ``overwrite``
    each row is an ``ON CONFLICT DO UPDATE``.

    Folders are written before overlay rows (for the ``folder_id`` FK) and in
    TOPOLOGICAL order carrying their REAL ``parent_id`` — a blanket ``NULL`` first
    pass is WRONG because ``tool_folders`` is
    ``UNIQUE NULLS NOT DISTINCT (parent_id, name)``, so two folders sharing a ``name``
    under different parents would both collapse to ``(NULL, name)`` and collide.
    """
    report = _empty_report()
    folders = payload.get("folders") or []
    rows = payload.get("rows") or []

    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT id FROM tool_folders")
        existing_folders = {str(row[0]) for row in await cur.fetchall()}
        await cur.execute("SELECT tool_name FROM tool_meta")
        existing_rows = {row[0] for row in await cur.fetchall()}

        # Real parent_id in topological order (see _topological); a genuinely
        # dangling parent surfaces loudly as an FK violation.
        for folder in _topological(folders, existing_folders):
            if folder["id"] in existing_folders and mode == "skip":
                report["skipped_existing"] += 1
                continue
            await cur.execute(
                "INSERT INTO tool_folders (id, name, parent_id, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "name = EXCLUDED.name, parent_id = EXCLUDED.parent_id, created_at = EXCLUDED.created_at",
                (
                    folder["id"],
                    folder["name"],
                    folder["parent_id"],
                    datetime.fromisoformat(folder["created_at"]),
                ),
            )
            if folder["id"] in existing_folders:
                report["updated"] += 1
            else:
                report["created"] += 1

        for row in rows:
            if row["tool_name"] in existing_rows and mode == "skip":
                report["skipped_existing"] += 1
                continue
            await cur.execute(
                "INSERT INTO tool_meta (tool_name, display_name, folder_id, tags, hidden, badges, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tool_name) DO UPDATE SET "
                "display_name = EXCLUDED.display_name, folder_id = EXCLUDED.folder_id, "
                "tags = EXCLUDED.tags, hidden = EXCLUDED.hidden, badges = EXCLUDED.badges, "
                "created_at = EXCLUDED.created_at",
                (
                    row["tool_name"],
                    row["display_name"],
                    row["folder_id"],
                    list(row["tags"] or []),
                    row["hidden"],
                    list(row["badges"] or []),
                    datetime.fromisoformat(row["created_at"]),
                ),
            )
            if row["tool_name"] in existing_rows:
                report["updated"] += 1
            else:
                report["created"] += 1

    return report
