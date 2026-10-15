"""Shared checkpoint values through the real Redis and Postgres savers the kit builds.

Redis stores values once by default and Postgres stores every value inline by default; the deployment's
switch turns it on or off. A value above the threshold is stored once per thread and referenced; a later
checkpoint carrying it adds nothing; reads restore it whatever the switch says now; deletes and retention
remove it; a value removed behind the saver's back fails the read loudly. Opt-in like the kit's other
real-store tests: ``TAI42_KIT_REAL_REDIS_URL`` for Redis, ``TAI42_KIT_REAL_PG=1`` and the
``TAI_DATABASE_DEFAULT_PG_*`` env for Postgres.
"""

from __future__ import annotations

import asyncio
import base64
import operator
import os
import random
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph.checkpoint.redis")
pytest.importorskip("langgraph.checkpoint.postgres.aio")

from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint
from langgraph.graph import END, START, StateGraph

from tai42_kit.llm.checkpoint.checkpoint import create_checkpoint_resource, get_saver_from_resource
from tai42_kit.llm.checkpoint.chunking import CheckpointReferenceError
from tai42_kit.llm.checkpoint.codec import envelope_reference
from tai42_kit.llm.checkpoint.value_store import REDIS_VALUE_INDEX_PREFIX, REDIS_VALUE_KEY_PREFIX
from tai42_kit.settings import reset_all_settings

from ._real_redis import REAL_REDIS_ENV
from .test_checkpoint_codec_redis import _extend

pytestmark = pytest.mark.integration

SWITCH_ENV = "LLM_PROVIDER_CHECKPOINT_SHARED_VALUES"
SIZE_ENV = "LLM_PROVIDER_CHECKPOINT_SHARED_VALUE_BYTES"
_PG_OPT_IN = "TAI42_KIT_REAL_PG"
_METADATA: dict[str, Any] = {"source": "loop", "step": 0, "parents": {}}


def _conn_string(provider: str) -> str:
    if provider == "redis":
        url = os.environ.get(REAL_REDIS_ENV)
        if not url:
            pytest.skip(f"real-Redis shared-value test is opt-in: set {REAL_REDIS_ENV} to a Redis 8 URL")
        return url
    if os.environ.get(_PG_OPT_IN) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres shared-value test is opt-in: set {_PG_OPT_IN}=1 and the TAI_DATABASE_DEFAULT_PG_* env"
        )
    from tai42_kit.db import database_settings

    return database_settings("default").pg_dsn


@dataclass
class Store:
    """One provider's saver, with the probes a test reads the stored form through."""

    provider: str
    saver: Any
    resource: Any

    async def digests(self, thread_id: str) -> set[str]:
        if self.provider == "redis":
            prefix = f"{REDIS_VALUE_KEY_PREFIX}{{{thread_id}}}:"
            keys = [key async for key in self.resource.redis_client.scan_iter(match=f"{prefix}*", count=1000)]
            return {(key.decode() if isinstance(key, bytes) else key)[len(prefix) :] for key in keys}
        async with self.resource.handle.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT encode(digest, 'hex') FROM tai42_checkpoint_values WHERE thread_id = %s", (thread_id,)
            )
            rows = await cur.fetchall()
        return {next(iter(row.values())) if isinstance(row, dict) else row[0] for row in rows}

    async def stored_channel(self, thread_id: str, channel: str) -> Any:
        """The latest stored form of ``channel``: the Redis document's value, the Postgres blob decoded."""
        if self.provider == "redis":
            client = self.resource.redis_client
            key = await client.get(self.saver._make_redis_checkpoint_latest_key(thread_id, ""))
            [values] = await client.json().get(key, "$.checkpoint.channel_values")
            return values[channel]
        async with self.resource.handle.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT type, blob FROM checkpoint_blobs WHERE thread_id = %s AND channel = %s "
                "ORDER BY version DESC LIMIT 1",
                (thread_id, channel),
            )
            row = await cur.fetchone()
        assert row is not None
        return self.saver.serde.loads_typed((row["type"], row["blob"]))

    async def drop_value(self, thread_id: str, digest: str) -> None:
        if self.provider == "redis":
            await self.resource.redis_client.delete(f"{REDIS_VALUE_KEY_PREFIX}{{{thread_id}}}:{digest}")
            return
        async with self.resource.handle.connection() as conn:
            await conn.execute(
                "DELETE FROM tai42_checkpoint_values WHERE thread_id = %s AND digest = %s",
                (thread_id, bytes.fromhex(digest)),
            )

    async def stored_bytes(self, thread_id: str) -> int:
        """What the thread occupies: Redis key memory, or the bytes of its Postgres rows."""
        if self.provider == "redis":
            client = self.resource.redis_client
            total = 0
            async for key in client.scan_iter(match=f"*{thread_id}*", count=1000):
                total += await client.memory_usage(key) or 0
            return total
        queries = [
            "SELECT coalesce(sum(octet_length(checkpoint::text)), 0) FROM checkpoints WHERE thread_id = %s",
            "SELECT coalesce(sum(octet_length(blob)), 0) FROM checkpoint_blobs WHERE thread_id = %s",
            "SELECT coalesce(sum(octet_length(blob)), 0) FROM checkpoint_writes WHERE thread_id = %s",
            "SELECT coalesce(sum(octet_length(blob)), 0) FROM tai42_checkpoint_values WHERE thread_id = %s",
        ]
        total = 0
        async with self.resource.handle.connection() as conn, conn.cursor() as cur:
            for query in queries:
                await cur.execute(query, (thread_id,))
                row = await cur.fetchone()
                total += int(next(iter(row.values())) if isinstance(row, dict) else row[0])
        return total


async def _open(
    provider: str, monkeypatch: pytest.MonkeyPatch, shared: str | None, size: str = "1024"
) -> tuple[Store, Any]:
    """Open ``provider`` with the switch set to ``shared`` (``None`` leaves it unset) and the size ``size``."""
    if shared is None:
        monkeypatch.delenv(SWITCH_ENV, raising=False)
    else:
        monkeypatch.setenv(SWITCH_ENV, shared)
    monkeypatch.setenv(SIZE_ENV, size)
    reset_all_settings()
    resource, closer = await create_checkpoint_resource(provider, _conn_string(provider))
    return Store(provider, get_saver_from_resource(provider, resource), resource), closer


@pytest.fixture(params=["redis", "postgres"])
async def store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Store]:
    opened, closer = await _open(str(request.param), monkeypatch, "true")
    try:
        yield opened
    finally:
        await closer()
        reset_all_settings()


def _doc(index: int, salt: str = "") -> dict[str, Any]:
    """A document above the 1 KiB threshold whose fields are each below it."""
    return {"title": f"document {index}{salt}", "a": f"{index}{salt}" + "a" * 600, "b": "b" * 600}


DOCS = {f"d{i}": _doc(i) for i in range(3)}


def _config(thread_id: str) -> Any:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def _put(saver: Any, config: Any, previous: Any, values: dict[str, Any], changed: dict[str, str]) -> Any:
    checkpoint = empty_checkpoint() if previous is None else create_checkpoint(previous[0], None, previous[1] + 1)
    versions = dict(previous[0]["channel_versions"]) if previous is not None else {}
    versions.update(changed)
    checkpoint["channel_values"] = values
    checkpoint["channel_versions"] = versions
    step = 0 if previous is None else previous[1] + 1
    saved = await saver.aput(config, checkpoint, {**_METADATA, "step": step}, changed)
    return saved, (checkpoint, step)


async def test_a_value_above_the_threshold_is_stored_once_and_referenced(store: Store) -> None:
    thread_id = f"shared-{uuid.uuid4().hex}"
    try:
        await _put(store.saver, _config(thread_id), None, {"docs": DOCS, "n": 1}, {"docs": "1", "n": "1"})
        assert len(await store.digests(thread_id)) == 3
        stored = await store.stored_channel(thread_id, "docs")
        assert all(envelope_reference(item) is not None for item in stored.values())
        tup = await store.saver.aget_tuple(_config(thread_id))
        assert tup.checkpoint["channel_values"] == {"docs": DOCS, "n": 1}
    finally:
        await store.saver.adelete_thread(thread_id)


async def test_a_later_checkpoint_shares_the_unchanged_and_adds_only_the_changed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_kit.llm.checkpoint import redis_values

    thread_id = f"shared-{uuid.uuid4().hex}"
    config = _config(thread_id)
    walked: list[str] = []
    original_walk = redis_values._walk

    def counting_walk(value: Any, path: str) -> Any:
        walked.append(path)
        return original_walk(value, path)

    try:
        saved, previous = await _put(store.saver, config, None, {"docs": DOCS, "n": 1}, {"docs": "1", "n": "1"})
        monkeypatch.setattr(redis_values, "_walk", counting_walk)
        saved, previous = await _put(store.saver, saved, previous, {"docs": DOCS, "n": 2}, {"n": "2"})
        assert len(await store.digests(thread_id)) == 3
        if store.provider == "redis":
            assert "$.channel_values.docs" not in walked
            assert "$.channel_values.n" in walked
        changed = {**DOCS, "d1": _doc(1, "changed")}
        await _put(store.saver, saved, previous, {"docs": changed, "n": 3}, {"docs": "2", "n": "3"})
        assert len(await store.digests(thread_id)) == 4
        tuples = [t async for t in store.saver.alist(config)]
        assert [t.checkpoint["channel_values"] for t in tuples] == [
            {"docs": changed, "n": 3},
            {"docs": DOCS, "n": 2},
            {"docs": DOCS, "n": 1},
        ]
    finally:
        await store.saver.adelete_thread(thread_id)


async def test_a_pending_write_above_the_threshold_round_trips_and_shares_its_value(store: Store) -> None:
    thread_id = f"shared-{uuid.uuid4().hex}"
    config = _config(thread_id)
    written = {"d9": _doc(9)}
    try:
        saved, previous = await _put(store.saver, config, None, {"n": 1}, {"n": "1"})
        await store.saver.aput_writes(saved, [("docs", written)], task_id="task-1")
        assert len(await store.digests(thread_id)) == 1
        tup = await store.saver.aget_tuple(config)
        assert tup.pending_writes == [("task-1", "docs", written)]
        await _put(store.saver, saved, previous, {"docs": written, "n": 2}, {"docs": "1", "n": "2"})
        assert len(await store.digests(thread_id)) == 1
        assert (await store.saver.aget_tuple(config)).checkpoint["channel_values"]["docs"] == written
    finally:
        await store.saver.adelete_thread(thread_id)


async def test_deleting_the_thread_removes_its_values(store: Store) -> None:
    thread_id = f"shared-{uuid.uuid4().hex}"
    saved, _previous = await _put(store.saver, _config(thread_id), None, {"docs": DOCS}, {"docs": "1"})
    await store.saver.aput_writes(saved, [("docs", {"d9": _doc(9)})], task_id="task-1")
    assert len(await store.digests(thread_id)) == 4
    await store.saver.adelete_thread(thread_id)
    assert await store.digests(thread_id) == set()
    assert await store.saver.aget_tuple(_config(thread_id)) is None
    if store.provider == "redis":
        assert not await store.resource.redis_client.exists(f"{REDIS_VALUE_INDEX_PREFIX}{{{thread_id}}}")


async def test_a_value_removed_behind_the_savers_back_fails_every_read(store: Store) -> None:
    thread_id = f"shared-{uuid.uuid4().hex}"
    await _put(store.saver, _config(thread_id), None, {"docs": DOCS}, {"docs": "1"})
    digest = sorted(await store.digests(thread_id))[0]
    await store.drop_value(thread_id, digest)
    try:
        with pytest.raises(CheckpointReferenceError, match=f"references a value {digest} the store does not hold"):
            await store.saver.aget_tuple(_config(thread_id))
        with pytest.raises(CheckpointReferenceError, match=digest):
            async for _ in store.saver.alist(_config(thread_id)):
                pass
        with pytest.raises(CheckpointReferenceError, match=digest):
            await asyncio.to_thread(store.saver.get_tuple, _config(thread_id))
    finally:
        await store.saver.adelete_thread(thread_id)


# --------------------------------------------------------------------------- #
# Postgres: a large string channel, which the library keeps inline in every checkpoint row
# --------------------------------------------------------------------------- #
_TEXT = "".join(f"paragraph {index}. " for index in range(400))


async def _checkpoint_rows(store: Store, thread_id: str) -> list[str]:
    async with store.resource.handle.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT checkpoint::text AS c FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id", (thread_id,)
        )
        rows = await cur.fetchall()
    return [row["c"] for row in rows]


async def test_a_large_string_channel_is_stored_once_on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    opened, closer = await _open("postgres", monkeypatch, "true")
    thread_id = f"text-{uuid.uuid4().hex}"
    config = _config(thread_id)
    try:
        saved, previous = await _put(opened.saver, config, None, {"text": _TEXT, "n": 0}, {"text": "1", "n": "1"})
        for step in range(1, 4):
            saved, previous = await _put(
                opened.saver, saved, previous, {"text": _TEXT, "n": step}, {"n": str(step + 1)}
            )
        rows = await _checkpoint_rows(opened, thread_id)
        assert len(rows) == 4
        assert all("paragraph 399" not in row for row in rows)
        assert len(await opened.digests(thread_id)) == 1
        tuples = [t async for t in opened.saver.alist(config)]
        assert [t.checkpoint["channel_values"] for t in tuples] == [{"text": _TEXT, "n": n} for n in (3, 2, 1, 0)]
    finally:
        await opened.saver.adelete_thread(thread_id)
        await closer()
        reset_all_settings()


async def test_a_string_channel_stored_inline_is_shared_at_the_next_checkpoint_on_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inline, close_inline = await _open("postgres", monkeypatch, "false")
    thread_id = f"text-{uuid.uuid4().hex}"
    config = _config(thread_id)
    saved, previous = await _put(inline.saver, config, None, {"text": _TEXT, "n": 0}, {"text": "1", "n": "1"})
    await close_inline()
    shared, close_shared = await _open("postgres", monkeypatch, "true")
    try:
        await _put(shared.saver, saved, previous, {"text": _TEXT, "n": 1}, {"n": "2"})
        first, second = await _checkpoint_rows(shared, thread_id)
        assert "paragraph 399" in first
        assert "paragraph 399" not in second
        tuples = [t async for t in shared.saver.alist(config)]
        assert [t.checkpoint["channel_values"] for t in tuples] == [{"text": _TEXT, "n": 1}, {"text": _TEXT, "n": 0}]
    finally:
        await shared.saver.adelete_thread(thread_id)
        await close_shared()
        reset_all_settings()


# --------------------------------------------------------------------------- #
# Redis retention: the waiting TTL on every value, re-armed by a referencing write
# --------------------------------------------------------------------------- #
@pytest.fixture
async def redis_store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Store]:
    opened, closer = await _open("redis", monkeypatch, "true")
    try:
        yield opened
    finally:
        await closer()
        reset_all_settings()


async def _value_ttls(store: Store, thread_id: str) -> dict[str, int]:
    client = store.resource.redis_client
    keys = [key async for key in client.scan_iter(match=f"tai42:checkpoint:value*{{{thread_id}}}*", count=1000)]
    return {(key.decode() if isinstance(key, bytes) else key): await client.ttl(key) for key in keys}


async def test_every_value_and_the_index_carry_the_waiting_ttl_and_a_write_rearms_them(redis_store: Store) -> None:
    thread_id = f"ttl-{uuid.uuid4().hex}"
    waiting = 10080 * 60
    client = redis_store.resource.redis_client
    try:
        saved, previous = await _put(redis_store.saver, _config(thread_id), None, {"docs": DOCS}, {"docs": "1"})
        ttls = await _value_ttls(redis_store, thread_id)
        assert len(ttls) == 4  # three values and the index
        assert all(waiting - 60 <= ttl <= waiting for ttl in ttls.values()), ttls
        for key in ttls:
            await client.expire(key, 100)
        await _put(redis_store.saver, saved, previous, {"docs": DOCS, "n": 1}, {"n": "1"})
        rearmed = await _value_ttls(redis_store, thread_id)
        assert all(waiting - 60 <= ttl <= waiting for ttl in rearmed.values()), rearmed
    finally:
        await redis_store.saver.adelete_thread(thread_id)


async def test_a_view_stamps_its_own_waiting_on_the_values_it_writes(redis_store: Store) -> None:
    from tai42_kit.llm.checkpoint.checkpoint import create_redis_saver_view

    thread_id = f"view-{uuid.uuid4().hex}"
    view = await create_redis_saver_view(redis_store.resource, 2)
    try:
        await _put(view, _config(thread_id), None, {"docs": DOCS}, {"docs": "1"})
        ttls = await _value_ttls(redis_store, thread_id)
        assert len(ttls) == 4
        assert all(60 < ttl <= 120 for ttl in ttls.values()), ttls
        assert (await redis_store.saver.aget_tuple(_config(thread_id))).checkpoint["channel_values"]["docs"] == DOCS
    finally:
        await view.adelete_thread(thread_id)


async def test_the_values_expire_with_the_threads_retention(redis_store: Store) -> None:
    from tai42_kit.llm.checkpoint import checkpoint as cp
    from tai42_kit.llm.checkpoint.codec import GuardedAsyncRedisSaver

    thread_id = f"expiry-{uuid.uuid4().hex}"
    short = GuardedAsyncRedisSaver(
        redis_client=redis_store.resource.redis_client,
        ttl={"default_ttl": 0.05, "refresh_on_read": False},
        shared_value_threshold=cp.shared_value_threshold("redis"),
    )
    cp._guard_saver_serialization(short, cp.checkpoint_provider_facts("redis"))
    await short.asetup()
    saved, _previous = await _put(short, _config(thread_id), None, {"docs": DOCS}, {"docs": "1"})
    await short.aput_writes(saved, [("docs", {"d9": _doc(9)})], task_id="task-1")
    assert len(await redis_store.digests(thread_id)) == 4
    await asyncio.sleep(4.5)
    assert await _value_ttls(redis_store, thread_id) == {}
    assert await short.aget_tuple(_config(thread_id)) is None


# --------------------------------------------------------------------------- #
# Postgres: the delta channel history resolves its shared values
# --------------------------------------------------------------------------- #
class _SnapshotState(TypedDict):
    items: Annotated[list, DeltaChannel(_extend, snapshot_frequency=1)]
    note: Annotated[list, operator.add]


def _snapshot_first(state: _SnapshotState) -> dict[str, Any]:
    return {"items": ["a"], "note": ["first"]}


def _snapshot_second(state: _SnapshotState) -> dict[str, Any]:
    from langgraph.types import interrupt

    return {"items": [interrupt("continue?")], "note": ["second"]}


def _snapshot_third(state: _SnapshotState) -> dict[str, Any]:
    return {"items": ["c"], "note": ["third"]}


def _snapshotting_delta_graph() -> Any:
    """A delta-channel graph that snapshots the channel at every step."""
    builder = StateGraph(_SnapshotState)
    builder.add_node("first", _snapshot_first)
    builder.add_node("second", _snapshot_second)
    builder.add_node("third", _snapshot_third)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", "third")
    builder.add_edge("third", END)
    return builder


async def test_a_delta_channel_graph_rebuilds_its_shared_values_on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    opened, closer = await _open("postgres", monkeypatch, "true")
    big = "p" * 3000

    async def run(checkpointer: Any, thread_id: str) -> Any:
        from langgraph.types import Command

        graph = _snapshotting_delta_graph().compile(checkpointer=checkpointer)
        config: Any = {"configurable": {"thread_id": thread_id}}
        await graph.ainvoke({"items": [big], "note": []}, config)
        return await graph.ainvoke(Command(resume=big + "b"), config)

    thread_id = f"delta-{uuid.uuid4().hex}"
    try:
        final = await run(opened.saver, thread_id)
        assert await opened.digests(thread_id)
        async with opened.resource.handle.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT type FROM checkpoint_blobs WHERE thread_id = %s AND channel = 'items'", (thread_id,)
            )
            types = {row["type"] for row in await cur.fetchall()}
        assert any(type_.endswith("+ref") for type_ in types), types
        assert final == await run(InMemorySaver(), "reference")
        assert final["items"] == [big, "a", big + "b", "c"]
    finally:
        await opened.saver.adelete_thread(thread_id)
        await closer()
        reset_all_settings()


# --------------------------------------------------------------------------- #
# The size proof: a thread shaped like a long conversation, sharing on against off
# --------------------------------------------------------------------------- #
def _incompressible(rng: random.Random, size: int) -> str:
    return base64.b64encode(rng.randbytes(size)).decode()


# The raw sizes of the eight documents; their JSON is about a third larger. "uniform": every document
# just above the default threshold, so each is shared on its own — the best case. "mixed": documents
# spread across 33-75 KB, so those below the default stay inside their dictionary's shared value.
DOCUMENT_SIZES = {
    "uniform": [50_000] * 8,
    "mixed": [25_000 + index * 4_400 for index in range(8)],
}


async def _conversation(store: Store, thread_id: str, steps: int, sizes: list[int]) -> list[float]:
    """``steps`` checkpoints: a constant result, a dictionary of documents with one changed per step, and
    small bookkeeping values changed every step; a pending write per step. Returns each aput's CPU time."""
    rng = random.Random(7)
    documents = {f"doc{i}": {"index": i, "body": _incompressible(rng, size)} for i, size in enumerate(sizes)}
    result = {"rows": [_incompressible(rng, 2_000) for _ in range(100)]}
    config: Any = _config(thread_id)
    previous = None
    cpu: list[float] = []
    for step in range(steps):
        index = step % len(sizes)
        documents = {**documents, f"doc{index}": {"index": step, "body": _incompressible(rng, sizes[index])}}
        values = {
            "documents": documents,
            "result": result,
            "context": {"step": step, "notes": _incompressible(rng, 1_500)},
        }
        changed = {"documents": str(step + 1), "context": str(step + 1)}
        if step == 0:
            changed["result"] = "1"
        start = time.process_time()
        config, previous = await _put(store.saver, config, previous, values, changed)
        cpu.append(time.process_time() - start)
        await store.saver.aput_writes(config, [("context", {"step": step})], task_id=f"task-{step}")
    tup = await store.saver.aget_tuple(_config(thread_id))
    assert tup.checkpoint["channel_values"]["documents"] == documents
    assert tup.checkpoint["channel_values"]["result"] == result
    return cpu


@pytest.mark.parametrize("shape", list(DOCUMENT_SIZES))
@pytest.mark.parametrize("provider", ["redis", "postgres"])
async def test_a_conversation_stores_less_with_its_values_shared(
    provider: str, shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: dict[str, int] = {}
    for label, shared in (("shared", "true"), ("inline", "false")):
        opened, closer = await _open(provider, monkeypatch, shared, "65536")
        thread_id = f"size-{label}-{uuid.uuid4().hex}"
        try:
            cpu = await _conversation(opened, thread_id, 30, DOCUMENT_SIZES[shape])
            sizes[label] = await opened.stored_bytes(thread_id)
            print(  # the measurement is reported, not asserted
                f"{provider} {shape} {label}: {sizes[label]} bytes stored, aput CPU mean "
                f"{sum(cpu) / len(cpu) * 1000:.2f} ms, max {max(cpu) * 1000:.2f} ms"
            )
        finally:
            await opened.saver.adelete_thread(thread_id)
            await closer()
    reset_all_settings()
    if shape == "uniform":
        assert sizes["shared"] <= sizes["inline"] / 4, sizes
    else:
        assert sizes["shared"] < sizes["inline"], sizes


# --------------------------------------------------------------------------- #
# Each store's default, the switch, and a thread read back after the switch changed
# --------------------------------------------------------------------------- #
async def test_postgres_stores_every_value_inline_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from tai42_kit.llm.checkpoint.postgres_saver import PostgresMsgpackValueEncoding

    opened, closer = await _open("postgres", monkeypatch, None)
    thread_id = f"default-{uuid.uuid4().hex}"
    config = _config(thread_id)
    handed: list[Any] = []
    encodes: list[int] = []
    library_aput = AsyncPostgresSaver.aput
    original_encode = PostgresMsgpackValueEncoding.encode

    async def spy_aput(self: Any, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        handed.append(checkpoint)
        return await library_aput(self, config, checkpoint, metadata, new_versions)

    def counting_encode(self: Any, value: Any) -> Any:
        encodes.append(1)
        return original_encode(self, value)

    monkeypatch.setattr(AsyncPostgresSaver, "aput", spy_aput)
    monkeypatch.setattr(PostgresMsgpackValueEncoding, "encode", counting_encode)
    try:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"docs": DOCS, "n": 1}
        checkpoint["channel_versions"] = {"docs": "1", "n": "1"}
        await opened.saver.aput(config, checkpoint, _METADATA, {"docs": "1", "n": "1"})
        assert handed == [checkpoint]
        assert handed[0] is checkpoint
        assert encodes == []
        async with opened.resource.handle.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT type FROM checkpoint_blobs WHERE thread_id = %s AND channel = 'docs'", (thread_id,)
            )
            assert [row["type"] for row in await cur.fetchall()] == ["msgpack"]
        assert await opened.digests(thread_id) == set()
        assert (await opened.saver.aget_tuple(config)).checkpoint["channel_values"] == {"docs": DOCS, "n": 1}
        assert [t.checkpoint["channel_values"] async for t in opened.saver.alist(config)] == [{"docs": DOCS, "n": 1}]
    finally:
        await opened.saver.adelete_thread(thread_id)
        await closer()
        reset_all_settings()


async def test_redis_shares_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    opened, closer = await _open("redis", monkeypatch, None)
    thread_id = f"default-{uuid.uuid4().hex}"
    try:
        await _put(opened.saver, _config(thread_id), None, {"docs": DOCS}, {"docs": "1"})
        assert len(await opened.digests(thread_id)) == 3
        stored = await opened.stored_channel(thread_id, "docs")
        assert all(envelope_reference(item) is not None for item in stored.values())
    finally:
        await opened.saver.adelete_thread(thread_id)
        await closer()
        reset_all_settings()


async def test_redis_with_the_switch_off_stores_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.llm.checkpoint.value_store import RedisCheckpointValueStore

    opened, closer = await _open("redis", monkeypatch, "false", "65536")
    thread_id = f"off-{uuid.uuid4().hex}"
    calls: list[str] = []
    original_touch = RedisCheckpointValueStore.touch
    original_put = RedisCheckpointValueStore.put

    async def counting_touch(self: Any, *args: Any) -> Any:
        calls.append("touch")
        return await original_touch(self, *args)

    async def counting_put(self: Any, *args: Any) -> Any:
        calls.append("put")
        return await original_put(self, *args)

    monkeypatch.setattr(RedisCheckpointValueStore, "touch", counting_touch)
    monkeypatch.setattr(RedisCheckpointValueStore, "put", counting_put)
    large = {"pad": "z" * 70_000}
    try:
        saved, _previous = await _put(opened.saver, _config(thread_id), None, {"v": large}, {"v": "1"})
        await opened.saver.aput_writes(saved, [("v", large)], task_id="task-1")
        stored = await opened.stored_channel(thread_id, "v")
        assert stored["args"][0] == "inflate"
        assert await opened.digests(thread_id) == set()
        assert calls == []
        assert (await opened.saver.aget_tuple(_config(thread_id))).checkpoint["channel_values"] == {"v": large}
    finally:
        await opened.saver.adelete_thread(thread_id)
        await closer()
        reset_all_settings()


@pytest.mark.parametrize("provider", ["redis", "postgres"])
async def test_a_thread_written_with_sharing_on_reads_back_with_it_off(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    on, close_on = await _open(provider, monkeypatch, "true")
    thread_id = f"switch-{uuid.uuid4().hex}"
    config = _config(thread_id)
    saved, previous = await _put(on.saver, config, None, {"docs": DOCS, "n": 1}, {"docs": "1", "n": "1"})
    await on.saver.aput_writes(saved, [("docs", {"d9": _doc(9)})], task_id="task-1")
    await _put(on.saver, saved, previous, {"docs": DOCS, "n": 2}, {"n": "2"})
    await close_on()
    off, close_off = await _open(provider, monkeypatch, "false")
    try:
        assert await off.digests(thread_id)
        tup = await off.saver.aget_tuple(config)
        assert tup.checkpoint["channel_values"] == {"docs": DOCS, "n": 2}
        listed = [t async for t in off.saver.alist(config)]
        assert [t.checkpoint["channel_values"] for t in listed] == [{"docs": DOCS, "n": 2}, {"docs": DOCS, "n": 1}]
        assert listed[1].pending_writes == [("task-1", "docs", {"d9": _doc(9)})]
        if provider == "postgres":
            history = await off.saver.aget_delta_channel_history(config=listed[0].config, channels=["docs"])
            assert all(envelope_reference(item) is None for item in _leaves(history))
        await off.saver.adelete_thread(thread_id)
        assert await off.digests(thread_id) == set()
    finally:
        await off.saver.adelete_thread(thread_id)
        await close_off()
        reset_all_settings()


def _leaves(value: Any) -> list[Any]:
    """Every dictionary inside ``value``."""
    if isinstance(value, dict):
        return [value, *(leaf for item in value.values() for leaf in _leaves(item))]
    if isinstance(value, (list, tuple)):
        return [leaf for item in value for leaf in _leaves(item)]
    return []


@pytest.mark.parametrize("provider", ["redis", "postgres"])
async def test_a_read_without_references_costs_only_the_savers_own_read(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reports the wall per ``aget_tuple`` of a reference-free checkpoint, with the switch on and off."""
    for shared in ("true", "false"):
        opened, closer = await _open(provider, monkeypatch, shared, "65536")
        thread_id = f"read-{shared}-{uuid.uuid4().hex}"
        try:
            await _put(opened.saver, _config(thread_id), None, {"docs": DOCS, "n": 1}, {"docs": "1", "n": "1"})
            start = time.perf_counter()
            for _ in range(50):
                await opened.saver.aget_tuple(_config(thread_id))
            print(  # the measurement is reported, not asserted
                f"{provider} switch {shared}: aget_tuple without references "
                f"{(time.perf_counter() - start) / 50 * 1000:.2f} ms"
            )
        finally:
            await opened.saver.adelete_thread(thread_id)
            await closer()
    reset_all_settings()


@pytest.mark.parametrize("held_by", ["channel", "pending write"])
async def test_alist_resolves_a_reference_held_only_by_the_oldest_tuple(store: Store, held_by: str) -> None:
    thread_id = f"oldest-{uuid.uuid4().hex}"
    config = _config(thread_id)
    try:
        if held_by == "channel":
            saved, previous = await _put(store.saver, config, None, {"docs": DOCS}, {"docs": "1"})
        else:
            saved, previous = await _put(store.saver, config, None, {"n": 0}, {"n": "1"})
            await store.saver.aput_writes(saved, [("docs", DOCS)], task_id="task-1")
        saved, previous = await _put(store.saver, saved, previous, {"n": 1}, {"n": "2"})
        await _put(store.saver, saved, previous, {"n": 2}, {"n": "3"})
        listed = [t async for t in store.saver.alist(config)]
        assert len(listed) == 3
        oldest = listed[-1]
        if held_by == "channel":
            assert oldest.checkpoint["channel_values"] == {"docs": DOCS}
        else:
            assert oldest.pending_writes == [("task-1", "docs", DOCS)]
    finally:
        await store.saver.adelete_thread(thread_id)
