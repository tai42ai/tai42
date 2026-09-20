"""A REAL Postgres + Redis exercise of the OAuth token store's durable half: the
``UNIQUE (provider_id, alias)`` constraint surfaced as ``AliasInUseError`` (both the
create-only and the upsert paths, keyed off the real ``exc.diag.constraint_name``), the
``ON CONFLICT (connection_id) DO NOTHING`` create-only guard, the compare-and-set on the
real ``BYTEA`` blob with the ``cache_version`` bump, the ``session_expires_at > now()``
expiry filter over a live ``timestamptz``, and the version-fenced Redis cache-miss
repopulate — behavior no fake pool + fake redis can prove.

The durable Postgres is the operator-provided ``TAI_DATABASE_DEFAULT_PG_*``; the redis
cache is the shared instance named by ``CONNECTOR_STORE_REDIS_URL``. It is OPT-IN: set
``TAI42_SKELETON_REAL_PG=1``. Without the opt-in the tests SKIP VISIBLY (never a silent
skip)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import LiteralString, cast

import pytest
from tai42_contract.connectors.errors import ConnectorError
from tai42_contract.connectors.service import AliasInUseError
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.connectors.settings import connector_store_settings
from tai42_skeleton.connectors.store.redis_pg import RedisPgConnectorTokenStore
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"


async def _pg_exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@dataclass
class _Fixture:
    store: RedisPgConnectorTokenStore
    token: str
    cids: list[str] = field(default_factory=list)

    def cid(self) -> str:
        value = str(uuid.uuid4())
        self.cids.append(value)
        return value


@pytest.fixture
async def token_store() -> AsyncIterator[_Fixture]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres+Redis token store test is opt-in: set {_OPT_IN_ENV}=1, point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres and CONNECTOR_STORE_REDIS_URL at a live "
            "Redis to run it (needs the durable UNIQUE constraint + CAS + a real cache — no fake)"
        )
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    fix = _Fixture(store=RedisPgConnectorTokenStore(), token=f"prov-it-{uuid.uuid4().hex[:12]}")
    yield fix
    # Drop this run's durable rows (scoped by the per-run provider) and every redis cache
    # key it touched (scoped by the unique connection ids), so a shared database + shared
    # redis stay isolated and a re-run starts clean.
    await _pg_exec("DELETE FROM connector_connections WHERE provider_id = %s", (fix.token,))
    if fix.cids:
        async with client_ctx(RedisClient, connector_store_settings().redis) as client:
            for cid in fix.cids:
                await client.delete(fix.store._rec_key(cid))
    await shutdown_all_clients()


async def test_alias_unique_constraint_surfaces_as_alias_in_use(token_store: _Fixture) -> None:
    s, token = token_store.store, token_store.token
    first, second = token_store.cid(), token_store.cid()
    await s.put(first, b"blob-1", create_only=True, provider_id=token, alias="primary")
    # A second, different connection claiming the SAME (provider_id, alias) trips the durable
    # ``UNIQUE`` — the store reads the real constraint name off the driver and raises
    # AliasInUseError (never a silent second row).
    with pytest.raises(AliasInUseError):
        await s.put(second, b"blob-2", create_only=True, provider_id=token, alias="primary")
    # The upsert path enforces it too: re-aliasing onto a taken pair raises the same error.
    await s.put(second, b"blob-2", provider_id=token, alias="secondary")
    with pytest.raises(AliasInUseError):
        await s.put(second, b"blob-2b", provider_id=token, alias="primary")


async def test_create_only_conflict_on_connection_id_is_loud(token_store: _Fixture) -> None:
    s, token = token_store.store, token_store.token
    cid = token_store.cid()
    await s.put(cid, b"blob-1", create_only=True, provider_id=token, alias="a")
    # ``ON CONFLICT (connection_id) DO NOTHING`` returns no row on a re-create; the store
    # turns that empty RETURNING into a loud ConnectorError rather than a silent overwrite.
    with pytest.raises(ConnectorError, match="already exists"):
        await s.put(cid, b"blob-2", create_only=True, provider_id=token, alias="b")
    assert await s.get(cid) == b"blob-1"


async def test_compare_and_set_on_real_bytea_bumps_cache_version(token_store: _Fixture) -> None:
    s, token = token_store.store, token_store.token
    cid = token_store.cid()
    await s.put(cid, b"v1", create_only=True, provider_id=token, alias="a")
    # CAS on the durable BYTEA: the update commits only if the stored ciphertext still equals
    # the blob the caller refreshed from.
    assert await s.put(cid, b"v2", expected_blob=b"v1") is True
    assert await s.get(cid) == b"v2"
    # A stale expected blob loses the CAS (a peer rotated first) — 0 rows, no write.
    assert await s.put(cid, b"v3", expected_blob=b"v1") is False
    assert await s.get(cid) == b"v2"
    # Each durable write bumped the real ``cache_version`` counter (create=1, CAS win=2).
    version = await _read_version(cid)
    assert version == 2


async def test_expired_session_hidden_by_now_filter(token_store: _Fixture) -> None:
    s, token = token_store.store, token_store.token
    cid = token_store.cid()
    past = datetime.now(UTC) - timedelta(hours=1)
    await s.put(cid, b"dead", create_only=True, provider_id=token, alias="a", session_expires_at=past)
    # The default read applies ``session_expires_at IS NULL OR session_expires_at > now()`` in
    # SQL, so a lapsed session is hidden from serving reads and the listing …
    assert await s.get(cid) is None
    assert cid not in await s.list()
    # … while the cleanup path (include_expired) drops the filter so disconnect can still load
    # and purge it.
    assert await s.get(cid, include_expired=True) == b"dead"


async def test_cache_miss_repopulates_from_postgres(token_store: _Fixture) -> None:
    s, token = token_store.store, token_store.token
    cid = token_store.cid()
    await s.put(cid, b"warm", create_only=True, provider_id=token, alias="a")
    # Evict the hot key: the next get is a genuine cache MISS that falls back to the durable
    # Postgres row and repopulates the real Redis cache (version-fenced).
    async with client_ctx(RedisClient, connector_store_settings().redis) as client:
        await client.delete(s._rec_key(cid))
    assert await s.get(cid) == b"warm"
    async with client_ctx(RedisClient, connector_store_settings().redis) as client:
        # redis-py 7.x stubs hget's field as ``str`` and its value as ``str``; a bytes field is
        # accepted and the undecoded value comes back as bytes at runtime (as the store relies on).
        cached = await cast(
            "Awaitable[bytes | None]",
            client.hget(s._rec_key(cid), b"blob"),  # pyright: ignore[reportArgumentType]
        )
    assert cached == b"warm"


async def test_delete_removes_durable_row_and_serves_none(token_store: _Fixture) -> None:
    s, token = token_store.store, token_store.token
    cid = token_store.cid()
    await s.put(cid, b"blob", create_only=True, provider_id=token, alias="a")
    await s.delete(cid)
    # The durable delete is authoritative; a following get finds no blob (cache tombstone →
    # miss → empty Postgres) and the row is gone from the listing.
    assert await s.get(cid) is None
    assert cid not in await s.list()


async def _read_version(cid: str) -> int:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
        conn.cursor() as cur,
    ):
        await cur.execute("SELECT cache_version FROM connector_connections WHERE connection_id = %s", (uuid.UUID(cid),))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])
