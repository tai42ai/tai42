"""The resolver, offline, over a fake value store: references restored, a missing or altered value fails loudly.

The Redis side is read the way the saver reads it (the serializer's reviver turns a ``ref`` envelope into
a reference); the Postgres side reads the msgpack blobs back as plain dictionaries.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Collection, Mapping
from typing import Any, cast

import orjson
import pytest

pytest.importorskip("langgraph.checkpoint.redis")

from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from tai42_kit.llm.checkpoint import CheckpointResource, get_saver_from_resource
from tai42_kit.llm.checkpoint.checkpoint import _GuardedSerializer
from tai42_kit.llm.checkpoint.chunking import (
    CheckpointReferenceError,
    ValueChunker,
    resolve_references,
    resolve_tuple,
)
from tai42_kit.llm.checkpoint.codec import CheckpointedException, FaithfulRedisSerializer
from tai42_kit.llm.checkpoint.envelope import BlobCompressCodec
from tai42_kit.llm.checkpoint.redis_values import RedisCheckpointEncoder, RedisJsonValueEncoding
from tai42_kit.llm.checkpoint.stores.memory import MemoryFinishedThreadLedger
from tai42_kit.llm.checkpoint.value_encoding import MsgpackValueEncoding
from tai42_kit.llm.checkpoint.value_store import StoredBlob

from .test_checkpoint_codec_redis import VALUES

THRESHOLD = 1024
THREAD = "thread-r1"


class FakeValueStore:
    """Blobs by digest; records every fetch."""

    def __init__(self, blobs: Mapping[str, StoredBlob]) -> None:
        self.blobs = dict(blobs)
        self.fetches: list[list[str]] = []

    async def get(self, digests: Collection[str]) -> Mapping[str, StoredBlob]:
        self.fetches.append(sorted(digests))
        return {digest: self.blobs[digest] for digest in digests if digest in self.blobs}


def _shaped(value: Any) -> dict[str, Any]:
    """``value`` inside a document above the threshold, beside a large string and a small scalar."""
    return {"document": {"v": value, "a": "a" * 700, "b": "b" * 700}, "text": "t" * 3000, "n": 1}


def _redis_encoder() -> RedisCheckpointEncoder:
    return RedisCheckpointEncoder(RedisJsonValueEncoding(FaithfulRedisSerializer(), 65536))


def _redis_tuple(values: dict[str, Any], writes: list[tuple[str, Any]]) -> tuple[CheckpointTuple, FakeValueStore, Any]:
    encoder = _redis_encoder()
    chunker = ValueChunker(encoder.encoding, THRESHOLD)
    checkpoint = {
        "v": 4,
        "id": "c1",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": values,
        "channel_versions": dict.fromkeys(values, "1"),
        "versions_seen": {},
        "updated_channels": None,
    }
    data = encoder.encode_checkpoint(checkpoint, {}, chunker)
    encoded_writes = [(channel, encoder.encode_write(value, chunker)) for channel, value in writes]
    serializer = FaithfulRedisSerializer()
    stored = serializer.loads_typed(("json", data))
    pending = [("task-1", channel, serializer.loads_typed(encoded)) for channel, encoded in encoded_writes]
    store = FakeValueStore({digest: encoder.encoding.store_form(enc) for digest, enc in chunker.blobs.items()})
    tup = CheckpointTuple(
        config={"configurable": {"thread_id": THREAD, "checkpoint_ns": "", "checkpoint_id": "c1"}},
        checkpoint=stored,
        metadata={"source": "input", "step": 0, "parents": {}},
        pending_writes=pending,
    )
    return tup, store, encoder.encoding


def _assert_same(got: Any, original: Any) -> None:
    if isinstance(original, BaseException):
        assert isinstance(got, CheckpointedException)
        assert str(got) == str(original)
        return
    assert type(got) is type(original)
    assert got == original


@pytest.mark.parametrize("name", [name for name in VALUES if name != "nested_bytes"])
async def test_every_faithful_value_is_restored_from_a_shared_document_on_redis(name):
    original = VALUES[name]
    tup, store, encoding = _redis_tuple({"v": _shaped(original)}, [("v", _shaped(original))])
    assert store.blobs, "the shaped value is above the threshold"
    resolved = await resolve_tuple(tup, encoding, store.get)
    assert resolved.pending_writes is not None
    for got in (resolved.checkpoint["channel_values"]["v"], resolved.pending_writes[0][2]):
        assert got["text"] == "t" * 3000
        assert got["n"] == 1
        _assert_same(got["document"]["v"], original)


async def test_a_value_with_nested_bytes_is_restored_on_redis():
    original = {"payload": b"\x00\x01binary", "buf": bytearray(b"\xff")}
    tup, store, encoding = _redis_tuple({"v": _shaped(original)}, [])
    resolved = await resolve_tuple(tup, encoding, store.get)
    assert resolved.checkpoint["channel_values"]["v"]["document"]["v"] == original


async def test_references_inside_a_blob_are_fetched_in_the_same_round_trip():
    inner = {"big": {"a": "a" * 700, "b": "b" * 700}, **{f"k{i}": "s" * 100 for i in range(30)}}
    tup, store, encoding = _redis_tuple({"outer": inner}, [])
    resolved = await resolve_tuple(tup, encoding, store.get)
    assert resolved.checkpoint["channel_values"]["outer"] == inner
    assert len(store.fetches) == 1
    assert len(store.fetches[0]) == 2


async def test_a_nested_value_missing_from_the_store_raises():
    inner = {"big": {"a": "a" * 700, "b": "b" * 700}, **{f"k{i}": "s" * 100 for i in range(30)}}
    tup, store, encoding = _redis_tuple({"outer": inner}, [])
    nested = hashlib.sha256(encoding.encode({"a": "a" * 700, "b": "b" * 700})[1]).hexdigest()
    del store.blobs[nested]
    with pytest.raises(CheckpointReferenceError, match=f"references a value {nested} the store does not hold"):
        await resolve_tuple(tup, encoding, store.get)


_DOCUMENT = {"p": "x" * 600, "q": "y" * 600, "k": "original"}


async def test_every_place_holding_the_same_value_gets_its_own_object_on_redis():
    nested = {"inner": copy.deepcopy(_DOCUMENT), **{f"k{i}": "s" * 100 for i in range(30)}}
    values = {"a": copy.deepcopy(_DOCUMENT), "b": copy.deepcopy(_DOCUMENT), "c": nested, "d": copy.deepcopy(nested)}
    tup, store, encoding = _redis_tuple(values, [("a", copy.deepcopy(_DOCUMENT))])
    assert len(store.blobs) == 2
    resolved = await resolve_tuple(tup, encoding, store.get)
    channels = resolved.checkpoint["channel_values"]
    assert resolved.pending_writes is not None
    written = resolved.pending_writes[0][2]
    places = [channels["a"], channels["b"], written, channels["c"]["inner"], channels["d"]["inner"]]
    assert len({id(place) for place in places}) == len(places)
    assert channels["c"] is not channels["d"]
    channels["a"]["k"] = "changed in place"
    channels["c"]["inner"]["k"] = "changed in place"
    assert channels["b"] == written == channels["d"]["inner"] == _DOCUMENT


async def test_every_place_holding_the_same_value_gets_its_own_object_on_postgres():
    encoding = _postgres_encoding()
    chunker = ValueChunker(encoding, THRESHOLD)
    rewritten, _ = chunker.chunk({"a": copy.deepcopy(_DOCUMENT), "b": copy.deepcopy(_DOCUMENT)})
    store = FakeValueStore({digest: encoding.store_form(enc) for digest, enc in chunker.blobs.items()})
    assert len(store.blobs) == 1
    resolved = await resolve_references(rewritten, encoding, store.get, THREAD)
    assert resolved["a"] is not resolved["b"]
    resolved["a"]["k"] = "changed in place"
    assert resolved["b"] == _DOCUMENT


async def test_a_tuple_without_references_is_returned_as_it_is():
    tup, store, encoding = _redis_tuple({"small": {"a": 1}}, [("small", 2)])
    assert await resolve_tuple(tup, encoding, store.get) is tup
    assert store.fetches == []


async def test_a_missing_value_raises_naming_the_thread_and_the_digest():
    tup, store, encoding = _redis_tuple({"v": _shaped(1)}, [])
    gone = sorted(store.blobs)[0]
    del store.blobs[gone]
    with pytest.raises(CheckpointReferenceError) as excinfo:
        await resolve_tuple(tup, encoding, store.get)
    assert str(excinfo.value) == (
        f"checkpoint thread {THREAD!r} references a value {gone} the store does not hold; "
        "the thread cannot be read — delete it"
    )


async def test_a_value_whose_length_is_not_its_references_raises():
    tup, store, encoding = _redis_tuple({"v": _shaped(1)}, [])
    digest = sorted(store.blobs)[0]
    store.blobs[digest] = StoredBlob("json", store.blobs[digest].data + b" ")
    with pytest.raises(CheckpointReferenceError, match=rf"value {digest} is \d+ bytes, its reference records"):
        await resolve_tuple(tup, encoding, store.get)


async def test_a_value_with_an_unknown_type_raises():
    tup, store, encoding = _redis_tuple({"v": _shaped(1)}, [])
    digest = sorted(store.blobs)[0]
    store.blobs[digest] = StoredBlob("pickle", store.blobs[digest].data)
    with pytest.raises(CheckpointReferenceError, match="unknown type 'pickle'"):
        await resolve_tuple(tup, encoding, store.get)


async def test_an_undecodable_shared_value_raises():
    tup, store, encoding = _redis_tuple({"v": _shaped(1)}, [])
    digest = sorted(store.blobs)[0]
    corrupt = orjson.dumps({"lc": 2, "type": "constructor", "id": ["os", "getcwd"], "args": []})
    store.blobs[digest] = StoredBlob("json", corrupt)
    tup = tup._replace(
        checkpoint={
            **tup.checkpoint,
            "channel_values": {"v": _ref_like(tup.checkpoint["channel_values"]["v"], digest, len(corrupt))},
        }
    )
    with pytest.raises(Exception, match="cannot be decoded"):
        await resolve_tuple(tup, encoding, store.get)


def _ref_like(value: Any, digest: str, size: int) -> Any:
    from tai42_kit.llm.checkpoint.chunking import CheckpointValueRef

    if isinstance(value, CheckpointValueRef) and value.digest == digest:
        return CheckpointValueRef(digest, size, value.closure)
    if isinstance(value, dict):
        return {key: _ref_like(item, digest, size) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------- #
# Postgres: references read back as plain dictionaries
# --------------------------------------------------------------------------- #
def _postgres_encoding() -> MsgpackValueEncoding:
    return MsgpackValueEncoding(_GuardedSerializer(JsonPlusSerializer(), BlobCompressCodec(2048)), 2048)


async def test_a_postgres_value_is_restored_and_a_large_blob_is_compressed():
    encoding = _postgres_encoding()
    chunker = ValueChunker(encoding, THRESHOLD)
    original = {"documents": {f"d{i}": {"a": f"{i}" * 700, "b": "b" * 700} for i in range(3)}, "big": "z" * 5000}
    rewritten, encoded = chunker.chunk(original)
    blobs = {digest: encoding.store_form(enc) for digest, enc in chunker.blobs.items()}
    assert any(blob.type == "msgpack+zlib" for blob in blobs.values())
    read_back = encoding.decode(StoredBlob(*encoded), _whole_ref(encoded), THREAD)
    assert read_back == rewritten
    store = FakeValueStore(blobs)
    assert await resolve_references(read_back, encoding, store.get, THREAD) == original


def _whole_ref(encoded: tuple[str, bytes]) -> Any:
    from tai42_kit.llm.checkpoint.chunking import CheckpointValueRef

    return CheckpointValueRef("0" * 64, len(encoded[1]))


# --------------------------------------------------------------------------- #
# The built-in stores' savers through the kit: a read resolves only what its decode announced
# --------------------------------------------------------------------------- #
@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts every top-level resolver walk (``find_references`` over a read's tuple or value)."""
    from tai42_kit.llm.checkpoint import chunking

    calls: list[int] = []
    depth: list[int] = []
    original = chunking.find_references

    def counting(value: Any, encoding: Any, found: Any) -> None:
        if not depth:
            calls.append(1)
        depth.append(1)
        try:
            original(value, encoding, found)
        finally:
            depth.pop()

    monkeypatch.setattr(chunking, "find_references", counting)
    return calls


class _ThreadValues:
    """A thread value store over a fake: ``get`` reads it, ``touch`` and ``put`` record what a write asks."""

    def __init__(self, store: FakeValueStore | None = None) -> None:
        self.store = store or FakeValueStore({})
        self.touched: list[Any] = []
        self.put_values: dict[str, StoredBlob] = {}

    async def get(self, thread_id: str, digests: Collection[str]) -> Mapping[str, StoredBlob]:
        assert thread_id == THREAD
        return await self.store.get(digests)

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        self.touched.append(sorted(digests))
        return {digest for digest in digests if digest not in self.put_values}

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        self.put_values.update(blobs)
        self.store.blobs.update(blobs)

    async def held_digests(self, thread_id: str) -> set[str]:
        return set(self.store.blobs)

    async def delete_thread(self, thread_id: str) -> None:
        self.store.blobs.clear()


_CONFIG: Any = {"configurable": {"thread_id": THREAD, "checkpoint_ns": "", "checkpoint_id": "c1"}}
_LARGE = {"documents": {f"d{i}": {"a": f"{i}" * 700, "b": "b" * 700} for i in range(3)}}


def _saver(provider: str, handle: Any, threshold: int | None, values: Any) -> Any:
    """The saver the kit hands a graph for ``provider`` over ``handle``, sharing above ``threshold``."""
    resource = CheckpointResource(provider, handle, MemoryFinishedThreadLedger(), values)
    saver: Any = get_saver_from_resource(provider, resource)
    saver.threshold = threshold
    return saver


def _postgres_saver(threshold: int | None, values: Any = None) -> Any:
    return _saver("postgres", object(), threshold, values or _ThreadValues())


def _postgres_tuple(serde: Any, blobs: Mapping[str, tuple[str, bytes]]) -> CheckpointTuple:
    """A tuple read the way the library reads one: every blob decoded by the saver's serializer."""
    return CheckpointTuple(
        config=_CONFIG,
        checkpoint=cast(
            "Any", {"v": 4, "id": "c1", "channel_values": {k: serde.loads_typed(b) for k, b in blobs.items()}}
        ),
        metadata={"source": "loop", "step": 1, "parents": {}},
        pending_writes=[],
    )


def _postgres_stored(saver: Any, value: Any) -> tuple[dict[str, tuple[str, bytes]], FakeValueStore]:
    """``value`` as a channel is stored with sharing on: its blob, under the referencing tag, and its value store."""
    from tai42_kit.llm.checkpoint import PreEncodedValue

    encoding = saver.encoding
    chunker = ValueChunker(encoding, THRESHOLD)
    rewritten, encoded = chunker.chunk(value)
    assert rewritten is not value
    store = FakeValueStore({digest: encoding.store_form(enc) for digest, enc in chunker.blobs.items()})
    return {"v": saver.serde.dumps_typed(PreEncodedValue(encoding.inline_form(encoded), references=True))}, store


async def test_a_postgres_read_without_references_walks_nothing(monkeypatch, walks):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    saver = _postgres_saver(THRESHOLD)
    stored = {"v": saver.serde.dumps_typed(_LARGE), "n": saver.serde.dumps_typed(1)}

    async def library_get(self: Any, config: Any) -> Any:
        return _postgres_tuple(self.serde, stored)

    async def library_list(self: Any, config: Any, **kwargs: Any) -> Any:
        for _ in range(2):
            yield _postgres_tuple(self.serde, stored)

    monkeypatch.setattr(AsyncPostgresSaver, "aget_tuple", library_get)
    monkeypatch.setattr(AsyncPostgresSaver, "alist", library_list)
    tup = await saver.aget_tuple(_CONFIG)
    assert tup is not None
    assert tup.checkpoint["channel_values"] == {"v": _LARGE, "n": 1}
    assert [t.checkpoint["channel_values"] async for t in saver.alist(_CONFIG)] == [{"v": _LARGE, "n": 1}] * 2
    assert walks == []


async def test_a_postgres_read_with_references_resolves_them(monkeypatch, walks):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    saver = _postgres_saver(None)
    stored, store = _postgres_stored(saver, _LARGE)
    saver.values = _ThreadValues(store)
    plain = {"v": saver.serde.dumps_typed({"small": 1})}

    async def library_get(self: Any, config: Any) -> Any:
        return _postgres_tuple(self.serde, stored)

    async def library_list(self: Any, config: Any, **kwargs: Any) -> Any:
        yield _postgres_tuple(self.serde, plain)
        yield _postgres_tuple(self.serde, stored)

    monkeypatch.setattr(AsyncPostgresSaver, "aget_tuple", library_get)
    monkeypatch.setattr(AsyncPostgresSaver, "alist", library_list)
    tup = await saver.aget_tuple(_CONFIG)
    assert tup is not None
    assert tup.checkpoint["channel_values"] == {"v": _LARGE}
    assert walks
    listed = [t.checkpoint["channel_values"] async for t in saver.alist(_CONFIG)]
    assert listed == [{"v": {"small": 1}}, {"v": _LARGE}]


async def test_the_postgres_delta_history_resolves_only_what_it_announced(monkeypatch, walks):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    saver = _postgres_saver(None)
    seed_value = {"items": ["s" * 2000]}
    write_value = ["w" * 2000]
    seed_blob, store = _postgres_stored(saver, seed_value)
    write_blob, write_store = _postgres_stored(saver, write_value)
    store.blobs.update(write_store.blobs)
    saver.values = _ThreadValues(store)
    plain_seed = saver.serde.dumps_typed(seed_value)
    shared = True

    async def library_history(self: Any, *, config: Any, channels: Any) -> Any:
        if not shared:
            return {"items": {"writes": [], "seed": self.serde.loads_typed(plain_seed)}}
        write = self.serde.loads_typed(write_blob["v"])
        return {"items": {"writes": [("task-1", "items", write)], "seed": self.serde.loads_typed(seed_blob["v"])}}

    monkeypatch.setattr(AsyncPostgresSaver, "aget_delta_channel_history", library_history)
    history = await saver.aget_delta_channel_history(config=_CONFIG, channels=["items"])
    assert history == {"items": {"writes": [("task-1", "items", write_value)], "seed": seed_value}}
    walked = len(walks)
    assert walked
    shared = False
    history = await saver.aget_delta_channel_history(config=_CONFIG, channels=["items"])
    assert history == {"items": {"writes": [], "seed": seed_value}}
    assert len(walks) == walked


def _redis_saver(threshold: int | None, values: Any = None) -> Any:
    from redis.asyncio import Redis as AsyncRedis

    from tai42_kit.llm.checkpoint.codec import GuardedAsyncRedisSaver
    from tai42_kit.llm.checkpoint.stores.redis import RedisHandle

    client = AsyncRedis.from_url("redis://127.0.0.1:1/0")
    handle = RedisHandle(GuardedAsyncRedisSaver(redis_client=client), client)
    return _saver(
        "redis", handle, threshold, _ThreadValues(values) if not isinstance(values, _NoValueStore) else values
    )


def _redis_stored(values: dict[str, Any]) -> tuple[bytes, FakeValueStore]:
    """A checkpoint document as the Redis store keeps it with sharing on, and its value store."""
    encoder = _redis_encoder()
    chunker = ValueChunker(encoder.encoding, THRESHOLD)
    checkpoint = {"v": 4, "id": "c1", "channel_values": values, "channel_versions": dict.fromkeys(values, "1")}
    data = encoder.encode_checkpoint(checkpoint, {}, chunker)
    return data, FakeValueStore({digest: encoder.encoding.store_form(enc) for digest, enc in chunker.blobs.items()})


def _redis_tuple_read(serde: Any, data: bytes) -> CheckpointTuple:
    """A tuple read the way the library reads one: the document decoded through the saver's serializer."""
    return CheckpointTuple(
        config=_CONFIG,
        checkpoint=serde.loads_typed(("json", data)),
        metadata={"source": "loop", "step": 1, "parents": {}},
        pending_writes=[],
    )


async def test_a_redis_read_without_references_walks_nothing(monkeypatch, walks):
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    saver = _redis_saver(THRESHOLD)
    data, _store = _redis_stored({"n": 1, "small": {"a": "x" * 100}})

    async def library_get(self: Any, config: Any) -> Any:
        return _redis_tuple_read(self.serde, data)

    async def library_list(self: Any, config: Any, **kwargs: Any) -> Any:
        # The library decodes every document of a listing before it yields the first.
        read = [_redis_tuple_read(self.serde, data) for _ in range(3)]
        for tup in read:
            yield tup

    monkeypatch.setattr(AsyncRedisSaver, "aget_tuple", library_get)
    monkeypatch.setattr(AsyncRedisSaver, "alist", library_list)
    tup = await saver.aget_tuple(_CONFIG)
    assert tup is not None
    assert tup.checkpoint["channel_values"]["n"] == 1
    assert len([t async for t in saver.alist(_CONFIG)]) == 3
    assert walks == []


async def test_a_redis_read_with_references_resolves_every_listed_tuple(monkeypatch, walks):
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    shared, store = _redis_stored({"docs": _LARGE})
    plain, _ = _redis_stored({"n": 1})
    saver = _redis_saver(None, store)

    async def library_get(self: Any, config: Any) -> Any:
        return _redis_tuple_read(self.serde, shared)

    async def library_list(self: Any, config: Any, **kwargs: Any) -> Any:
        # The library decodes every document of a listing before it yields the first.
        read = [
            _redis_tuple_read(self.serde, plain),
            _redis_tuple_read(self.serde, shared),
            _redis_tuple_read(self.serde, shared),
        ]
        for tup in read:
            yield tup

    monkeypatch.setattr(AsyncRedisSaver, "aget_tuple", library_get)
    monkeypatch.setattr(AsyncRedisSaver, "alist", library_list)
    tup = await saver.aget_tuple(_CONFIG)
    assert tup is not None
    assert tup.checkpoint["channel_values"] == {"docs": _LARGE}
    assert walks
    listed = [t.checkpoint["channel_values"] async for t in saver.alist(_CONFIG)]
    assert listed == [{"n": 1}, {"docs": _LARGE}, {"docs": _LARGE}]


# --------------------------------------------------------------------------- #
# The write doors: with sharing off the library's own path; on, the blobs that reference are tagged
# --------------------------------------------------------------------------- #
def _checkpoint_with(values: dict[str, Any]) -> Any:
    return {
        "v": 4,
        "id": "c2",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": values,
        "channel_versions": dict.fromkeys(values, "1"),
        "versions_seen": {},
        "updated_channels": None,
    }


class _NoValueStore:
    """A value store no write may reach."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the value store was used: {name}")


async def test_a_postgres_write_with_sharing_off_is_the_librarys_own(monkeypatch):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    handed: list[Any] = []
    encodes: list[int] = []

    async def library_aput(self: Any, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        handed.append((checkpoint, new_versions))
        return config

    async def library_aput_writes(self: Any, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        handed.append(writes)

    original_encode = MsgpackValueEncoding.encode

    def counting_encode(self: Any, value: Any) -> Any:
        encodes.append(1)
        return original_encode(self, value)

    monkeypatch.setattr(AsyncPostgresSaver, "aput", library_aput)
    monkeypatch.setattr(AsyncPostgresSaver, "aput_writes", library_aput_writes)
    monkeypatch.setattr(MsgpackValueEncoding, "encode", counting_encode)
    saver = _saver("postgres", object(), None, _NoValueStore())
    checkpoint = _checkpoint_with({"docs": _LARGE, "text": "t" * 5000})
    versions: Any = {"docs": "1", "text": "1"}
    writes = [("docs", _LARGE)]
    await saver.aput(_CONFIG, checkpoint, {"source": "loop", "step": 1, "parents": {}}, versions)
    await saver.aput_writes(_CONFIG, writes, "task-1")
    assert handed[0][0] is checkpoint
    assert handed[0][1] is versions
    assert handed[1] is writes
    assert encodes == []


async def test_a_postgres_write_with_sharing_on_tags_the_blobs_that_reference(monkeypatch):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from tai42_kit.llm.checkpoint import PreEncodedValue

    handed: list[Any] = []

    async def library_aput(self: Any, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        handed.append(checkpoint["channel_values"])
        return config

    async def library_aput_writes(self: Any, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        handed.append(dict(writes))

    monkeypatch.setattr(AsyncPostgresSaver, "aput", library_aput)
    monkeypatch.setattr(AsyncPostgresSaver, "aput_writes", library_aput_writes)
    values = _ThreadValues()
    saver = _postgres_saver(THRESHOLD, values)
    checkpoint = _checkpoint_with({"docs": _LARGE, "small": {"a": 1}, "text": "t" * 5000})
    versions: Any = {"docs": "1", "small": "1", "text": "1"}
    await saver.aput(_CONFIG, checkpoint, {"source": "loop", "step": 1, "parents": {}}, versions)
    await saver.aput_writes(_CONFIG, [("docs", _LARGE), ("small", {"a": 1})], "task-1")
    channels, written = handed
    tags = {
        name: saver.serde.dumps_typed(value)[0]
        for name, value in channels.items()
        if isinstance(value, PreEncodedValue)
    }
    assert tags == {"docs": "msgpack+ref", "small": "msgpack", "text": "msgpack+ref"}
    assert {name: saver.serde.dumps_typed(value)[0] for name, value in written.items()} == {
        "docs": "msgpack+ref",
        "small": "msgpack",
    }
    assert values.put_values


async def test_a_redis_write_with_sharing_off_is_the_librarys_own(monkeypatch):
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    handed: list[Any] = []

    async def library_aput(
        self: Any, config: Any, checkpoint: Any, metadata: Any, new_versions: Any, *args: Any
    ) -> Any:
        handed.append(checkpoint)
        return config

    async def library_aput_writes(self: Any, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        handed.append(writes)

    monkeypatch.setattr(AsyncRedisSaver, "aput", library_aput)
    monkeypatch.setattr(AsyncRedisSaver, "aput_writes", library_aput_writes)
    saver = _redis_saver(None, _NoValueStore())
    config: Any = {"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}
    checkpoint = _checkpoint_with({"docs": _LARGE})
    writes = [("docs", _LARGE)]
    await saver.aput(config, checkpoint, {"source": "loop", "step": 1, "parents": {}}, {})
    await saver.aput_writes(config, writes, "task-1")
    assert handed == [checkpoint, writes]
    # The library's own write encodes through the faithful codec the kit installed.
    assert orjson.loads(saver.serde.dumps_typed(checkpoint)[1])["channel_values"]["docs"] == _LARGE
