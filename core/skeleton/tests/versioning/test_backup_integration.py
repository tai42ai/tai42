"""A REAL Postgres round-trip of the versioned-document backup section: seed documents +
version history across TWO kinds (one with a rollback-independent append log, one
soft-deleted so its history survives as a ghost), export the two tables, wipe, then import
and assert the store reads every row back — the two-table ``document_id`` foreign key, the
opaque JSONB bodies, the tag arrays, and the ``is_active`` soft-delete flag all intact.

The SQL round-trip is pinned without a live store in ``test_backup.py``; only a real
database exercises the FK link, jsonb, and the ``ON CONFLICT (id) DO UPDATE`` upserts. It is
OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a live
Postgres. Without the opt-in it SKIPS VISIBLY (never a silent skip)."""

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

import tai42_skeleton.versioning.store as versioning_store
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry
from tai42_skeleton.versioning.backup import export_versioned_documents, import_versioned_documents
from tai42_skeleton.versioning.store import PostgresVersionedStore

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@pytest.fixture
async def real_store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[PostgresVersionedStore, str, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres versioned-document backup round-trip is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs the two-table FK + jsonb — no fake)"
        )
    # The suite-wide autouse fixture points the store's ``client_ctx`` at the in-memory fake;
    # seeding needs the REAL durable store, so restore the genuine pooled seam.
    monkeypatch.setattr(versioning_store, "client_ctx", client_ctx)
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    token = uuid.uuid4().hex[:12]
    kind1, kind2 = f"vk1-{token}", f"vk2-{token}"
    await _wipe(kind1, kind2)
    yield PostgresVersionedStore(), kind1, kind2
    await _wipe(kind1, kind2)
    await shutdown_all_clients()


async def _wipe(kind1: str, kind2: str) -> None:
    # The FK cascade drops the version rows with their parent document.
    await _exec("DELETE FROM versioned_documents WHERE kind IN (%s, %s)", (kind1, kind2))


def _scope(payload: dict[str, Any], kind1: str, kind2: str) -> dict[str, Any]:
    """Narrow the export to this run's two kinds — a shared database holds other kinds the
    round-trip must not touch — carrying the version rows whose document belongs to them."""
    documents = [d for d in payload["documents"] if d["kind"] in (kind1, kind2)]
    doc_ids = {d["id"] for d in documents}
    versions = [v for v in payload["versions"] if v["document_id"] in doc_ids]
    return {"documents": documents, "versions": versions}


async def test_backup_round_trip_preserves_history_and_soft_delete(
    real_store: tuple[PostgresVersionedStore, str, str],
) -> None:
    store, kind1, kind2 = real_store
    # kind1/"a": an append log of two versions (active pointer at v2).
    await store.create(kind1, "a", {"n": 1}, ["first"])
    await store.save_version(kind1, "a", {"n": 2}, ["second"])
    # kind2/"b": created then soft-deleted — its history must survive as an inactive ghost.
    await store.create(kind2, "b", {"m": "x"})
    await store.soft_delete(kind2, "b")

    payload = _scope(await export_versioned_documents(), kind1, kind2)
    assert len(payload["documents"]) == 2
    assert len(payload["versions"]) == 3  # a@v1, a@v2, b@v1

    await _wipe(kind1, kind2)
    created = await import_versioned_documents(payload)
    assert created["errors"] == []
    assert created["created"] == 2  # two documents restored
    assert created["updated"] == 0

    # The append log restored intact: active pointer at v2, both bodies + tags present.
    version, body = await store.get_active_version_and_body(kind1, "a")
    assert (version, body) == (2, {"n": 2})
    versions = {v.version: (v.body, v.tags) for v in await store.list_versions(kind1, "a")}
    assert versions == {1: ({"n": 1}, ["first"]), 2: ({"n": 2}, ["second"])}

    # The soft-deleted ghost survived: absent from the active listing, present in the table
    # with ``is_active = false`` and its one version row.
    assert [r.name for r in await store.list(kind2)] == []
    assert await _row_is_active(kind2, "b") is False
    assert await _version_count(kind2, "b") == 1

    # A second import of the same payload is idempotent — every document already present.
    again = await import_versioned_documents(payload)
    assert again["created"] == 0
    assert again["skipped_existing"] == 2


async def _row_is_active(kind: str, name: str) -> bool:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT is_active FROM versioned_documents WHERE kind = %s AND name = %s", (kind, name))
        row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def _version_count(kind: str, name: str) -> int:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            "SELECT count(*) FROM versioned_document_versions v "
            "JOIN versioned_documents d ON d.id = v.document_id "
            "WHERE d.kind = %s AND d.name = %s",
            (kind, name),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])
