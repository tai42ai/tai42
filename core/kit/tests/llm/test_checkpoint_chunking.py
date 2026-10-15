"""The shared-value chunker, offline, over both stores' encodings: what becomes a reference, and what is encoded.

The values are neutral synthetic shapes (a dictionary of documents, a large result, small scalars);
nothing depends on a channel name or a consumer's shape.
"""

from __future__ import annotations

import hashlib
from typing import Any

import orjson
import pytest

pytest.importorskip("langgraph.checkpoint.redis")

from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.types import Interrupt, Send

from tai42_kit.llm.checkpoint.checkpoint import _GuardedSerializer
from tai42_kit.llm.checkpoint.chunking import CheckpointValueRef, ValueChunker
from tai42_kit.llm.checkpoint.codec import (
    BlobCompressCodec,
    FaithfulRedisSerializer,
    RedisFaithfulCodec,
    envelope_reference,
)
from tai42_kit.llm.checkpoint.postgres_saver import PostgresMsgpackValueEncoding
from tai42_kit.llm.checkpoint.redis_values import RedisCheckpointEncoder

THRESHOLD = 1024


def _redis_encoding() -> Any:
    return RedisCheckpointEncoder(THRESHOLD, 65536).encoding


def _postgres_encoding() -> Any:
    return PostgresMsgpackValueEncoding(_GuardedSerializer(JsonPlusSerializer(), BlobCompressCodec(65536)), 65536)


ENCODINGS = {"redis": _redis_encoding, "postgres": _postgres_encoding}


def _ref(value: Any) -> CheckpointValueRef | None:
    # Both encodings write the same ref envelope; the chunker's output is the stored form.
    return envelope_reference(value)


def _doc(index: int, size: int = 1500) -> dict[str, Any]:
    """A document above the threshold whose every field is below it, so the document itself is shared."""
    part = max(size // 3, 1)
    return {"title": f"document {index}", "a": f"{index}a" + "y" * part, "b": "y" * part, "c": "y" * part}


def _chunker(store: str, threshold: int | None = THRESHOLD) -> ValueChunker:
    return ValueChunker(ENCODINGS[store](), threshold)


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_a_sub_value_above_the_threshold_becomes_a_reference_and_its_siblings_stay_inline(store):
    chunker = _chunker(store)
    value = {"documents": {"a": _doc(1), "b": _doc(2)}, "count": 3, "label": "small"}
    rewritten, (_type, data) = chunker.chunk(value)
    assert rewritten["count"] == 3
    assert rewritten["label"] == "small"
    refs = {name: _ref(item) for name, item in rewritten["documents"].items()}
    assert all(ref is not None for ref in refs.values())
    assert len(chunker.blobs) == 2
    assert b"y" * 100 not in data
    for name, ref in refs.items():
        assert ref is not None
        encoded = chunker.blobs[ref.digest]
        assert ref.digest == hashlib.sha256(encoded[1]).hexdigest()
        assert ref.size == len(encoded[1])
        assert encoded == ENCODINGS[store]().encode(value["documents"][name])


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_the_same_bytes_at_two_paths_are_one_blob(store):
    chunker = _chunker(store)
    rewritten, _ = chunker.chunk({"initial": {"a": _doc(1)}, "current": {"a": _doc(1)}})
    first, second = _ref(rewritten["initial"]["a"]), _ref(rewritten["current"]["a"])
    assert first is not None
    assert first == second
    assert len(chunker.blobs) == 1


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_a_change_deep_inside_one_document_changes_only_its_digest(store):
    before, _ = _chunker(store).chunk({"a": _doc(1), "b": _doc(2)})
    changed_doc = _doc(2)
    changed_doc["c"] = changed_doc["c"][:-1] + "z"
    after, _ = _chunker(store).chunk({"a": _doc(1), "b": changed_doc})
    assert _ref(before["a"]) == _ref(after["a"])
    assert _ref(before["b"]) != _ref(after["b"])


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_a_scalar_string_above_the_threshold_is_referenced(store):
    chunker = _chunker(store)
    rewritten, _ = chunker.chunk({"text": "q" * 5000, "n": 1})
    assert _ref(rewritten["text"]) is not None
    assert rewritten["n"] == 1


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_a_value_whose_children_are_small_becomes_one_blob(store):
    chunker = _chunker(store)
    value = {f"k{i}": "s" * 100 for i in range(30)}
    rewritten, _ = chunker.chunk({"many": value})
    ref = _ref(rewritten["many"])
    assert ref is not None
    assert ref.closure == ()
    assert len(chunker.blobs) == 1


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_a_blob_holding_references_names_them_in_its_closure(store):
    chunker = _chunker(store)
    inner = {"big": _doc(1), **{f"k{i}": "s" * 100 for i in range(30)}}
    rewritten, _ = chunker.chunk({"outer": inner})
    outer_ref = _ref(rewritten["outer"])
    assert outer_ref is not None
    inner_digest = hashlib.sha256(ENCODINGS[store]().encode(_doc(1))[1]).hexdigest()
    assert outer_ref.closure == (inner_digest,)
    assert set(chunker.blobs) == {outer_ref.digest, inner_digest}


def test_the_library_markers_are_descended_never_replaced():
    from tai42_kit.llm.checkpoint.codec import _walk

    chunker = _chunker("redis")
    value = _walk(
        {
            "interrupt": Interrupt(value={"doc": _doc(1)}, id="i1"),
            "send": Send("node", {"doc": _doc(2)}),
            "snapshot": _DeltaSnapshot([_doc(3)]),
        },
        "$",
    )
    rewritten, _ = chunker.chunk(value)
    assert isinstance(rewritten["interrupt"], Interrupt)
    assert rewritten["interrupt"].id == "i1"
    assert _ref(rewritten["interrupt"].value["doc"]) is not None
    assert isinstance(rewritten["send"], Send)
    assert rewritten["send"].node == "node"
    assert _ref(rewritten["send"].arg["doc"]) is not None
    assert isinstance(rewritten["snapshot"], _DeltaSnapshot)
    assert _ref(rewritten["snapshot"].value[0]) is not None


def test_the_library_markers_are_descended_on_postgres():
    chunker = _chunker("postgres")
    rewritten, _ = chunker.chunk({"snapshot": _DeltaSnapshot([_doc(3)]), "send": Send("n", {"doc": _doc(2)})})
    assert isinstance(rewritten["snapshot"], _DeltaSnapshot)
    assert _ref(rewritten["snapshot"].value[0]) is not None
    assert isinstance(rewritten["send"], Send)
    assert _ref(rewritten["send"].arg["doc"]) is not None


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_a_snapshot_rebuilt_around_a_reference_is_a_shared_snapshot(store):
    from tai42_kit.llm.checkpoint.chunking import SharedSnapshot

    rewritten, _ = _chunker(store).chunk({"snapshot": _DeltaSnapshot([_doc(3)])})
    assert type(rewritten["snapshot"]) is SharedSnapshot
    assert isinstance(rewritten["snapshot"], _DeltaSnapshot)
    assert _ref(rewritten["snapshot"].value[0]) is not None


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_a_snapshot_whose_payload_stays_inline_is_the_original_object(store):
    snapshot = _DeltaSnapshot([{"small": 1}])
    rewritten, _ = _chunker(store).chunk({"snapshot": snapshot})
    assert rewritten["snapshot"] is snapshot


async def test_a_resolved_shared_snapshot_is_a_plain_snapshot():
    from tai42_kit.llm.checkpoint.chunking import StoredBlob, resolve_references

    encoding = _postgres_encoding()
    chunker = ValueChunker(encoding, THRESHOLD)
    rewritten, _ = chunker.chunk(_DeltaSnapshot([_doc(3)]))
    blobs = {digest: StoredBlob(*encoded) for digest, encoded in chunker.blobs.items()}

    async def fetch(digests: Any) -> Any:
        return {digest: blobs[digest] for digest in digests}

    resolved = await resolve_references(rewritten, encoding, fetch, "t1")
    assert type(resolved) is _DeltaSnapshot
    assert resolved.value == [_doc(3)]


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_a_serialized_object_is_replaced_whole_never_descended(store):
    chunker = _chunker(store)
    serialized = {"lc": 1, "type": "constructor", "id": ["x", "Y"], "kwargs": {"a": _doc(1), "b": _doc(2)}}
    rewritten, _ = chunker.chunk({"object": serialized})
    ref = _ref(rewritten["object"])
    assert ref is not None
    assert ref.closure == ()
    assert len(chunker.blobs) == 1


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_no_threshold_references_nothing(store):
    chunker = _chunker(store, None)
    value = {"a": _doc(1, 100_000)}
    rewritten, encoded = chunker.chunk(value)
    assert rewritten is value
    assert chunker.blobs == {}
    assert encoded == ENCODINGS[store]().encode(value)


def _corpus() -> list[Any]:
    return [
        {"documents": {f"d{i}": _doc(i, 300 * i) for i in range(6)}, "n": 1},
        [_doc(1), [_doc(2, 3000), "w" * 2000], {"deep": {"deeper": _doc(3, 4000)}}],
        {"mixed": [1, 2.5, True, None, "t" * 1100], "nested": {"x": {"y": {"z": "v" * 900}}}},
        {"wide": {f"k{i}": f"value {i} " * 20 for i in range(200)}},
    ]


@pytest.mark.parametrize("store", list(ENCODINGS))
def test_every_measured_size_is_the_length_of_the_store_serializers_own_encoding(store, monkeypatch):
    encoding = ENCODINGS[store]()
    library = JsonPlusRedisSerializer() if store == "redis" else JsonPlusSerializer()
    measured: list[tuple[Any, bytes]] = []
    original = encoding.encode

    def recording(value: Any) -> tuple[str, bytes]:
        encoded = original(value)
        measured.append((value, encoded[1]))
        return encoded

    monkeypatch.setattr(encoding, "encode", recording)
    for value in _corpus():
        ValueChunker(encoding, THRESHOLD).chunk(value)
    assert len(measured) > 50
    for value, data in measured:
        if value is None:
            # A bare None is the serializer's own "null" type with no bytes; nested, it is JSON null.
            continue
        assert data == library.dumps_typed(value)[1]


def _counting_default_handler(monkeypatch) -> list[str]:
    calls: list[str] = []
    original = FaithfulRedisSerializer._default_handler

    def counting(self: Any, obj: Any) -> Any:
        if isinstance(obj, Send):
            calls.append(obj.node)
        return original(self, obj)

    monkeypatch.setattr(FaithfulRedisSerializer, "_default_handler", counting)
    return calls


def _checkpoint(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": 4,
        "id": "c1",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": values,
        "channel_versions": dict.fromkeys(values, "1"),
        "versions_seen": {},
        "updated_channels": None,
    }


def test_a_channel_value_is_encoded_once_when_nothing_is_shared(monkeypatch):
    calls = _counting_default_handler(monkeypatch)
    checkpoint = _checkpoint({"v": {"probe": Send("probe", 1), "pad": "x" * 10_000}})
    type_, _data = RedisFaithfulCodec(65536).dumps_typed(FaithfulRedisSerializer(), checkpoint)
    assert type_ == "json"
    assert calls == ["probe"]


def test_a_write_value_is_encoded_once_when_nothing_is_shared(monkeypatch):
    calls = _counting_default_handler(monkeypatch)
    RedisFaithfulCodec(65536).dumps_typed(FaithfulRedisSerializer(), {"probe": Send("probe", 1), "pad": "x" * 10_000})
    assert calls == ["probe"]


def test_each_byte_is_encoded_once_per_nesting_level_above_the_threshold(monkeypatch):
    calls = _counting_default_handler(monkeypatch)
    encoder = RedisCheckpointEncoder(THRESHOLD, 65536)
    checkpoint = _checkpoint({"v": {"doc": {"probe": Send("probe", 1), "a": "a" * 700, "b": "b" * 700}}})
    data = encoder.encode_checkpoint(checkpoint, {}, encoder.chunker())
    # The channel level, the document level and the marker's own level; the document becomes a blob
    # from the bytes measured, the channel is encoded again around its reference, and the checkpoint
    # text reuses the channel's encoding.
    assert calls == ["probe", "probe", "probe"]
    stored = orjson.loads(data)["channel_values"]["v"]["doc"]
    assert envelope_reference(stored) is not None


def test_a_channel_reused_from_the_parent_is_not_walked_or_encoded(monkeypatch):
    calls = _counting_default_handler(monkeypatch)
    encoder = RedisCheckpointEncoder(THRESHOLD, 65536)
    stored = {"kept": [1, 2, 3]}
    checkpoint = _checkpoint({"v": {"probe": Send("probe", 1)}, "kept": object()})
    data = encoder.encode_checkpoint(checkpoint, {"kept": stored}, encoder.chunker())
    assert orjson.loads(data)["channel_values"]["kept"] == stored
    assert calls == ["probe"]
