"""A REAL Postgres round-trip of the tool-metadata backup section: seed a folder tree (two
folders sharing a name under DIFFERENT parents — legal only under the real
``UNIQUE NULLS NOT DISTINCT (parent_id, name)``) plus overlay rows spanning the tri-state
``hidden`` and the tag / badge arrays, export both tables, wipe, then import in the
section's topological order and assert the store reads the tree and overlays back — the
self-referential ``parent_id`` FK, the ``folder_id`` FK, and the ``hidden`` NULL/TRUE/FALSE
distinction all intact.

The SQL round-trip is pinned without a live store in ``test_backup.py``; only a real
database exercises the FKs and the composite unique. It is OPT-IN: set
``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres.
Without the opt-in it SKIPS VISIBLY (never a silent skip)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any, LiteralString

import pytest
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.tool_meta.store as tool_meta_store
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry
from tai42_skeleton.tool_meta.backup import export_tool_meta, import_tool_meta
from tai42_skeleton.tool_meta.store import PostgresToolMetaStore

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@pytest.fixture
async def real_store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[PostgresToolMetaStore, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres tool-metadata backup round-trip is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs the FKs + composite unique — no fake)"
        )
    # The suite-wide autouse fixture points the store's ``client_ctx`` at the in-memory fake;
    # seeding needs the REAL durable store, so restore the genuine pooled seam.
    monkeypatch.setattr(tool_meta_store, "client_ctx", client_ctx)
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    token = uuid.uuid4().hex[:12]
    await _wipe(token)
    yield PostgresToolMetaStore(), token
    await _wipe(token)
    await shutdown_all_clients()


async def _wipe(token: str) -> None:
    # Overlay rows (folder_id FK) before folders; folders in one statement so a parent and its
    # child drop together without tripping the self-referential FK.
    await _exec("DELETE FROM tool_meta WHERE tool_name LIKE %s", (f"%{token}%",))
    await _exec("DELETE FROM tool_folders WHERE name LIKE %s", (f"%{token}%",))


def _scope(payload: dict[str, Any], folder_ids: set[str], token: str) -> dict[str, Any]:
    """Narrow the export to this run's folders + overlay rows — a shared database holds other
    tools' rows the round-trip must not touch."""
    return {
        "folders": [f for f in payload["folders"] if f["id"] in folder_ids],
        "rows": [r for r in payload["rows"] if token in r["tool_name"]],
    }


async def test_backup_round_trip_preserves_tree_and_tristate_hidden(
    real_store: tuple[PostgresToolMetaStore, str],
) -> None:
    store, token = real_store
    # Two roots, each with a child of the SAME name — legal only because the real unique is
    # ``NULLS NOT DISTINCT (parent_id, name)`` keyed on the DIFFERENT parents.
    root_a = await store.create_folder(f"root-a-{token}")
    root_b = await store.create_folder(f"root-b-{token}")
    child_a = await store.create_folder(f"child-{token}", parent_id=root_a.id)
    child_b = await store.create_folder(f"child-{token}", parent_id=root_b.id)
    folder_ids = {root_a.id, root_b.id, child_a.id, child_b.id}

    # Overlay rows spanning the tri-state ``hidden`` and the tag / badge arrays.
    await store.upsert_meta(
        f"{token}-filed", display_name="Filed", folder_id=child_a.id, tags=["x", "y"], hidden=True, badges=["beta"]
    )
    await store.upsert_meta(f"{token}-unopinion", display_name=None, folder_id=None, tags=[], hidden=None)
    await store.upsert_meta(f"{token}-forced-visible", display_name=None, folder_id=None, tags=[], hidden=False)

    payload = _scope(await export_tool_meta(), folder_ids, token)
    assert len(payload["folders"]) == 4
    assert len(payload["rows"]) == 3

    await _wipe(token)
    report = await import_tool_meta(payload)
    assert report["errors"] == []
    assert report["created"] == 7  # 4 folders + 3 overlay rows

    # The tree restored: the two same-named children survive under their real parents.
    by_id = {f.id: f for f in await store.list_folders()}
    assert (by_id[child_a.id].name, by_id[child_a.id].parent_id) == (f"child-{token}", root_a.id)
    assert (by_id[child_b.id].name, by_id[child_b.id].parent_id) == (f"child-{token}", root_b.id)

    # The tri-state ``hidden`` round-trips as three distinct values, and the placement / arrays
    # come back intact.
    filed = await store.get_meta(f"{token}-filed")
    assert filed is not None
    assert (filed.folder_id, filed.hidden, filed.tags, filed.badges) == (child_a.id, True, ["x", "y"], ["beta"])
    assert (await store.get_meta(f"{token}-unopinion")).hidden is None  # type: ignore[union-attr]
    assert (await store.get_meta(f"{token}-forced-visible")).hidden is False  # type: ignore[union-attr]

    # A second import of the same payload is idempotent — every key already present.
    again = await import_tool_meta(payload)
    assert again["created"] == 0
    assert again["skipped_existing"] == 7
