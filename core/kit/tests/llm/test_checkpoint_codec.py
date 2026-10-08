"""The checkpoint codecs, offline: the faithful Redis codec's walk, envelopes, reviver and inline compression,
the exception record, and the Postgres blob compression.

The Redis saver's two read paths are exercised without a server: the writes path is the serializer's
``loads_typed``; the checkpoint-document path is the saver's own ``_recursive_deserialize`` over the
stored JSON (run on the kit's saver class). The real-saver round trips live in
``test_checkpoint_codec_redis.py``.
"""

from __future__ import annotations

import base64
import dataclasses
import enum
import json
import pickle
import zlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import orjson
import pydantic
import pytest

pytest.importorskip("langgraph.checkpoint.redis")

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde import _msgpack as lg_msgpack
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.pregel._algo import _coerce_pending_error
from langgraph.types import Interrupt

from tai42_kit.llm.checkpoint import codec
from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError, _GuardedSerializer
from tai42_kit.llm.checkpoint.codec import (
    CODEC_KINDS,
    COMPRESSED_BLOB_TYPE,
    KIT_ENVELOPE_ID,
    BlobCompressCodec,
    CheckpointedException,
    FaithfulRedisSerializer,
    GuardedAsyncRedisSaver,
    RedisFaithfulCodec,
    exception_record,
)

THRESHOLD = 65536
PAD = "x" * 70_000


class Shade(enum.Enum):
    DARK = "dark"


class Row(pydantic.BaseModel):
    count: int
    amount: Decimal


class NotAModel:
    pass


@dataclasses.dataclass
class Tally:
    total: int
    label: str = dataclasses.field(init=False, default="")


@dataclasses.dataclass(frozen=True)
class FrozenPoint:
    x: int


class NodeFailureError(Exception):
    def __init__(self, message: str, node_id: str) -> None:
        super().__init__(message)
        self.node_id = node_id


class Opaque:
    def __repr__(self) -> str:
        return "<Opaque handle>"


class TaggedMessage(AIMessage):
    """A message subclass the LangChain reviver does not allow."""


def _serde(threshold: int | None = THRESHOLD) -> _GuardedSerializer:
    return _GuardedSerializer(FaithfulRedisSerializer(), RedisFaithfulCodec(threshold))


def _env(kind: str, payload: Any) -> dict[str, Any]:
    return {"lc": 2, "type": "constructor", "id": list(KIT_ENVELOPE_ID), "args": [kind, payload]}


def _document_saver(serde: Any) -> GuardedAsyncRedisSaver:
    """The kit's Redis saver over ``serde``; building it does no I/O, and its document walk needs no server."""
    from redis.asyncio import Redis as AsyncRedis

    saver = GuardedAsyncRedisSaver(redis_client=AsyncRedis.from_url("redis://127.0.0.1:1/0"))
    saver.serde = serde
    return saver


def _document_read(serde: Any, stored: Any) -> tuple[Any, list[CheckpointSerializationError]]:
    failures: list[CheckpointSerializationError] = []
    token = codec._decode_failures.set(failures)
    try:
        return _document_saver(serde)._recursive_deserialize(stored), failures
    finally:
        codec._decode_failures.reset(token)


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


def _both_paths(value: Any, threshold: int | None = THRESHOLD) -> tuple[Any, Any, dict[str, Any]]:
    serde = _serde(threshold)
    type_, data = serde.dumps_typed(_checkpoint({"v": value}))
    assert type_ == "json"
    stored = orjson.loads(data)
    from_document, failures = _document_read(serde, stored["channel_values"])
    assert failures == []
    from_write = serde.loads_typed(serde.dumps_typed(value))
    return from_document["v"], from_write, stored["channel_values"]["v"]


def _raising_node_failure() -> NodeFailureError:
    try:
        try:
            raise ValueError("root cause")
        except ValueError as cause:
            failure = NodeFailureError("node n1 failed", "n1")
            failure.add_note("while running the step")
            raise failure from cause
    except NodeFailureError as raised:
        return raised


def test_the_codec_kinds():
    assert {
        "int_key_dict",
        "datetime",
        "date",
        "time",
        "timedelta",
        "timezone",
        "decimal",
        "uuid",
        "tuple",
        "set",
        "frozenset",
        "bytes",
        "enum",
        "model",
        "dataclass",
        "exception",
        "inflate",
    } == CODEC_KINDS


# --------------------------------------------------------------------------- #
# The walk: what is stored, and what is refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (Decimal("1.5"), "decimal"),
        ((1, 2), "tuple"),
        ({1: "a"}, "int_key_dict"),
        (frozenset({1}), "frozenset"),
        (Shade.DARK, "enum"),
        (UTC, "timezone"),
    ],
)
def test_each_degrading_type_is_stored_as_its_kit_envelope(value, kind):
    _serde_ = _serde()
    stored = orjson.loads(_serde_.dumps_typed(_checkpoint({"v": value}))[1])["channel_values"]["v"]
    assert stored["id"] == list(KIT_ENVELOPE_ID)
    assert stored["args"][0] == kind


def test_an_unknown_object_is_refused_naming_its_path():
    with pytest.raises(CheckpointSerializationError) as excinfo:
        _serde().dumps_typed(_checkpoint({"state": {"items": [1, {"handle": NotAModel()}]}}))
    assert str(excinfo.value) == (
        "checkpoint value at $.channel_values.state.items[1].handle of type NotAModel cannot be stored "
        "faithfully on the redis checkpointer"
    )


def test_a_class_defined_in_a_function_is_refused():
    class Local(enum.Enum):
        A = 1

    with pytest.raises(CheckpointSerializationError, match="defined inside a function"):
        _serde().dumps_typed({"v": Local.A})


def test_the_callers_value_is_never_mutated():
    value = {"t": (1, Decimal("2")), "k": {3: "c"}}
    snapshot = repr(value)
    _serde().dumps_typed(_checkpoint({"v": value}))
    assert repr(value) == snapshot


def test_str_int_float_subclasses_are_stored_as_their_base_value():
    class Label(str):
        __slots__ = ()

    class Count(int):
        pass

    class Ratio(float):
        pass

    got_document, got_write, _ = _both_paths({"s": Label("x"), "i": Count(3), "f": Ratio(0.5)})
    for got in (got_document, got_write):
        assert got == {"s": "x", "i": 3, "f": 0.5}
        assert [type(v) for v in got.values()] == [str, int, float]


def test_bool_none_and_exact_scalars_pass_unchanged():
    got_document, got_write, stored = _both_paths({"b": True, "n": None, "i": 2**63, "f": 1.5, "s": "t"})
    assert stored == {"b": True, "n": None, "i": 2**63, "f": 1.5, "s": "t"}
    assert got_document == got_write == stored


def test_a_frozen_dataclass_round_trips():
    got_document, got_write, _ = _both_paths(FrozenPoint(4))
    assert got_document == got_write == FrozenPoint(4)


def test_a_nested_model_and_aliases_round_trip():
    class_value = Row(count=2, amount=Decimal("0.10"))
    got_document, got_write, _ = _both_paths([class_value, {"r": class_value}])
    assert got_document == got_write == [class_value, {"r": class_value}]


def test_a_dict_key_that_is_an_enum_keeps_its_type():
    got_document, got_write, _ = _both_paths({Shade.DARK: 1})
    assert got_document == got_write == {Shade.DARK: 1}


def test_a_top_level_none_write_is_stored_by_the_library():
    serde = _serde()
    assert serde.dumps_typed(None) == ("null", b"")
    assert serde.loads_typed(("null", b"")) is None


def test_a_top_level_bytes_write_is_stored_by_the_library():
    serde = _serde()
    assert serde.dumps_typed(b"\x00raw") == ("bytes", b"\x00raw")
    assert serde.dumps_typed(bytearray(b"\x01")) == ("bytearray", b"\x01")


def test_a_langchain_value_with_no_constructor_form_is_refused(monkeypatch):
    from langchain_core.messages import AIMessage

    message = AIMessage(content="x")
    monkeypatch.setattr(AIMessage, "to_json", lambda self: {"lc": 1, "type": "not_implemented", "id": ["x"]})
    with pytest.raises(CheckpointSerializationError, match="of type AIMessage cannot be stored faithfully"):
        _serde().dumps_typed({"m": message})


def test_an_unencodable_integer_names_its_path():
    with pytest.raises(CheckpointSerializationError) as excinfo:
        _serde().dumps_typed({"payload": {"count": 2**64}})
    assert "['count']" in str(excinfo.value)
    assert str(2**64) in str(excinfo.value)


def test_a_non_json_result_after_the_rewrite_is_refused(monkeypatch):
    monkeypatch.setattr(FaithfulRedisSerializer, "dumps_typed", lambda self, obj: ("msgpack", b"\x80"))
    with pytest.raises(CheckpointSerializationError, match="could not be JSON-encoded after the faithful rewrite"):
        RedisFaithfulCodec(None).dumps_typed(FaithfulRedisSerializer(), {"v": 1})


# --------------------------------------------------------------------------- #
# Exceptions: a readable record, revived as CheckpointedException
# --------------------------------------------------------------------------- #
def test_an_exception_chain_is_stored_as_a_record_and_revived():
    failure = _raising_node_failure()
    got_document, got_write, stored = _both_paths(failure)
    assert stored["args"][0] == "exception"
    for got in (got_document, got_write):
        assert isinstance(got, CheckpointedException)
        assert str(got) == "node n1 failed"
        assert got.exc_type == f"{NodeFailureError.__module__}.NodeFailureError"
        assert got.record["id"][-1] == "NodeFailureError"
        assert got.record["kwargs"]["node_id"] == "n1"
        assert got.record["kwargs"]["__notes__"] == ["while running the step"]
        assert got.record["repr_kwargs"] == []
        assert got.cause is not None
        assert str(got.cause) == "root cause"
        assert got.cause.exc_type == "builtins.ValueError"
        assert got.__cause__ is got.cause


def test_an_attribute_json_cannot_hold_is_its_repr_and_listed():
    failure = NodeFailureError("boom", "n2")
    failure.amount = Decimal("1.5")  # type: ignore[attr-defined]
    failure.handle = Opaque()  # type: ignore[attr-defined]
    failure.original_exc = KeyError("inner")  # type: ignore[attr-defined]
    record = exception_record(failure)
    assert record["kwargs"]["amount"] == "Decimal('1.5')"
    assert record["kwargs"]["handle"] == "<Opaque handle>"
    assert sorted(record["repr_kwargs"]) == ["amount", "handle"]
    assert record["kwargs"]["original_exc"]["id"] == ["builtins", "KeyError"]
    assert record["kwargs"]["original_exc"]["message"] == "'inner'"
    json.dumps(record)


def test_a_cyclic_context_stops_with_cause_cycle():
    first = ValueError("first")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first
    record = exception_record(first)
    assert record["cause"]["message"] == "second"
    assert record["cause"]["cause"] is None
    assert record["cause"]["cause_cycle"] is True
    json.dumps(record)


def test_a_suppressed_context_is_not_recorded():
    try:
        try:
            raise ValueError("hidden")
        except ValueError:
            raise RuntimeError("shown") from None
    except RuntimeError as exc:
        record = exception_record(exc)
    assert record["cause"] is None


def test_a_revived_exception_is_stored_again_unchanged():
    revived = _serde().loads_typed(_serde().dumps_typed(_raising_node_failure()))
    again = _serde().loads_typed(_serde().dumps_typed(revived))
    assert again.record == revived.record


def test_a_revived_exception_pickles():
    revived = CheckpointedException(exception_record(_raising_node_failure()))
    copy = pickle.loads(pickle.dumps(revived))  # noqa: S301 -- our own payload
    assert copy.record == revived.record


def test_an_error_pending_write_revives_for_langgraph():
    serde = _serde()
    revived = serde.loads_typed(serde.dumps_typed(_raising_node_failure()))
    assert _coerce_pending_error(revived) is revived


# --------------------------------------------------------------------------- #
# The inline compressed envelope
# --------------------------------------------------------------------------- #
def _large_values() -> dict[str, Any]:
    tally = Tally(total=3)
    tally.label = "set after init"
    return {
        "tuple": (1, 2),
        "dataclass": tally,
        "exception": _raising_node_failure(),
        "model": Row(count=5, amount=Decimal("1.50")),
        "enum": Shade.DARK,
    }


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict-msgpack"])
@pytest.mark.parametrize("name", list(_large_values()))
def test_a_value_above_the_threshold_is_stored_compressed_and_comes_back_faithful(monkeypatch, strict, name):
    monkeypatch.setattr(lg_msgpack, "STRICT_MSGPACK_ENABLED", strict)
    value = {"pad": PAD, "v": _large_values()[name]}
    got_document, got_write, stored = _both_paths(value)
    assert stored["id"] == list(KIT_ENVELOPE_ID)
    assert stored["args"][0] == "inflate"
    assert stored["args"][1][0] == "json"
    for got in (got_document["v"], got_write["v"]):
        original = value["v"]
        if name == "exception":
            assert isinstance(got, CheckpointedException)
            assert str(got) == "node n1 failed"
            assert got.record["kwargs"]["node_id"] == "n1"
            assert str(got.cause) == "root cause"
        else:
            assert type(got) is type(original)
            assert got == original
            if name == "dataclass":
                assert got.label == "set after init"


def test_a_value_at_or_below_the_threshold_is_stored_inline():
    serde = _serde(threshold=10_000)
    value = {"pad": "y" * 100}
    exact = len(FaithfulRedisSerializer().dumps_typed(value)[1])
    stored = orjson.loads(_serde(threshold=exact).dumps_typed(_checkpoint({"v": value}))[1])
    assert stored["channel_values"]["v"] == value
    stored = orjson.loads(serde.dumps_typed(_checkpoint({"v": value}))[1])
    assert stored["channel_values"]["v"] == value


def test_no_threshold_never_compresses():
    _, _, stored = _both_paths({"pad": PAD}, threshold=None)
    assert stored == {"pad": PAD}


def test_a_write_value_above_the_threshold_is_compressed():
    serde = _serde()
    stored = orjson.loads(serde.dumps_typed({"pad": PAD})[1])
    assert stored["args"][0] == "inflate"
    assert serde.loads_typed(serde.dumps_typed({"pad": PAD})) == {"pad": PAD}


def test_compression_keeps_the_library_markers():
    serde = _serde()
    value = [Interrupt(value={"amount": Decimal("1"), "pad": PAD}, id="i1")]
    back = serde.loads_typed(serde.dumps_typed(value))
    assert back == value
    assert isinstance(back[0], Interrupt)


# --------------------------------------------------------------------------- #
# The read side: every undecodable envelope raises, on both paths
# --------------------------------------------------------------------------- #
def _inflate(json_bytes: bytes) -> dict[str, Any]:
    return _env("inflate", ["json", base64.b64encode(zlib.compress(json_bytes, 1)).decode()])


_CORRUPT: dict[str, dict[str, Any]] = {
    "corrupt decimal": _env("decimal", "not-a-number"),
    "unknown kind": _env("no-such-kind", 1),
    "malformed args": {"lc": 2, "type": "constructor", "id": list(KIT_ENVELOPE_ID), "args": "decimal"},
    "enum of a non-enum class": _env("enum", [__name__, "Row", 1]),
    "model of a non-model class": _env("model", [__name__, "NotAModel", {}]),
    "dataclass of a non-dataclass": _env("dataclass", [__name__, "NotAModel", {}]),
    "unimportable class": _env("model", ["no_such_module_anywhere", "X", {}]),
    "malformed exception record": _env("exception", {"id": "x"}),
    "exception record that is not an object": _env("exception", "boom"),
    "exception record without a message": _env("exception", {"id": ["builtins", "ValueError"], "message": 1}),
    "exception record without kwargs": _env("exception", {"id": ["builtins", "ValueError"], "message": "m"}),
    "class reference without a module": _env("model", [1, "Row", {}]),
    "dataclass fields that are not an object": _env("dataclass", [__name__, "Tally", [1]]),
    "datetime that is not text": _env("datetime", 5),
    "inflate with bad base64": _env("inflate", ["json", "!!not-base64!!"]),
    "inflate of a non-zlib payload": _env("inflate", ["json", base64.b64encode(b"plain").decode()]),
    "inflate with another tag": _env("inflate", ["msgpack", base64.b64encode(zlib.compress(b"{}")).decode()]),
    "corrupt decimal inside inflated JSON": _inflate(orjson.dumps({"d": _env("decimal", "nan-ish")})),
    "bad bytes": _env("bytes", ["str", "AA=="]),
    "foreign lc:2 id": {"lc": 2, "type": "constructor", "id": ["os", "getcwd"], "args": []},
    "foreign lc:2 dataclass": {"lc": 2, "type": "constructor", "id": [__name__, "Tally"], "kwargs": {"total": 1}},
    "lc:1 id the LangChain reviver refuses": {
        "lc": 1,
        "type": "constructor",
        "id": ["langchain", "schema", "messages", "TaggedMessage"],
        "kwargs": {"content": "x", "type": "ai"},
    },
}


@pytest.mark.parametrize("name", list(_CORRUPT))
def test_an_undecodable_envelope_raises_on_the_writes_path(name):
    serde = _serde()
    with pytest.raises(CheckpointSerializationError, match="cannot be decoded"):
        serde.loads_typed(("json", orjson.dumps({"x": _CORRUPT[name]})))


@pytest.mark.parametrize("name", list(_CORRUPT))
def test_an_undecodable_envelope_is_collected_once_on_the_document_path(name):
    _value, failures = _document_read(_serde(), {"x": _CORRUPT[name]})
    assert len(failures) == 1
    assert "cannot be decoded" in str(failures[0])


def test_a_safe_library_envelope_still_revives():
    serde = _serde()
    stored = {"lc": 2, "type": "constructor", "id": ["builtins", "set"], "args": [[1, 2]]}
    assert serde.loads_typed(("json", orjson.dumps({"x": stored}))) == {"x": {1, 2}}


def test_an_interrupt_at_document_level_revives():
    got_document, got_write, _ = _both_paths(Interrupt(value={"amount": Decimal("4.5")}, id="i1"))
    assert got_document == got_write == Interrupt(value={"amount": Decimal("4.5")}, id="i1")


# --------------------------------------------------------------------------- #
# Postgres: compressed blobs
# --------------------------------------------------------------------------- #
def test_a_large_msgpack_blob_is_compressed_and_read_back():
    inner = JsonPlusSerializer()
    blob_codec = BlobCompressCodec(1024)
    value = {"pad": "z" * 5000, "d": Decimal("1.25")}
    type_, data = blob_codec.dumps_typed(inner, value)
    assert type_ == COMPRESSED_BLOB_TYPE
    assert len(data) < 5000
    assert blob_codec.loads_typed(inner, (type_, data)) == value


def test_a_small_blob_and_no_threshold_stay_uncompressed():
    inner = JsonPlusSerializer()
    assert BlobCompressCodec(1024).dumps_typed(inner, {"a": 1})[0] == "msgpack"
    assert BlobCompressCodec(None).dumps_typed(inner, {"pad": "z" * 5000})[0] == "msgpack"
    assert BlobCompressCodec(1024).loads_typed(inner, inner.dumps_typed({"a": 1})) == {"a": 1}


def test_a_corrupt_compressed_blob_raises():
    with pytest.raises(CheckpointSerializationError, match="checkpoint blob cannot be decompressed"):
        BlobCompressCodec(1024).loads_typed(JsonPlusSerializer(), (COMPRESSED_BLOB_TYPE, b"not zlib"))


def test_the_guard_routes_postgres_blobs_through_the_codec():
    serde = _GuardedSerializer(JsonPlusSerializer(), BlobCompressCodec(10))
    type_, data = serde.dumps_typed({"pad": "z" * 100})
    assert type_ == COMPRESSED_BLOB_TYPE
    assert serde.loads_typed((type_, data)) == {"pad": "z" * 100}


# --------------------------------------------------------------------------- #
# Metadata never passes through the codec
# --------------------------------------------------------------------------- #
def test_metadata_is_encoded_by_the_library_serializer_alone():
    serde = _serde(threshold=1)
    metadata = {"source": "loop", "step": 3, "parents": {}, "counters_since_delta_snapshot": {"messages": (2, 3)}}
    type_, data = serde.dumps_metadata_typed(metadata)
    assert (type_, data) == FaithfulRedisSerializer().dumps_typed(metadata)
    assert json.loads(data)["counters_since_delta_snapshot"] == {"messages": [2, 3]}


def test_a_metadata_encode_failure_raises_the_typed_error():
    serde = _GuardedSerializer(JsonPlusSerializer())
    with pytest.raises(CheckpointSerializationError):
        serde.dumps_metadata_typed({"n": 2**64})


def test_a_naive_and_an_aware_datetime_round_trip():
    naive = datetime(2026, 1, 1, 1, 1)
    aware = datetime(2026, 1, 1, 1, 1, tzinfo=UTC)
    got_document, got_write, _ = _both_paths([naive, aware, timedelta(days=-1, seconds=5)])
    assert got_document == got_write == [naive, aware, timedelta(days=-1, seconds=5)]
    assert got_document[0].tzinfo is None


def test_a_delta_snapshot_keeps_its_marker_and_its_values():
    got_document, got_write, stored = _both_paths(_DeltaSnapshot([Decimal("1.5")]))
    assert got_document == got_write == _DeltaSnapshot([Decimal("1.5")])
    assert stored["__delta_snapshot__"] is True


def test_an_exception_attribute_holding_a_json_object_is_kept_as_is():
    error = NodeFailureError("failed", "n1")
    error.details = {"attempts": [1, 2], "ok": None}  # pyright: ignore[reportAttributeAccessIssue]
    error.keyed = {1: "a"}  # pyright: ignore[reportAttributeAccessIssue]
    record = exception_record(error)
    assert record["kwargs"]["details"] == {"attempts": [1, 2], "ok": None}
    assert record["kwargs"]["keyed"] == repr({1: "a"})
    assert "keyed" in record["repr_kwargs"]


def test_a_saver_without_a_codec_reads_through_its_own_serializer():
    from tai42_kit.llm.checkpoint.checkpoint import _guard_saver_serialization
    from tai42_kit.llm.checkpoint.providers import checkpoint_provider_facts

    saver = _guard_saver_serialization(InMemorySaver(), checkpoint_provider_facts("memory"))
    serde: Any = saver.serde
    assert serde.loads_typed(serde.dumps_typed({"n": Decimal("1.5")})) == {"n": Decimal("1.5")}
    assert serde.with_msgpack_allowlist == serde._inner.with_msgpack_allowlist


# --------------------------------------------------------------------------- #
# Values JSON cannot hold faithfully are refused at write
# --------------------------------------------------------------------------- #
class _FloatSubclass(float):
    pass


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), _FloatSubclass("nan")])
def test_a_non_finite_float_is_refused_naming_its_path_on_both_write_paths(value):
    serde = _serde()
    with pytest.raises(CheckpointSerializationError) as in_document:
        serde.dumps_typed(_checkpoint({"v": {"readings": [1.0, value]}}))
    assert str(in_document.value) == (
        f"checkpoint value at $.channel_values.v.readings[1] of type {type(value).__qualname__} cannot be stored "
        f"faithfully on the redis checkpointer: {value!r} has no JSON form"
    )
    with pytest.raises(CheckpointSerializationError, match=r"at \$\.readings\[1\] .*has no JSON form"):
        serde.dumps_typed({"readings": [1.0, value]})


def test_a_finite_float_round_trips_unchanged():
    got_document, got_write, stored = _both_paths([0.5, -0.0, 1e308])
    assert got_document == got_write == stored == [0.5, -0.0, 1e308]


def test_a_non_finite_exception_attribute_is_its_repr_and_listed():
    failure = NodeFailureError("boom", "n3")
    failure.ratio = float("nan")  # type: ignore[attr-defined]
    failure.readings = [1.0, float("inf")]  # type: ignore[attr-defined]
    record = exception_record(failure)
    assert record["kwargs"]["ratio"] == "nan"
    assert record["kwargs"]["readings"] == "[1.0, inf]"
    assert sorted(record["repr_kwargs"]) == ["ratio", "readings"]
    json.dumps(record, allow_nan=False)


def test_a_langchain_serializable_the_reviver_does_not_allow_is_refused_at_write():
    serde = _serde()
    message = TaggedMessage(content="hi")
    with pytest.raises(CheckpointSerializationError) as in_document:
        serde.dumps_typed(_checkpoint({"messages": [message]}))
    assert str(in_document.value) == (
        "checkpoint value at $.channel_values.messages[0] of type TaggedMessage cannot be stored faithfully on "
        "the redis checkpointer: LangChain will not revive "
        "('langchain', 'schema', 'messages', 'TaggedMessage') from a checkpoint"
    )
    with pytest.raises(CheckpointSerializationError, match="LangChain will not revive"):
        serde.dumps_typed(message)


def test_a_core_langchain_message_is_stored_and_revived():
    message = AIMessage(content="hi", additional_kwargs={"amount": Decimal("1.5")})
    got_document, got_write, _ = _both_paths(message)
    for got in (got_document, got_write):
        assert type(got) is AIMessage
        assert got == message


def test_a_langchain_value_the_reviver_validator_refuses_is_refused_at_write():
    from langchain_core.prompts import PromptTemplate

    # ``model_construct`` skips the template check, which would import jinja2; the value serializes the same.
    prompt = PromptTemplate.model_construct(template="{{ a }}", input_variables=["a"], template_format="jinja2")
    with pytest.raises(CheckpointSerializationError) as refused:
        _serde().dumps_typed({"prompt": prompt})
    assert str(refused.value).startswith(
        "checkpoint value at $.prompt of type PromptTemplate cannot be stored faithfully on the redis checkpointer: "
        "LangChain will not revive ('langchain', 'prompts', 'prompt', 'PromptTemplate') from a checkpoint: "
    )
