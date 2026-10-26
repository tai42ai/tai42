"""The versioned store's population reads on real Postgres: each active body, and every version.

There is no fake here. These are OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point
``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres. Without the opt-in they SKIP VISIBLY
with a clear reason (never a silent skip).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import LiteralString

import pytest
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.versioning.store as versioning_store
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry
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
async def real_store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[PostgresVersionedStore, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres store listing test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (the JOIN reads run as SQL — no fake)"
        )
    # The suite-wide autouse fixture points the store's ``client_ctx`` at the in-memory fake;
    # these reads need the real database, so the genuine seam is restored.
    monkeypatch.setattr(versioning_store, "client_ctx", client_ctx)
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    kind = f"listing_it_{uuid.uuid4().hex}"
    try:
        yield PostgresVersionedStore(), kind
    finally:
        await _exec("DELETE FROM versioned_documents WHERE kind = %s", (kind,))


async def _seed(store: PostgresVersionedStore, kind: str) -> None:
    await store.create(kind, "b", {"gen": 1})
    await store.save_version(kind, "b", {"gen": 2})
    await store.save_version(kind, "b", {"gen": 3})
    await store.rollback(kind, "b", 2)
    await store.create(kind, "a", {"gen": 1})
    await store.create(kind, "gone", {"gen": 1})
    await _exec("UPDATE versioned_documents SET is_active = false WHERE kind = %s AND name = 'gone'", (kind,))
    await store.create(f"{kind}_other", "a", {"gen": 9})


async def test_each_active_document_reads_its_active_version_and_body(real_store) -> None:
    store, kind = real_store
    try:
        await _seed(store, kind)
        assert await store.list_active_version_and_body(kind) == {"a": (1, {"gen": 1}), "b": (2, {"gen": 2})}
    finally:
        await _exec("DELETE FROM versioned_documents WHERE kind = %s", (f"{kind}_other",))


async def test_every_version_of_each_active_document_reads_oldest_first(real_store) -> None:
    store, kind = real_store
    try:
        await _seed(store, kind)
        assert await store.list_every_version_body(kind) == {
            "a": [(1, {"gen": 1})],
            "b": [(1, {"gen": 1}), (2, {"gen": 2}), (3, {"gen": 3})],
        }
    finally:
        await _exec("DELETE FROM versioned_documents WHERE kind = %s", (f"{kind}_other",))
