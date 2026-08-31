"""A REAL Postgres concurrency test for the tool_meta overlay ``merge_meta`` primitive.

Reproduces the concurrent-boot seed crash: two serve workers apply the same preset seed
against one shared Postgres, so ``merge_meta`` (the seed's placement write) can run
concurrently with ``delete_meta`` (the preset-create clean-slate cascade a sibling worker
runs) against the SAME pre-existing overlay row.

``merge_meta`` must survive that race. The bug it guards: an ``INSERT ... ON CONFLICT DO
NOTHING`` does NOT lock a pre-existing conflicting row, so a concurrent ``delete_meta``
could delete it in the window before a follow-up ``SELECT ... FOR UPDATE``, leaving the
locked read with zero rows — ``RuntimeError: expected a row from a RETURNING statement,
got none``, the exact signature that made ``serve-b`` exit early (code 3). The fixed
primitive locks the row in the upsert itself (``ON CONFLICT DO UPDATE ... RETURNING``), so
a concurrent delete blocks instead of racing, and a delete that already committed just
re-materializes a clean base.

This needs real Postgres row locking — there is no fake here. It is OPT-IN: set
``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a live Postgres.
Without the opt-in the test SKIPS VISIBLY with a clear reason (never a silent skip).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import LiteralString

import pytest
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.tool_meta.store as tool_meta_store
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.tool_meta.store import PostgresToolMetaStore

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"

# The overlay tables, verbatim from the skeleton baseline migration (idempotent
# ``IF NOT EXISTS`` so an already-migrated database is left untouched).
_SCHEMA_SQL: tuple[LiteralString, ...] = (
    "CREATE TABLE IF NOT EXISTS tool_folders ("
    " id UUID NOT NULL DEFAULT gen_random_uuid(), name TEXT NOT NULL,"
    " parent_id UUID REFERENCES tool_folders(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
    " PRIMARY KEY (id),"
    " CONSTRAINT tool_folders_parent_name_unique UNIQUE NULLS NOT DISTINCT (parent_id, name))",
    "CREATE TABLE IF NOT EXISTS tool_meta ("
    " tool_name TEXT NOT NULL, display_name TEXT, folder_id UUID REFERENCES tool_folders(id),"
    " tags TEXT[] NOT NULL DEFAULT '{}', hidden BOOLEAN, badges TEXT[] NOT NULL DEFAULT '{}',"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (tool_name))",
)


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
            f"real-Postgres concurrency test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs real row locking — no fake)"
        )
    # The suite-wide autouse fixture points the store's ``client_ctx`` at the in-memory
    # tool_meta fake so the offline suite never opens Postgres; this test needs the REAL
    # thing (a fake cannot exhibit the row-lock race), so restore the genuine seam.
    monkeypatch.setattr(tool_meta_store, "client_ctx", client_ctx)
    # Rebuild the cached settings so ``TAI_DATABASE_DEFAULT_PG_*`` from the environment is read.
    reset_all_settings()
    for statement in _SCHEMA_SQL:
        await _exec(statement)
    # A unique tool name confines the test to its own row.
    tool_name = f"seed_race_it_{uuid.uuid4().hex}"
    try:
        yield PostgresToolMetaStore(), tool_name
    finally:
        await _exec("DELETE FROM tool_meta WHERE tool_name = %s", (tool_name,))


async def test_merge_meta_survives_concurrent_delete_no_returning_none(real_store):
    """A ``merge_meta`` racing a ``delete_meta`` on the same PRE-EXISTING row must never
    raise ``RuntimeError: expected a row from a RETURNING statement, got none`` — the
    concurrent-boot signature that exited ``serve-b`` early. On the unfixed primitive
    (``INSERT ... ON CONFLICT DO NOTHING`` + ``SELECT ... FOR UPDATE``) the delete slips
    into the unlocked window and the locked read finds zero rows; each iteration reds with
    ~93% probability, so a handful reproduce it near-certainly. The fixed primitive locks
    the row in the upsert, so every merge either completes or re-materializes a clean base."""
    store, tool_name = real_store

    for _ in range(25):
        # The row PRE-EXISTS this iteration — the case where a naive ``DO NOTHING`` insert
        # takes no lock, leaving the follow-up locked read exposed to a concurrent delete.
        await store.merge_meta(tool_name, patch={"tags": ["palette"]})

        async def _merge() -> None:
            await store.merge_meta(tool_name, patch={"display_name": "Send message"})

        async def _delete() -> None:
            await store.delete_meta(tool_name)

        # Both hit one shared Postgres on separate pooled connections — real concurrency,
        # not a simulated interleave. The merge must not raise.
        await asyncio.gather(_merge(), _delete())

    # After the race the primitive is still sound: a final merge lands the seed's placement
    # and reads back exactly one coherent row.
    record = await store.merge_meta(tool_name, patch={"display_name": "Send message", "tags": ["palette"]})
    assert record.tool_name == tool_name
    assert record.display_name == "Send message"
    assert record.tags == ["palette"]
