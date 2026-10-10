"""The store-format generation gate: an empty store records the generation, old data is refused at first use."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.base import empty_checkpoint

from tai42_kit.llm.checkpoint import checkpoint as cp
from tai42_kit.llm.checkpoint import store_format
from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.checkpoint.ledger import MemoryFinishedThreadLedger
from tai42_kit.llm.checkpoint.store_format import (
    CHECKPOINT_STORE_FORMAT_GENERATION,
    CheckpointStoreFormatError,
    ensure_store_format,
)

from ._real_redis import real_redis_url

_METADATA: dict[str, Any] = {"source": "input", "step": 0, "parents": {}}

# The Redis marker value this version records in an empty store.
_REDIS_MARKER = str(CHECKPOINT_STORE_FORMAT_GENERATION).encode()


def _refusal(provider: str, found: str) -> str:
    return (
        f"the {provider} checkpoint store holds data written in an older format (generation {found}, this version "
        f"writes {CHECKPOINT_STORE_FORMAT_GENERATION}); reset the checkpoint store before starting this version"
    )


async def _put_one(saver: Any) -> None:
    config = {"configurable": {"thread_id": f"t-{uuid.uuid4().hex}", "checkpoint_ns": ""}}
    await saver.aput(config, empty_checkpoint(), _METADATA, {})


def test_the_generation_is_three():
    assert CHECKPOINT_STORE_FORMAT_GENERATION == 3


async def test_memory_store_carries_no_marker():
    from langgraph.checkpoint.memory import InMemorySaver

    await ensure_store_format(CheckpointResource("memory", InMemorySaver(), MemoryFinishedThreadLedger()))


# --------------------------------------------------------------------------- #
# sqlite
# --------------------------------------------------------------------------- #
@pytest.fixture
async def sqlite_store(tmp_path) -> AsyncIterator[tuple[Any, CheckpointResource]]:
    aiosqlite = pytest.importorskip("aiosqlite")
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    conn = await aiosqlite.connect(str(tmp_path / "store.db"))
    try:
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        yield saver, CheckpointResource("sqlite", conn, MemoryFinishedThreadLedger())
    finally:
        await conn.close()


async def _sqlite_marker(conn: Any) -> int | None:
    async with conn.execute("SELECT generation FROM tai42_checkpoint_format WHERE id = 1") as cursor:
        row = await cursor.fetchone()
    return None if row is None else row[0]


async def test_sqlite_empty_store_records_the_generation(sqlite_store):
    _saver, resource = sqlite_store
    await ensure_store_format(resource)
    await ensure_store_format(resource)
    assert await _sqlite_marker(resource.handle) == CHECKPOINT_STORE_FORMAT_GENERATION


async def test_sqlite_data_without_a_marker_is_refused(sqlite_store):
    saver, resource = sqlite_store
    await _put_one(saver)
    with pytest.raises(CheckpointStoreFormatError) as excinfo:
        await ensure_store_format(resource)
    assert str(excinfo.value) == _refusal("sqlite", "none")


async def test_sqlite_another_generation_is_refused(sqlite_store):
    _saver, resource = sqlite_store
    await ensure_store_format(resource)
    await resource.handle.execute("UPDATE tai42_checkpoint_format SET generation = 1")
    await resource.handle.commit()
    with pytest.raises(CheckpointStoreFormatError) as excinfo:
        await ensure_store_format(resource)
    assert str(excinfo.value) == _refusal("sqlite", "1")


async def test_sqlite_a_generation_two_store_is_refused(sqlite_store):
    _saver, resource = sqlite_store
    await ensure_store_format(resource)
    await resource.handle.execute("UPDATE tai42_checkpoint_format SET generation = 2")
    await resource.handle.commit()
    with pytest.raises(CheckpointStoreFormatError) as excinfo:
        await ensure_store_format(resource)
    assert str(excinfo.value) == _refusal("sqlite", "2")


async def test_sqlite_marked_store_with_data_proceeds(sqlite_store):
    saver, resource = sqlite_store
    await ensure_store_format(resource)
    await _put_one(saver)
    await ensure_store_format(resource)


# --------------------------------------------------------------------------- #
# postgres — a throwaway database per test
# --------------------------------------------------------------------------- #
@pytest.fixture
async def postgres_store() -> AsyncIterator[tuple[Any, CheckpointResource]]:
    if os.environ.get("TAI42_KIT_REAL_PG") not in ("1", "true", "True"):
        pytest.skip("real-Postgres store-format test is opt-in: set TAI42_KIT_REAL_PG=1 and the PG env")
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection, sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from tai42_kit.clients.impl.postgres import open_dedicated_pool
    from tai42_kit.db import database_settings
    from tai42_kit.llm._langgraph_postgres import LANGGRAPH_CONNECTION_KWARGS

    admin_dsn = database_settings("default").pg_dsn
    name = f"tai42_kit_format_{uuid.uuid4().hex[:12]}"
    async with await AsyncConnection.connect(admin_dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    info = conninfo_to_dict(admin_dsn)
    info["dbname"] = name
    pool = await open_dedicated_pool(
        make_conninfo("", **info), role="test", connection_kwargs=dict(LANGGRAPH_CONNECTION_KWARGS)
    )
    try:
        saver = AsyncPostgresSaver(cast("Any", pool))
        await saver.setup()
        yield saver, CheckpointResource("postgres", pool, MemoryFinishedThreadLedger())
    finally:
        await pool.close()
        async with await AsyncConnection.connect(admin_dsn, autocommit=True) as admin:
            await admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


integration = pytest.mark.integration


@integration
async def test_postgres_empty_store_records_the_generation(postgres_store):
    from tai42_kit.llm.checkpoint import postgres_store as pg_store

    _saver, resource = postgres_store
    await ensure_store_format(resource)
    assert await pg_store.read_format_generation(resource.handle) == CHECKPOINT_STORE_FORMAT_GENERATION


@integration
async def test_postgres_data_without_a_marker_is_refused(postgres_store):
    saver, resource = postgres_store
    await _put_one(saver)
    with pytest.raises(CheckpointStoreFormatError) as excinfo:
        await ensure_store_format(resource)
    assert str(excinfo.value) == _refusal("postgres", "none")


@integration
async def test_postgres_another_generation_is_refused(postgres_store):
    _saver, resource = postgres_store
    await ensure_store_format(resource)
    async with resource.handle.connection() as conn:
        await conn.execute("UPDATE tai42_checkpoint_format SET generation = 1")
    with pytest.raises(CheckpointStoreFormatError) as excinfo:
        await ensure_store_format(resource)
    assert str(excinfo.value) == _refusal("postgres", "1")


@integration
async def test_postgres_concurrent_first_builds_converge(postgres_store):
    from tai42_kit.llm.checkpoint import postgres_store as pg_store

    _saver, resource = postgres_store
    await asyncio.gather(*(ensure_store_format(resource) for _ in range(4)))
    assert await pg_store.read_format_generation(resource.handle) == CHECKPOINT_STORE_FORMAT_GENERATION


# --------------------------------------------------------------------------- #
# redis — a saver on its own key prefix and a marker key of its own per test
# --------------------------------------------------------------------------- #
@pytest.fixture
async def redis_store(monkeypatch) -> AsyncIterator[tuple[Any, CheckpointResource]]:
    url = real_redis_url()
    from redis.asyncio import Redis as AsyncRedis

    from tai42_kit.llm.checkpoint.codec import GuardedAsyncRedisSaver

    suffix = uuid.uuid4().hex[:12]
    monkeypatch.setattr(store_format, "REDIS_FORMAT_KEY", f"tai42:checkpoint:format:{suffix}")
    client = AsyncRedis.from_url(url)
    saver = GuardedAsyncRedisSaver(
        redis_client=client, checkpoint_prefix=f"gate{suffix}", checkpoint_write_prefix=f"gatew{suffix}"
    )
    cp._guard_saver_serialization(saver, cp.checkpoint_provider_facts("redis"))
    await saver.asetup()
    try:
        yield saver, CheckpointResource("redis", saver, MemoryFinishedThreadLedger(), client)
    finally:
        await client.delete(store_format.REDIS_FORMAT_KEY)
        await saver.checkpoints_index.delete(drop=True)
        await saver.checkpoint_writes_index.delete(drop=True)
        await client.aclose()


@integration
async def test_redis_empty_store_records_the_generation(redis_store):
    _saver, resource = redis_store
    await ensure_store_format(resource)
    await ensure_store_format(resource)
    assert await resource.redis_client.get(store_format.REDIS_FORMAT_KEY) == _REDIS_MARKER


@integration
async def test_redis_data_without_a_marker_is_refused(redis_store):
    saver, resource = redis_store
    await _put_one(saver)
    with pytest.raises(CheckpointStoreFormatError) as excinfo:
        await ensure_store_format(resource)
    assert str(excinfo.value) == _refusal("redis", "none")


@integration
async def test_redis_another_generation_is_refused(redis_store):
    _saver, resource = redis_store
    await resource.redis_client.set(store_format.REDIS_FORMAT_KEY, "1")
    with pytest.raises(CheckpointStoreFormatError) as excinfo:
        await ensure_store_format(resource)
    assert str(excinfo.value) == _refusal("redis", "1")


@integration
async def test_redis_a_garbage_marker_is_refused(redis_store):
    _saver, resource = redis_store
    await resource.redis_client.set(store_format.REDIS_FORMAT_KEY, "two")
    with pytest.raises(CheckpointStoreFormatError, match="holds 'two', not a generation"):
        await ensure_store_format(resource)


@integration
async def test_redis_concurrent_first_builds_converge(redis_store):
    _saver, resource = redis_store
    await asyncio.gather(*(ensure_store_format(resource) for _ in range(4)))
    assert await resource.redis_client.get(store_format.REDIS_FORMAT_KEY) == _REDIS_MARKER


async def test_a_redis_resource_without_its_client_is_refused():
    with pytest.raises(ValueError, match="carries its client"):
        await ensure_store_format(CheckpointResource("redis", object(), MemoryFinishedThreadLedger()))


# --------------------------------------------------------------------------- #
# the gate runs at saver build, once per store, and a refusal closes the resource
# --------------------------------------------------------------------------- #
async def test_the_gate_runs_at_the_first_saver_build_only(monkeypatch):
    seen: list[str] = []

    async def _recording_gate(resource):
        seen.append(resource.provider)

    monkeypatch.setattr(cp, "ensure_store_format", _recording_gate)
    registry = checkpoint_registry()
    try:
        assert seen == []  # no saver built, no gate
        await registry.get_checkpointer("memory", None)
        await registry.get_checkpointer("memory", None)
        await registry.ledger("memory", None)
        assert seen == ["memory"]
    finally:
        await registry.close_all()


async def test_a_refused_store_is_closed_and_the_build_fails(monkeypatch, tmp_path):
    pytest.importorskip("aiosqlite")
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        await _put_one(saver)

    closed: list[bool] = []
    real_create = cp._create_sqlite_checkpoint

    async def _tracking_create(conn_string):
        resource, cleanup = await real_create(conn_string)

        async def _cleanup():
            closed.append(True)
            await cleanup()

        return resource, _cleanup

    monkeypatch.setitem(cp._BUILDERS, "sqlite", _tracking_create)
    with pytest.raises(CheckpointStoreFormatError):
        await cp.create_checkpoint_resource("sqlite", path)
    assert closed == [True]
