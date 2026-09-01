"""REAL two-transaction Postgres concurrency tests for the versioned-document store.

Two races, both needing genuine MVCC + row locking:

* the ``for_update`` locked read: the non-JOIN two-statement lock in
  ``PostgresVersionedStore.get_active_body`` must NOT raise a spurious
  ``DocumentNotFoundError`` when a second concurrent editor blocks on the row lock and
  the first commits a NEW active version. A single ``JOIN ... FOR UPDATE`` trips an
  EvalPlanQual hazard under READ COMMITTED: the blocked scan re-evaluates the version
  join under its ORIGINAL snapshot, where the just-committed version row is invisible,
  and finds 0 rows for a document that plainly exists;
* the concurrent-boot SEED claim: two replicas creating the same ``(kind, name)`` must
  converge on ONE active document, and the loser must keep a USABLE transaction — a
  raised unique violation would abort its whole unit of work.

There is no fake here. These are OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point
``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres. Without the opt-in they SKIP VISIBLY
with a clear reason (never a silent skip).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import LiteralString

import pytest
from tai42_contract.versioning.errors import DocumentExistsError
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.versioning.store as versioning_store
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.versioning.store import PostgresVersionedStore

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"

# The two tables + the partial-unique active index, verbatim from the skeleton init SQL
# (idempotent ``IF NOT EXISTS`` so an already-migrated database is left untouched).
_SCHEMA_SQL: tuple[LiteralString, ...] = (
    "CREATE TABLE IF NOT EXISTS versioned_documents ("
    " id BIGSERIAL NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,"
    " active_version INTEGER NOT NULL, is_active BOOLEAN NOT NULL DEFAULT TRUE,"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (id))",
    "CREATE UNIQUE INDEX IF NOT EXISTS versioned_documents_active_name_unique"
    " ON versioned_documents (kind, name) WHERE is_active",
    "CREATE TABLE IF NOT EXISTS versioned_document_versions ("
    " id BIGSERIAL NOT NULL,"
    " document_id BIGINT NOT NULL REFERENCES versioned_documents (id) ON DELETE CASCADE,"
    " version INTEGER NOT NULL, body JSONB NOT NULL, tags TEXT[] NOT NULL DEFAULT '{}',"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (id),"
    " CONSTRAINT versioned_document_versions_doc_version_unique UNIQUE (document_id, version))",
)


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
            f"real-Postgres concurrency test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs real MVCC — no fake)"
        )
    # The suite-wide autouse fixture points the store's ``client_ctx`` at the in-memory
    # versioning fake so the offline suite never opens Postgres; this test needs the REAL
    # thing (a fake cannot exhibit the MVCC/EvalPlanQual hazard), so restore the genuine seam.
    monkeypatch.setattr(versioning_store, "client_ctx", client_ctx)
    # Rebuild the cached settings so ``TAI_DATABASE_DEFAULT_PG_*`` from the environment is read
    # (a stale cached settings object would target the wrong database).
    reset_all_settings()
    for statement in _SCHEMA_SQL:
        await _exec(statement)
    kind = f"role_tx1_it_{uuid.uuid4().hex}"
    try:
        yield PostgresVersionedStore(), kind
    finally:
        # The unique per-test kind confines the fixture to its own rows; the FK cascade
        # drops the version rows with the parent.
        await _exec("DELETE FROM versioned_documents WHERE kind = %s", (kind,))


async def test_concurrent_editor_reads_committed_body_no_spurious_404(real_store):
    store, kind = real_store
    name = "ops"
    await store.create(kind, name, {"gen": 1})

    lock_held = asyncio.Event()
    reader_started = asyncio.Event()

    async def first_editor() -> None:
        async with store.transaction() as tx:
            # Lock the parent row, append v2, and re-point active_version -> 2. The whole
            # transaction (lock + uncommitted v2) is held until the second editor's locked
            # read is blocking on it, then commits on context exit.
            await store.get_active_body(kind, name, tx=tx, for_update=True)
            await store.save_version(kind, name, {"gen": 2}, tx=tx)
            lock_held.set()
            await reader_started.wait()
            # Give the second editor's FOR UPDATE statement time to reach Postgres and
            # block on the lock BEFORE this transaction commits — the exact race that trips
            # the JOIN's EvalPlanQual hazard.
            await asyncio.sleep(0.5)

    async def second_editor() -> dict:
        await lock_held.wait()
        async with store.transaction() as tx:
            reader_started.set()
            # Blocks on the first editor's row lock; unblocks once it commits v2. The fix
            # must then read the COMMITTED v2 body rather than raise a spurious 404.
            return await store.get_active_body(kind, name, tx=tx, for_update=True)

    writer = asyncio.create_task(first_editor())
    body = await second_editor()
    await writer

    assert body == {"gen": 2}


async def test_concurrent_seed_claims_converge_on_the_winner(real_store):
    # Two platform replicas booting against one Postgres apply the same declared seed:
    # both find the document absent and both claim it. The winner inserts; the loser's
    # insert BLOCKS on the uncommitted index entry, then — once the winner commits —
    # must resolve to "already seeded" WITHOUT aborting its transaction, so it can read
    # the winner's body on that same unit of work and commit. A create that let the
    # unique violation raise leaves the loser's transaction aborted, so this read dies
    # with InFailedSqlTransaction and the boot handler takes the replica down.
    store, kind = real_store
    name = "editor"
    winner_body = {"grants": {"tools": "write"}}

    claimed = asyncio.Event()
    loser_started = asyncio.Event()

    async def winning_replica() -> None:
        async with store.transaction() as tx:
            await store.create(kind, name, winner_body, tx=tx)
            claimed.set()
            await loser_started.wait()
            # Let the loser's INSERT reach Postgres and block on this uncommitted index
            # entry BEFORE this transaction commits — the exact concurrent-boot race.
            await asyncio.sleep(0.5)

    async def losing_replica() -> dict:
        await claimed.wait()
        async with store.transaction() as tx:
            loser_started.set()
            with pytest.raises(DocumentExistsError):
                await store.create(kind, name, {"grants": {"tools": "read"}}, tx=tx)
            return await store.get_active_body(kind, name, tx=tx)

    winner = asyncio.create_task(winning_replica())
    seen_by_loser = await losing_replica()
    await winner

    # First writer wins, the loser read it, and the seed is stored exactly once.
    assert seen_by_loser == winner_body
    assert await store.get_active_body(kind, name) == winner_body
    assert [record.name for record in await store.list(kind)] == [name]
    assert [version.version for version in await store.list_versions(kind, name)] == [1]
