"""The shared-values saver every store's saver is wrapped in, offline: writes, the read collector, list reads,
the synchronous doors and a waiting view's value store.

The inner savers here are fakes or the in-process store; the value stores are spies. Nothing names a store.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Collection, Iterator, Mapping
from typing import Any, ClassVar, cast

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.base import CheckpointTuple, empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from tai42_kit.llm.checkpoint import (
    CheckpointCodec,
    CheckpointProvider,
    CheckpointProviderFacts,
    CheckpointReferenceError,
    CheckpointResource,
    CheckpointSerializationError,
    CheckpointWrite,
    MsgpackValueEncoding,
    StoredBlob,
    checkpoint_provider,
    create_saver_with_waiting,
    get_saver_from_resource,
    providers,
    register_checkpoint_provider,
)
from tai42_kit.llm.checkpoint.checkpoint import _GuardedSerializer
from tai42_kit.llm.checkpoint.chunking import ValueChunker
from tai42_kit.llm.checkpoint.reads import record_failure, record_reference
from tai42_kit.llm.checkpoint.shared_values import SharedValuesSaver
from tai42_kit.llm.checkpoint.stores.memory import MemoryFinishedThreadLedger
from tai42_kit.settings import reset_all_settings

THRESHOLD = 1024
DOC = {"title": "document", "a": "a" * 600, "b": "b" * 600}


class SpyValues:
    """A value store recording every call."""

    def __init__(self) -> None:
        self.held: dict[str, dict[str, StoredBlob]] = {}
        self.calls: list[str] = []

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        self.calls.append("touch")
        return {digest for digest in digests if digest not in self.held.get(thread_id, {})}

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        self.calls.append("put")
        self.held.setdefault(thread_id, {}).update(blobs)

    async def get(self, thread_id: str, digests: Collection[str]) -> dict[str, StoredBlob]:
        held = self.held.get(thread_id, {})
        return {digest: held[digest] for digest in digests if digest in held}

    async def held_digests(self, thread_id: str) -> set[str]:
        return set(self.held.get(thread_id, {}))

    async def delete_thread(self, thread_id: str) -> None:
        self.calls.append("delete_thread")
        self.held.pop(thread_id, None)


def _encoding() -> MsgpackValueEncoding:
    return MsgpackValueEncoding(_GuardedSerializer(JsonPlusSerializer()), None)


def _config(thread_id: str = "t1") -> Any:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


class FakeInner:
    """An inner saver recording the writes it is handed and answering reads from what a test gives it."""

    def __init__(self) -> None:
        self.serde = _GuardedSerializer(JsonPlusSerializer())
        self.puts: list[Any] = []
        self.deleted: list[str] = []
        self.tuple: CheckpointTuple | None = None
        self.on_read: Any = None
        self.listed: list[tuple[CheckpointTuple, Any]] = []

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        self.puts.append((checkpoint, new_versions))
        return config

    async def aput_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        self.puts.append(list(writes))

    async def aget_tuple(self, config: Any) -> CheckpointTuple | None:
        if self.on_read is not None:
            self.on_read()
        return self.tuple

    async def alist(self, config: Any, **_: Any) -> AsyncIterator[CheckpointTuple]:
        for tup, announce in self.listed:
            if announce is not None:
                announce()
            yield tup

    async def aget_delta_channel_history(self, *, config: Any, channels: Any) -> Any:
        if self.on_read is not None:
            self.on_read()
        return {"docs": {"writes": [], "seed": self.tuple.checkpoint["channel_values"]["docs"] if self.tuple else None}}

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


def _wrapper(inner: Any, values: SpyValues, threshold: int | None = THRESHOLD, provider: str = "memory") -> Any:
    return SharedValuesSaver(inner, checkpoint_provider(provider), cast("Any", values), threshold, _encoding())


def _stored_reference(values: SpyValues, thread_id: str = "t1") -> dict[str, Any]:
    """``DOC`` stored once in ``values`` for ``thread_id``; returns the reference that stands for it."""
    encoding = _encoding()
    chunker = ValueChunker(encoding, THRESHOLD)
    rewritten, _encoded = chunker.chunk({"doc": DOC})
    values.held.setdefault(thread_id, {}).update(
        {digest: encoding.store_form(blob) for digest, blob in chunker.blobs.items()}
    )
    return rewritten["doc"]


def _tuple(values: dict[str, Any], checkpoint_id: str = "c1", thread_id: str = "t1") -> CheckpointTuple:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = values
    return CheckpointTuple(
        config={"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": checkpoint_id}},
        checkpoint=checkpoint,
        metadata={},
        pending_writes=[],
    )


@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the reads the saver resolves."""
    from tai42_kit.llm.checkpoint import shared_values as shared_values_mod

    counted: list[int] = []
    for name in ("resolve_tuple", "resolve_references"):
        real = getattr(shared_values_mod, name)

        def counting(*args: Any, _real: Any = real, **kwargs: Any) -> Any:
            counted.append(1)
            return _real(*args, **kwargs)

        monkeypatch.setattr(shared_values_mod, name, counting)
    return counted


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
async def test_with_sharing_off_a_write_is_the_inner_savers_own():
    inner = FakeInner()
    values = SpyValues()
    saver = _wrapper(inner, values, threshold=None)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"doc": DOC}
    await saver.aput(_config(), checkpoint, {}, {"doc": 1})
    await saver.aput_writes(_config(), [("doc", DOC)], "task-1")
    assert inner.puts[0][0] is checkpoint
    assert inner.puts[1] == [("doc", DOC)]
    assert values.calls == []


async def test_a_stores_own_preparation_is_what_the_inner_saver_is_handed(registry):
    class Preparing(_Hooks):
        name: ClassVar[str] = "preparing-store"
        facts: ClassVar[CheckpointProviderFacts] = CheckpointProviderFacts(
            durable=False, retention="sweep", channel_storage="per_checkpoint"
        )

        def serializer(self, saver: Any, compress_threshold: int | None) -> tuple[Any, CheckpointCodec | None]:
            return saver.serde, None

        async def prepare_checkpoint(self, write: CheckpointWrite, checkpoint: Any, new_versions: Any) -> Any:
            await write.values.touch(write.thread_id, ["0" * 64])
            await write.store([], {})
            return {**checkpoint, "prepared": True}, {"prepared": 1}

    register_checkpoint_provider(Preparing)
    inner = FakeInner()
    values = SpyValues()
    saver = SharedValuesSaver(
        cast("Any", inner), checkpoint_provider("preparing-store"), cast("Any", values), THRESHOLD, _encoding()
    )
    await saver.aput(_config(), empty_checkpoint(), {}, {})
    prepared, versions = inner.puts[0]
    assert prepared["prepared"] is True
    assert versions == {"prepared": 1}
    assert values.calls == ["touch"]


async def test_the_copying_preparation_stores_a_value_once_and_then_only_rearms_it():
    inner = FakeInner()
    values = SpyValues()
    saver = _wrapper(inner, values, provider="sqlite")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"doc": DOC, "n": 1}
    await saver.aput(_config(), checkpoint, {}, {"doc": 1, "n": 1})
    assert values.calls == ["touch", "put"]
    assert len(values.held["t1"]) == 1
    await saver.aput(_config(), {**checkpoint, "id": "next"}, {}, {"n": 2})
    assert values.calls == ["touch", "put", "touch"]
    # The whole checkpoint is handed over encoded once, under the tag that announces a reference.
    handed = inner.puts[0][0]
    assert inner.serde.dumps_typed(handed)[0] == "msgpack+ref"


async def test_a_write_referencing_a_value_neither_held_nor_encoded_is_refused():
    values = SpyValues()
    write = CheckpointWrite(
        saver=cast("Any", FakeInner()),
        config=_config(),
        thread_id="t1",
        values=cast("Any", values),
        encoding=_encoding(),
        threshold=THRESHOLD,
    )
    digest = "a" * 64
    with pytest.raises(CheckpointReferenceError, match=f"thread 't1' references a value {digest} it does not hold"):
        await write.store([digest], {})
    assert values.calls == ["touch"]


# --------------------------------------------------------------------------- #
# The read collector
# --------------------------------------------------------------------------- #
async def test_a_decode_failure_the_read_recorded_raises_naming_the_thread(walks):
    inner = FakeInner()
    inner.tuple = _tuple({"n": 1})
    inner.on_read = lambda: record_failure(CheckpointSerializationError("broken"))
    with pytest.raises(CheckpointSerializationError, match=r"thread 't1' found 1 undecodable value\(s\): broken"):
        await _wrapper(inner, SpyValues()).aget_tuple(_config())


async def test_a_read_resolves_only_after_an_announcement(walks):
    values = SpyValues()
    reference = _stored_reference(values)
    inner = FakeInner()
    inner.tuple = _tuple({"doc": reference})
    saver = _wrapper(inner, values)
    unannounced = await saver.aget_tuple(_config())
    assert unannounced is inner.tuple
    assert walks == []
    inner.on_read = lambda: record_reference("msgpack")
    resolved = await saver.aget_tuple(_config())
    assert resolved.checkpoint["channel_values"]["doc"] == DOC
    assert len(walks) == 1


async def test_the_delta_history_resolves_what_its_read_announced(walks):
    values = SpyValues()
    inner = FakeInner()
    inner.tuple = _tuple({"docs": _stored_reference(values)})
    inner.on_read = lambda: record_reference("msgpack")
    history = await _wrapper(inner, values).aget_delta_channel_history(config=_config(), channels=["docs"])
    assert history["docs"]["seed"] == DOC


# --------------------------------------------------------------------------- #
# List reads: from the first announcement on, every tuple is resolved
# --------------------------------------------------------------------------- #
def _announce() -> None:
    record_reference("msgpack")


@pytest.mark.parametrize(
    ("announced_at", "resolved"),
    [(0, [True, True, True]), (2, [False, False, True]), (None, [False, False, False])],
)
async def test_a_list_read_resolves_from_its_first_announcement_on(walks, announced_at, resolved):
    values = SpyValues()
    reference = _stored_reference(values)
    inner = FakeInner()
    inner.listed = [
        (_tuple({"doc": reference}, f"c{index}"), _announce if index == announced_at else None) for index in range(3)
    ]
    tuples = [tup async for tup in _wrapper(inner, values).alist(_config())]
    assert [tup.checkpoint["channel_values"]["doc"] == DOC for tup in tuples] == resolved
    assert len(walks) == resolved.count(True)


# --------------------------------------------------------------------------- #
# The synchronous doors run the asynchronous ones on the loop the saver was built on
# --------------------------------------------------------------------------- #
@pytest.fixture
async def memory_saver(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUES", "true")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUE_BYTES", str(THRESHOLD))
    reset_all_settings()
    resource = CheckpointResource("memory", InMemorySaver(), MemoryFinishedThreadLedger(), cast("Any", SpyValues()))
    yield get_saver_from_resource("memory", resource), resource
    reset_all_settings()


async def test_the_sync_doors_serve_resolved_tuples_from_another_thread(memory_saver):
    saver, resource = memory_saver
    thread_id = f"sync-{uuid.uuid4().hex}"
    version = saver.get_next_version(None, None)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"doc": DOC}
    checkpoint["channel_versions"] = {"doc": version}
    await asyncio.to_thread(saver.put, _config(thread_id), checkpoint, {}, {"doc": version})
    assert "put" in resource.values.calls
    tup = await asyncio.to_thread(saver.get_tuple, _config(thread_id))
    assert tup.checkpoint["channel_values"]["doc"] == DOC
    listed = await asyncio.to_thread(lambda: list(saver.list(_config(thread_id))))
    assert [item.checkpoint["channel_values"]["doc"] for item in listed] == [DOC]
    await asyncio.to_thread(saver.delete_thread, thread_id)
    assert await resource.values.held_digests(thread_id) == set()


async def test_a_sync_door_on_the_loops_own_thread_is_refused(memory_saver):
    saver, _resource = memory_saver
    with pytest.raises(asyncio.InvalidStateError, match="only allowed from a different thread"):
        saver.get_tuple(_config())


# --------------------------------------------------------------------------- #
# A waiting view writes its values through the value store its registration hands with it
# --------------------------------------------------------------------------- #
class _Hooks(CheckpointProvider):
    async def open(self, conn_string: str | None) -> Any:
        raise NotImplementedError

    def saver(self, resource: Any) -> Any:
        return resource.handle

    def value_encoding(self, saver: Any, compress_threshold: int | None) -> Any:
        return MsgpackValueEncoding(saver.serde, compress_threshold)

    async def recorded_generation(self, resource: Any) -> int | None:
        return None

    async def record_generation(self, resource: Any, generation: int) -> None:
        return None

    async def holds_checkpoints(self, resource: Any) -> bool:
        return False


@pytest.fixture
def registry() -> Iterator[None]:
    held = dict(providers._PROVIDERS._generation.committed())
    instances = dict(providers._instances)
    yield
    committed = providers._PROVIDERS._generation.committed()
    committed.clear()
    committed.update(held)
    providers._instances.clear()
    providers._instances.update(instances)


async def test_a_waiting_view_writes_its_values_through_its_own_value_store(registry, monkeypatch):
    view_values = SpyValues()
    waited: list[int] = []

    class Keyed(_Hooks):
        name: ClassVar[str] = "keyed-store"
        facts: ClassVar[CheckpointProviderFacts] = CheckpointProviderFacts(
            durable=True, retention="native_ttl", channel_storage="per_checkpoint"
        )

        async def saver_with_waiting(self, resource: Any, waiting_minutes: int) -> Any:
            waited.append(waiting_minutes)
            return InMemorySaver(), view_values

    register_checkpoint_provider(Keyed)
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_SHARED_VALUE_BYTES", str(THRESHOLD))
    reset_all_settings()
    own_values = SpyValues()
    resource = CheckpointResource("keyed-store", InMemorySaver(), MemoryFinishedThreadLedger(), cast("Any", own_values))
    try:
        view = await create_saver_with_waiting("keyed-store", resource, 2)
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"doc": DOC}
        await view.aput(_config(), checkpoint, {}, {"doc": view.get_next_version(None, None)})
        own = get_saver_from_resource("keyed-store", resource)
        await own.aput(_config("t2"), checkpoint, {}, {"doc": own.get_next_version(None, None)})
    finally:
        reset_all_settings()
    assert waited == [2]
    assert view_values.calls == ["touch", "put"]
    assert list(view_values.held) == ["t1"]
    assert list(own_values.held) == ["t2"]


class _Forwarding(FakeInner):
    """An inner saver recording the rest of the contract the wrapper forwards."""

    def __init__(self) -> None:
        super().__init__()
        self.forwarded: list[tuple[str, Any]] = []
        self.config_specs: list[Any] = []

    async def adelete_for_runs(self, run_ids: Any) -> None:
        self.forwarded.append(("delete_for_runs", run_ids))

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        self.forwarded.append(("copy_thread", (source_thread_id, target_thread_id)))

    async def aprune(self, thread_ids: Any, *, strategy: str = "keep_latest") -> None:
        self.forwarded.append(("prune", (thread_ids, strategy)))


async def test_the_rest_of_the_contract_reaches_the_inner_saver_from_either_door():
    inner = _Forwarding()
    saver = _wrapper(cast("Any", inner), SpyValues())
    await saver.adelete_for_runs(["r1"])
    await saver.acopy_thread("a", "b")
    await saver.aprune(["t1"], strategy="delete")
    await asyncio.to_thread(saver.delete_for_runs, ["r2"])
    await asyncio.to_thread(saver.copy_thread, "c", "d")
    await asyncio.to_thread(lambda: saver.prune(["t2"]))
    assert inner.forwarded == [
        ("delete_for_runs", ["r1"]),
        ("copy_thread", ("a", "b")),
        ("prune", (["t1"], "delete")),
        ("delete_for_runs", ["r2"]),
        ("copy_thread", ("c", "d")),
        ("prune", (["t2"], "keep_latest")),
    ]
    await asyncio.to_thread(saver.put_writes, _config(), [("v", 1)], "task-1")
    history = await asyncio.to_thread(lambda: saver.get_delta_channel_history(config=_config(), channels=["docs"]))
    assert history == {"docs": {"writes": [], "seed": None}}
    assert saver.config_specs == []


async def test_the_versioned_preparation_keeps_a_delta_snapshot_a_snapshot():
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    from tai42_kit.llm.checkpoint import SharedSnapshot

    inner = FakeInner()
    values = SpyValues()
    saver = _wrapper(inner, values, provider="memory")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"docs": _DeltaSnapshot([DOC]), "small": _DeltaSnapshot([1])}
    await saver.aput(_config(), checkpoint, {}, {"docs": 1, "small": 1})
    handed = inner.puts[0][0]["channel_values"]
    assert isinstance(handed["docs"], SharedSnapshot)
    assert handed["small"] is checkpoint["channel_values"]["small"]
    assert inner.serde.dumps_typed(handed["docs"])[0] == "msgpack+ref"
    assert len(values.held["t1"]) == 1
