"""The resolver, offline, over a fake value store: references restored, a missing or altered value fails loudly.

The Redis side is read the way the saver reads it (the serializer's reviver turns a ``ref`` envelope into
a reference); the Postgres side reads the msgpack blobs back as plain dictionaries.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

import orjson
import pytest

pytest.importorskip("langgraph.checkpoint.redis")

from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from tai42_kit.llm.checkpoint.checkpoint import _GuardedSerializer
from tai42_kit.llm.checkpoint.chunking import (
    CheckpointReferenceError,
    StoredBlob,
    ValueChunker,
    resolve_references,
    resolve_tuple,
)
from tai42_kit.llm.checkpoint.codec import (
    BlobCompressCodec,
    CheckpointedException,
    FaithfulRedisSerializer,
)
from tai42_kit.llm.checkpoint.postgres_saver import GuardedAsyncPostgresSaver, PostgresMsgpackValueEncoding
from tai42_kit.llm.checkpoint.redis_values import RedisCheckpointEncoder

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


def _redis_tuple(values: dict[str, Any], writes: list[tuple[str, Any]]) -> tuple[CheckpointTuple, FakeValueStore, Any]:
    encoder = RedisCheckpointEncoder(THRESHOLD, 65536)
    chunker = encoder.chunker()
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


async def test_references_inside_a_blob_are_resolved_one_fetch_per_depth():
    inner = {"big": {"a": "a" * 700, "b": "b" * 700}, **{f"k{i}": "s" * 100 for i in range(30)}}
    tup, store, encoding = _redis_tuple({"outer": inner}, [])
    resolved = await resolve_tuple(tup, encoding, store.get)
    assert resolved.checkpoint["channel_values"]["outer"] == inner
    assert len(store.fetches) == 2


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
def _postgres_encoding() -> PostgresMsgpackValueEncoding:
    return PostgresMsgpackValueEncoding(_GuardedSerializer(JsonPlusSerializer(), BlobCompressCodec(2048)), 2048)


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


async def test_the_postgres_delta_history_resolves_its_writes_and_seeds(monkeypatch):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    encoding = _postgres_encoding()
    chunker = ValueChunker(encoding, THRESHOLD)
    seed_value = {"items": ["s" * 2000]}
    write_value = ["w" * 2000]
    seed, _ = chunker.chunk(seed_value)
    write, _ = chunker.chunk(write_value)
    store = FakeValueStore({digest: encoding.store_form(enc) for digest, enc in chunker.blobs.items()})

    async def library_history(self: Any, *, config: Any, channels: Any) -> Any:
        return {"items": {"writes": [("task-1", "items", write)], "seed": seed}}

    monkeypatch.setattr(AsyncPostgresSaver, "aget_delta_channel_history", library_history)
    saver = GuardedAsyncPostgresSaver(object())
    saver.serde = _GuardedSerializer(JsonPlusSerializer(), BlobCompressCodec(2048))

    class _Values:
        async def get(self, thread_id: str, digests: Collection[str]) -> Mapping[str, StoredBlob]:
            assert thread_id == THREAD
            return await store.get(digests)

    saver._values = _Values()  # pyright: ignore[reportAttributeAccessIssue]
    history = await saver.aget_delta_channel_history(
        config={"configurable": {"thread_id": THREAD, "checkpoint_ns": ""}}, channels=["items"]
    )
    assert history == {"items": {"writes": [("task-1", "items", write_value)], "seed": seed_value}}
