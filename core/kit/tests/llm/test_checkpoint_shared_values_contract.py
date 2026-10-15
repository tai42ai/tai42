"""Large values stored once, the same contract on every registered checkpoint store, read through the seam only.

Every store registered at collection runs: the in-process, sqlite and probe stores always, real Redis
and Postgres when they are configured. Each assertion goes through the seam a registration provides
(the saver the kit builds, the store's value store), never through a store's own keys or tables.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint

from tai42_kit.llm.checkpoint import (
    CheckpointReferenceError,
    checkpoint_providers,
    create_checkpoint_resource,
    get_saver_from_resource,
)
from tai42_kit.settings import reset_all_settings

from ._real_redis import real_redis_url

SWITCH_ENV = "LLM_PROVIDER_CHECKPOINT_SHARED_VALUES"
SIZE_ENV = "LLM_PROVIDER_CHECKPOINT_SHARED_VALUE_BYTES"
COMPRESS_ENV = "LLM_PROVIDER_CHECKPOINT_INLINE_COMPRESS_BYTES"
_METADATA: dict[str, Any] = {"source": "loop", "step": 0, "parents": {}}
_REAL: frozenset[str] = frozenset({"redis", "postgres"})

# One document above the 1 KiB threshold whose fields are each below it: stored once, one digest.
DOC = {"title": "document", "a": "a" * 600, "b": "b" * 600}


def _conn_string(provider: str, tmp: Path) -> str | None:
    if provider == "sqlite":
        pytest.importorskip("aiosqlite")
        return str(tmp / "checkpoints.db")
    if provider == "redis":
        return real_redis_url()
    if provider == "postgres":
        if os.environ.get("TAI42_KIT_REAL_PG") not in ("1", "true", "True"):
            pytest.skip(
                "real-Postgres contract test is opt-in: set TAI42_KIT_REAL_PG=1 and the TAI_DATABASE_DEFAULT_PG_* env"
            )
        from tai42_kit.db import database_settings

        return database_settings("default").pg_dsn
    # The in-process stores take no connection; the probe stores keep one store per string.
    return f"contract-{uuid.uuid4().hex}" if provider.startswith("probe") else None


class Store:
    """One registered store opened with the switch set: its resource and the saver the kit builds."""

    def __init__(self, provider: str, conn_string: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
        self.provider = provider
        self.conn_string = conn_string
        self._monkeypatch = monkeypatch
        self.resource: Any = None
        self._closer: Any = None

    async def open(self, switch: str | None) -> Any:
        """Open the store (once) and build a saver under ``switch`` (``None`` leaves it unset)."""
        if switch is None:
            self._monkeypatch.delenv(SWITCH_ENV, raising=False)
        else:
            self._monkeypatch.setenv(SWITCH_ENV, switch)
        self._monkeypatch.setenv(SIZE_ENV, "1024")
        reset_all_settings()
        if self.resource is None:
            self.resource, self._closer = await create_checkpoint_resource(self.provider, self.conn_string)
        return get_saver_from_resource(self.provider, self.resource)

    async def close(self) -> None:
        if self._closer is not None:
            await self._closer()
        reset_all_settings()

    async def held(self, thread_id: str) -> set[str]:
        return await self.resource.values.held_digests(thread_id)


@pytest.fixture(
    params=[
        pytest.param(name, marks=pytest.mark.integration) if name in _REAL else name for name in checkpoint_providers()
    ]
)
async def store(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Store]:
    opened = Store(str(request.param), _conn_string(str(request.param), tmp_path), monkeypatch)
    try:
        yield opened
    finally:
        await opened.close()


def _config(thread_id: str) -> Any:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def _put(saver: Any, config: Any, previous: Any, values: dict[str, Any], changed: dict[str, Any]) -> Any:
    checkpoint = empty_checkpoint() if previous is None else create_checkpoint(previous[0], None, previous[1] + 1)
    versions = dict(previous[0]["channel_versions"]) if previous is not None else {}
    versions.update(changed)
    checkpoint["channel_values"] = values
    checkpoint["channel_versions"] = versions
    step = 0 if previous is None else previous[1] + 1
    saved = await saver.aput(config, checkpoint, {**_METADATA, "step": step}, changed)
    return saved, (checkpoint, step)


def _version(saver: Any, current: Any = None) -> Any:
    return saver.get_next_version(current, None)


async def test_a_large_value_is_held_once_and_read_back(store: Store) -> None:
    saver = await store.open("true")
    thread_id = f"contract-{uuid.uuid4().hex}"
    try:
        doc_version = _version(saver)
        saved, previous = await _put(
            saver, _config(thread_id), None, {"doc": DOC, "n": 1}, {"doc": doc_version, "n": doc_version}
        )
        held = await store.held(thread_id)
        assert len(held) == 1
        await _put(saver, saved, previous, {"doc": DOC, "n": 2}, {"n": _version(saver, doc_version)})
        assert await store.held(thread_id) == held
        latest = await saver.aget_tuple(_config(thread_id))
        assert latest.checkpoint["channel_values"] == {"doc": DOC, "n": 2}
        listed = [tup.checkpoint["channel_values"] async for tup in saver.alist(_config(thread_id))]
        assert listed == [{"doc": DOC, "n": 2}, {"doc": DOC, "n": 1}]
    finally:
        await saver.adelete_thread(thread_id)


async def test_a_pending_write_shares_the_value_its_thread_holds(store: Store) -> None:
    saver = await store.open("true")
    thread_id = f"contract-{uuid.uuid4().hex}"
    try:
        saved, _previous = await _put(saver, _config(thread_id), None, {"doc": DOC}, {"doc": _version(saver)})
        held = await store.held(thread_id)
        await saver.aput_writes(saved, [("doc", DOC)], task_id="task-1")
        assert await store.held(thread_id) == held
        tup = await saver.aget_tuple(_config(thread_id))
        assert [(channel, value) for _task, channel, value in tup.pending_writes] == [("doc", DOC)]
    finally:
        await saver.adelete_thread(thread_id)


async def test_a_value_kept_inline_above_the_inline_compress_bytes_reads_back(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(COMPRESS_ENV, "256")
    saver = await store.open("true")
    thread_id = f"contract-{uuid.uuid4().hex}"
    # The document is held once; the note stays inline, below the shared-value bytes and above the compress bytes.
    note = "n" * 900
    try:
        version = _version(saver)
        saved, _previous = await _put(
            saver, _config(thread_id), None, {"doc": DOC, "note": note}, {"doc": version, "note": version}
        )
        await saver.aput_writes(saved, [("note", note)], task_id="task-1")
        tup = await saver.aget_tuple(_config(thread_id))
        assert tup.checkpoint["channel_values"] == {"doc": DOC, "note": note}
        assert [(channel, value) for _task, channel, value in tup.pending_writes] == [("note", note)]
        listed = [item.checkpoint["channel_values"] async for item in saver.alist(_config(thread_id))]
        assert listed == [{"doc": DOC, "note": note}]
    finally:
        await saver.adelete_thread(thread_id)


async def test_deleting_the_thread_deletes_its_values(store: Store) -> None:
    saver = await store.open("true")
    thread_id = f"contract-{uuid.uuid4().hex}"
    await _put(saver, _config(thread_id), None, {"doc": DOC}, {"doc": _version(saver)})
    assert await store.held(thread_id)
    await saver.adelete_thread(thread_id)
    assert await store.held(thread_id) == set()
    assert await saver.aget_tuple(_config(thread_id)) is None


async def test_a_value_the_store_no_longer_holds_fails_the_read_loudly(store: Store) -> None:
    saver = await store.open("true")
    thread_id = f"contract-{uuid.uuid4().hex}"
    try:
        await _put(saver, _config(thread_id), None, {"doc": DOC}, {"doc": _version(saver)})
        [digest] = await store.held(thread_id)
        await store.resource.values.delete_thread(thread_id)
        with pytest.raises(CheckpointReferenceError, match=f"references a value {digest} the store does not hold"):
            await saver.aget_tuple(_config(thread_id))
        with pytest.raises(CheckpointReferenceError):
            [tup async for tup in saver.alist(_config(thread_id))]
    finally:
        await saver.adelete_thread(thread_id)


async def test_with_the_switch_off_nothing_is_held(store: Store) -> None:
    saver = await store.open("false")
    thread_id = f"contract-{uuid.uuid4().hex}"
    try:
        saved, _previous = await _put(saver, _config(thread_id), None, {"doc": DOC}, {"doc": _version(saver)})
        await saver.aput_writes(saved, [("doc", DOC)], task_id="task-1")
        assert await store.held(thread_id) == set()
        tup = await saver.aget_tuple(_config(thread_id))
        assert tup.checkpoint["channel_values"] == {"doc": DOC}
        assert tup.pending_writes[0][2] == DOC
    finally:
        await saver.adelete_thread(thread_id)


async def test_unset_follows_the_stores_channel_storage(store: Store) -> None:
    saver = await store.open(None)
    thread_id = f"contract-{uuid.uuid4().hex}"
    try:
        await _put(saver, _config(thread_id), None, {"doc": DOC}, {"doc": _version(saver)})
        copies = checkpoint_providers()[store.provider].channel_storage == "per_checkpoint"
        assert len(await store.held(thread_id)) == (1 if copies else 0)
    finally:
        await saver.adelete_thread(thread_id)


async def test_a_thread_written_with_sharing_on_reads_back_with_it_off(store: Store) -> None:
    writer = await store.open("true")
    thread_id = f"contract-{uuid.uuid4().hex}"
    try:
        saved, _previous = await _put(writer, _config(thread_id), None, {"doc": DOC}, {"doc": _version(writer)})
        await writer.aput_writes(saved, [("doc", DOC)], task_id="task-1")
        assert await store.held(thread_id)
        reader = await store.open("false")
        tup = await reader.aget_tuple(_config(thread_id))
        assert tup.checkpoint["channel_values"] == {"doc": DOC}
        assert tup.pending_writes[0][2] == DOC
        assert [t.checkpoint["channel_values"] async for t in reader.alist(_config(thread_id))] == [{"doc": DOC}]
        await reader.adelete_thread(thread_id)
        assert await store.held(thread_id) == set()
    finally:
        await writer.adelete_thread(thread_id)


@pytest.mark.parametrize("held_by", ["channel", "pending write"])
async def test_alist_resolves_a_reference_held_only_by_the_oldest_tuple(store: Store, held_by: str) -> None:
    saver = await store.open("true")
    thread_id = f"contract-{uuid.uuid4().hex}"
    try:
        first = _version(saver)
        values = {"doc": DOC, "n": 1} if held_by == "channel" else {"n": 1}
        saved, previous = await _put(saver, _config(thread_id), None, values, dict.fromkeys(values, first))
        if held_by == "pending write":
            await saver.aput_writes(saved, [("doc", DOC)], task_id="task-1")
        second = _version(saver, first)
        saved, previous = await _put(saver, saved, previous, {"n": 2}, {"n": second})
        await _put(saver, saved, previous, {"n": 3}, {"n": _version(saver, second)})
        tuples = [tup async for tup in saver.alist(_config(thread_id))]
        assert [tup.checkpoint["channel_values"]["n"] for tup in tuples] == [3, 2, 1]
        oldest = tuples[2]
        if held_by == "channel":
            assert oldest.checkpoint["channel_values"]["doc"] == DOC
        else:
            assert [(channel, value) for _task, channel, value in oldest.pending_writes] == [("doc", DOC)]
    finally:
        await saver.adelete_thread(thread_id)


async def test_a_sqlite_conversation_stores_less_with_its_values_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size proof on the copying development store: a large unchanged channel kept once, not per checkpoint."""
    pytest.importorskip("aiosqlite")
    sizes: dict[str, int] = {}
    for switch in ("true", "false"):
        opened = Store("sqlite", str(tmp_path / f"size-{switch}.db"), monkeypatch)
        saver = await opened.open(switch)
        thread_id = f"size-{switch}"
        try:
            docs = {f"d{i}": {"i": i, "a": f"{i}" + "a" * 4000, "b": "b" * 4000} for i in range(8)}
            saved, previous = await _put(saver, _config(thread_id), None, {"docs": docs, "n": 0}, {"docs": 1, "n": 1})
            for step in range(1, 10):
                saved, previous = await _put(saver, saved, previous, {"docs": docs, "n": step}, {"n": step + 1})
            conn = opened.resource.handle
            total = 0
            for query in (
                "SELECT coalesce(sum(length(checkpoint)), 0) FROM checkpoints WHERE thread_id = ?",
                "SELECT coalesce(sum(length(blob)), 0) FROM tai42_checkpoint_values WHERE thread_id = ?",
            ):
                async with conn.execute(query, (thread_id,)) as cursor:
                    total += int((await cursor.fetchone())[0])
            sizes[switch] = total
        finally:
            await opened.close()
    ratio = sizes["true"] / sizes["false"]
    print(f"sqlite, ten checkpoints of one unchanged 64 KiB channel: shared {sizes['true']} B, ", end="")
    print(f"inline {sizes['false']} B, x{ratio:.2f}")
    assert ratio < 0.2
