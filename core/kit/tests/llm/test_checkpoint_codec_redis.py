"""Checkpoint values round-trip through the real Redis checkpoint saver the kit builds.

Every value goes through both of the saver's stored shapes: the checkpoint document (a channel
value inside ``aput``, read back by ``aget_tuple``'s document walk) and a pending write
(``aput_writes``, read back through ``loads_typed``). Opt-in real Redis (see ``_real_redis``).
"""

from __future__ import annotations

import dataclasses
import enum
import operator
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, TypedDict
from zoneinfo import ZoneInfo

import pydantic
import pytest

pytest.importorskip("langgraph.checkpoint.redis")

from langchain_core.messages import AIMessage
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, Send, interrupt

from tai42_kit.llm.checkpoint.checkpoint import create_checkpoint_resource, get_saver_from_resource

from ._real_redis import real_redis_url

pytestmark = pytest.mark.integration


class Shade(enum.Enum):
    LIGHT = "light"
    DARK = "dark"


class Inner(pydantic.BaseModel):
    amount: Decimal
    at: datetime


class Outer(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(populate_by_name=True)

    count: int = pydantic.Field(alias="Count")
    inner: Inner


@dataclasses.dataclass
class Tally:
    total: int
    label: str = dataclasses.field(init=False, default="")


def _tally() -> Tally:
    tally = Tally(total=3)
    tally.label = "set after init"
    return tally


_TZ = timezone(timedelta(hours=2), "Plus Two")
_BERLIN = ZoneInfo("Europe/Berlin")

VALUES: dict[str, Any] = {
    "decimal": Decimal("1.50"),
    "timedelta": timedelta(seconds=90),
    "int_key_dict": {1: "a", 10: "b"},
    "tz_datetime": datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC),
    "date": date(2026, 1, 2),
    "time": time(3, 4, 5, 6),
    "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "timezone": _TZ,
    "zoned_datetime": datetime(2026, 3, 1, 10, 0, tzinfo=_BERLIN),
    "zoned_datetime_fold": datetime(2026, 10, 25, 2, 30, tzinfo=_BERLIN, fold=1),
    "named_offset_datetime": datetime(2026, 1, 2, 3, 4, tzinfo=_TZ),
    "zoned_time": time(2, 30, tzinfo=_BERLIN, fold=1),
    "zoneinfo": _BERLIN,
    "pydantic_datetime": pydantic.TypeAdapter(datetime).validate_python("2026-03-01T10:00:00+02:00"),
    "pydantic_time": pydantic.TypeAdapter(time).validate_python("02:30:00-05:00"),
    "parsed_model": Inner.model_validate({"amount": "2.25", "at": "2026-01-01T00:00:00Z"}),
    "model": Outer(Count=5, inner=Inner(amount=Decimal("2.25"), at=datetime(2026, 1, 1, tzinfo=_BERLIN))),
    "enum": Shade.DARK,
    "tuple": (1, "a", (2, 3)),
    "set": {1, 2, 3},
    "frozenset": frozenset({"x", "y"}),
    "dataclass": _tally(),
    "nested_bytes": {"payload": b"\x00\x01binary", "buf": bytearray(b"\xff")},
    "message": AIMessage(content="hi", additional_kwargs={"amount": Decimal("3.75")}),
    "interrupt": Interrupt(value={"amount": Decimal("4.5")}, id="interrupt-1"),
    "send": Send("node_a", {"amount": Decimal("5.5")}),
}


@pytest.fixture
async def redis_saver() -> AsyncIterator[Any]:
    url = real_redis_url()
    resource, closer = await create_checkpoint_resource("redis", url)
    try:
        yield get_saver_from_resource("redis", resource)
    finally:
        await closer()


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _assert_same_clock(got: Any, original: Any) -> None:
    """A datetime or time read back carries the original's tzinfo, its zone name and its fold."""
    assert got.tzinfo == original.tzinfo
    assert got.utcoffset() == original.utcoffset()
    assert got.tzname() == original.tzname()
    assert got.fold == original.fold
    if isinstance(original, datetime) and original.tzinfo is not None:
        assert got.astimezone(UTC) == original.astimezone(UTC)
        assert (got + timedelta(days=180)).isoformat() == (original + timedelta(days=180)).isoformat()


async def _round_trip(saver: Any, value: Any) -> tuple[Any, Any]:
    thread_id = f"codec-{uuid.uuid4().hex}"
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"v": value}
    checkpoint["channel_versions"] = {"v": "1"}
    saved = await saver.aput(_config(thread_id), checkpoint, {"source": "input", "step": 0, "parents": {}}, {"v": "1"})
    await saver.aput_writes(saved, [("v", value)], task_id="task-1")
    tup = await saver.aget_tuple(_config(thread_id))
    assert tup is not None
    await saver.adelete_thread(thread_id)
    [(_task, channel, written)] = tup.pending_writes
    assert channel == "v"
    return tup.checkpoint["channel_values"]["v"], written


@pytest.mark.parametrize("name", list(VALUES))
async def test_value_round_trips_equal_on_both_paths(redis_saver: Any, name: str) -> None:
    original = VALUES[name]
    from_document, from_write = await _round_trip(redis_saver, original)
    for got in (from_document, from_write):
        assert type(got) is type(original)
        assert got == original
        if isinstance(original, (datetime, time)):
            _assert_same_clock(got, original)
        if isinstance(original, Outer):
            _assert_same_clock(got.inner.at, original.inner.at)
        if isinstance(original, Inner):
            _assert_same_clock(got.at, original.at)
        if isinstance(original, dict):
            for key, item in original.items():
                assert type(got[key]) is type(item)


# --------------------------------------------------------------------------- #
# Exceptions, compression and undecodable stored values through the real saver
# --------------------------------------------------------------------------- #
class NodeFailureError(Exception):
    def __init__(self, message: str, node_id: str) -> None:
        super().__init__(message)
        self.node_id = node_id


def _node_failure() -> NodeFailureError:
    try:
        try:
            raise ValueError("root cause")
        except ValueError as cause:
            raise NodeFailureError("node n1 failed", "n1") from cause
    except NodeFailureError as raised:
        return raised


async def test_an_exception_chain_round_trips_as_a_checkpointed_exception(redis_saver: Any) -> None:
    from tai42_kit.llm.checkpoint.codec import CheckpointedException

    for got in await _round_trip(redis_saver, _node_failure()):
        assert isinstance(got, CheckpointedException)
        assert str(got) == "node n1 failed"
        assert got.record["kwargs"]["node_id"] == "n1"
        assert got.cause is not None
        assert str(got.cause) == "root cause"


_PAD = "x" * 70_000


@pytest.mark.parametrize("name", ["tuple", "dataclass", "model", "enum"])
async def test_a_value_above_the_threshold_round_trips_through_the_saver(redis_saver: Any, name: str) -> None:
    value = {"pad": _PAD, "v": VALUES[name]}
    for got in await _round_trip(redis_saver, value):
        assert type(got["v"]) is type(VALUES[name])
        assert got == value


async def test_a_large_channel_value_is_stored_as_an_inflate_envelope(redis_saver: Any) -> None:
    thread_id = f"codec-{uuid.uuid4().hex}"
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"v": {"pad": _PAD}, "small": {"a": 1}}
    checkpoint["channel_versions"] = {"v": "1", "small": "1"}
    await redis_saver.aput(_config(thread_id), checkpoint, {"source": "input", "step": 0, "parents": {}}, {})
    key = await redis_saver._redis.get(redis_saver._make_redis_checkpoint_latest_key(thread_id, ""))
    stored = await redis_saver._redis.json().get(key, "$.checkpoint.channel_values")
    await redis_saver.adelete_thread(thread_id)
    assert stored[0]["v"]["args"][0] == "inflate"
    assert stored[0]["small"] == {"a": 1}


async def _stored_checkpoint_key(saver: Any, thread_id: str) -> str:
    key = await saver._redis.get(saver._make_redis_checkpoint_latest_key(thread_id, ""))
    return key.decode() if isinstance(key, bytes) else key


async def test_an_undecodable_stored_channel_value_raises_from_every_read(redis_saver: Any) -> None:
    import asyncio

    from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError

    thread_id = f"codec-{uuid.uuid4().hex}"
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"v": Decimal("1.5")}
    checkpoint["channel_versions"] = {"v": "1"}
    await redis_saver.aput(_config(thread_id), checkpoint, {"source": "input", "step": 0, "parents": {}}, {})
    key = await _stored_checkpoint_key(redis_saver, thread_id)
    await redis_saver._redis.json().set(key, "$.checkpoint.channel_values.v.args[1]", "not-a-number")
    try:
        with pytest.raises(CheckpointSerializationError, match=r"found 1 undecodable value\(s\)"):
            await redis_saver.aget_tuple(_config(thread_id))
        with pytest.raises(CheckpointSerializationError, match="undecodable"):
            async for _ in redis_saver.alist(_config(thread_id)):
                pass
        with pytest.raises(CheckpointSerializationError, match="undecodable"):
            await asyncio.to_thread(redis_saver.get_tuple, _config(thread_id))
    finally:
        await redis_saver.adelete_thread(thread_id)


async def test_an_undecodable_stored_write_raises(redis_saver: Any) -> None:
    import base64

    from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError

    thread_id = f"codec-{uuid.uuid4().hex}"
    saved = await redis_saver.aput(
        _config(thread_id), empty_checkpoint(), {"source": "input", "step": 0, "parents": {}}, {}
    )
    await redis_saver.aput_writes(saved, [("v", Decimal("1"))], task_id="task-1")
    keys = [k async for k in redis_saver._redis.scan_iter(match=f"*{thread_id}*")]
    write_keys = []
    for k in keys:
        name = k.decode() if isinstance(k, bytes) else k
        if name.startswith("checkpoint_write:"):
            write_keys.append(name)
    assert write_keys
    corrupt = base64.b64encode(b'{"lc":2,"type":"constructor","id":["os","getcwd"],"args":[]}').decode()
    for k in write_keys:
        await redis_saver._redis.json().set(k, "$.blob", corrupt)
    try:
        with pytest.raises(CheckpointSerializationError, match="cannot be decoded"):
            await redis_saver.aget_tuple(_config(thread_id))
    finally:
        await redis_saver.adelete_thread(thread_id)


# --------------------------------------------------------------------------- #
# The TTL is set at write and never refreshed by a read
# --------------------------------------------------------------------------- #
async def test_every_key_of_a_fresh_thread_carries_the_waiting_ttl_and_a_read_issues_no_expire(
    redis_saver: Any,
) -> None:
    thread_id = f"ttl-{uuid.uuid4().hex}"
    saved = await redis_saver.aput(
        _config(thread_id), empty_checkpoint(), {"source": "input", "step": 0, "parents": {}}, {}
    )
    await redis_saver.aput_writes(saved, [("v", 1)], task_id="task-1")
    try:
        keys = [k async for k in redis_saver._redis.scan_iter(match=f"*{thread_id}*")]
        assert len(keys) >= 3  # checkpoint, latest pointer, write
        waiting = 10080 * 60
        for key in keys:
            ttl = await redis_saver._redis.ttl(key)
            assert waiting - 60 <= ttl <= waiting, key
        before = (await redis_saver._redis.info("commandstats")).get("cmdstat_expire", {}).get("calls", 0)
        for _ in range(5):
            assert await redis_saver.aget_tuple(_config(thread_id)) is not None
        after = (await redis_saver._redis.info("commandstats")).get("cmdstat_expire", {}).get("calls", 0)
        assert after == before
    finally:
        await redis_saver.adelete_thread(thread_id)


# --------------------------------------------------------------------------- #
# Metadata: stored byte-identical to the plain library saver's, through a real graph
# --------------------------------------------------------------------------- #
def _extend(state: list[Any], writes: Sequence[list[Any]]) -> list[Any]:
    out = list(state)
    for write in writes:
        out.extend(write)
    return out


class DeltaState(TypedDict):
    items: Annotated[list, DeltaChannel(_extend)]
    note: Annotated[list, operator.add]


def _first(state: DeltaState) -> dict[str, Any]:
    return {"items": ["a"], "note": ["first"]}


def _second(state: DeltaState) -> dict[str, Any]:
    answer = interrupt("continue?")
    return {"items": [answer], "note": ["second"]}


def _third(state: DeltaState) -> dict[str, Any]:
    return {"items": ["c"], "note": ["third"]}


def _delta_graph() -> Any:
    builder = StateGraph(DeltaState)
    builder.add_node("first", _first)
    builder.add_node("second", _second)
    builder.add_node("third", _third)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", "third")
    builder.add_edge("third", END)
    return builder


async def _run_interrupted(checkpointer: Any, thread_id: str) -> dict[str, Any]:
    graph = _delta_graph().compile(checkpointer=checkpointer)
    config: Any = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke({"items": [], "note": []}, config)
    return await graph.ainvoke(Command(resume="b"), config)


async def test_metadata_is_stored_exactly_as_the_library_stores_it(monkeypatch) -> None:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.checkpoint.redis import AsyncRedisSaver

    from tai42_kit.settings import reset_all_settings

    url = real_redis_url()
    # Every metadata JSON is above this threshold, so any codec envelope would show.
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_INLINE_COMPRESS_BYTES", "1")
    reset_all_settings()
    resource, closer = await create_checkpoint_resource("redis", url)
    plain = AsyncRedisSaver(redis_client=resource.redis_client)
    try:
        saver = get_saver_from_resource("redis", resource)
        dumped: list[tuple[str, str]] = []
        saver_cls: Any = type(saver)
        kit_dump = saver_cls._dump_metadata

        def _recording_dump(self: Any, metadata: Any) -> str:
            ours = kit_dump(self, metadata)
            dumped.append((ours, plain._dump_metadata(metadata)))
            return ours

        monkeypatch.setattr(saver_cls, "_dump_metadata", _recording_dump)
        thread_id = f"meta-{uuid.uuid4().hex}"
        final = await _run_interrupted(saver, thread_id)
        expected = await _run_interrupted(InMemorySaver(), "reference")
        assert final == expected
        assert final["items"] == ["a", "b", "c"]

        assert dumped
        saw_counters = False
        for ours, library in dumped:
            assert ours == library
            assert "tai42_kit" not in ours
            saw_counters = saw_counters or "counters_since_delta_snapshot" in ours
        assert saw_counters
        tuples = [t async for t in saver.alist({"configurable": {"thread_id": thread_id}})]
        assert tuples
        for tup in tuples:
            assert {"source", "step", "parents"} <= set(tup.metadata)
            for pair in (tup.metadata.get("counters_since_delta_snapshot") or {}).values():
                assert isinstance(pair, list)
                assert len(pair) == 2
        await saver.adelete_thread(thread_id)
    finally:
        await closer()
        reset_all_settings()


# --------------------------------------------------------------------------- #
# Values JSON cannot hold faithfully are refused by both of the saver's writes
# --------------------------------------------------------------------------- #
class TaggedMessage(AIMessage):
    """A message subclass the LangChain reviver does not allow."""


_REFUSED: dict[str, tuple[Any, str]] = {
    "nan": (float("nan"), "nan has no JSON form"),
    "inf": (float("inf"), "inf has no JSON form"),
    "-inf": (float("-inf"), "-inf has no JSON form"),
    "message subclass": (TaggedMessage(content="hi"), "LangChain will not revive"),
}


@pytest.mark.parametrize("name", list(_REFUSED))
async def test_a_value_json_cannot_hold_is_refused_by_put_and_put_writes(redis_saver: Any, name: str) -> None:
    from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError

    value, reason = _REFUSED[name]
    thread_id = f"codec-{uuid.uuid4().hex}"
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"v": {"items": [value]}}
    checkpoint["channel_versions"] = {"v": "1"}
    metadata: Any = {"source": "input", "step": 0, "parents": {}}
    try:
        with pytest.raises(CheckpointSerializationError, match=r"\$\.channel_values\.v\.items\[0\]") as refused:
            await redis_saver.aput(_config(thread_id), checkpoint, metadata, {"v": "1"})
        assert reason in str(refused.value)
        saved = await redis_saver.aput(_config(thread_id), empty_checkpoint(), metadata, {})
        with pytest.raises(CheckpointSerializationError, match=r"\$\.items\[0\]") as refused_write:
            await redis_saver.aput_writes(saved, [("v", {"items": [value]})], task_id="task-1")
        assert reason in str(refused_write.value)
        tup = await redis_saver.aget_tuple(_config(thread_id))
        assert tup is not None
        assert tup.checkpoint["channel_values"] == {}
        assert tup.pending_writes == []
    finally:
        await redis_saver.adelete_thread(thread_id)


# --------------------------------------------------------------------------- #
# Deleting a thread removes every document it holds
# --------------------------------------------------------------------------- #
async def _thread_keys(saver: Any, thread_id: str) -> dict[str, int]:
    counts = {"checkpoint": 0, "checkpoint_write": 0}
    async for key in saver._redis.scan_iter(match=f"*{thread_id}*", count=5000):
        name = key.decode() if isinstance(key, bytes) else key
        prefix = name.split(":", 1)[0]
        if prefix in counts:
            counts[prefix] += 1
    return counts


async def test_deleting_a_thread_removes_every_write_past_one_search_page(redis_saver: Any) -> None:
    thread_id = f"delete-{uuid.uuid4().hex}"
    saved = await redis_saver.aput(
        _config(thread_id), empty_checkpoint(), {"source": "input", "step": 0, "parents": {}}, {}
    )
    for task in range(11):
        await redis_saver.aput_writes(saved, [(f"c{index}", index) for index in range(1000)], task_id=f"task-{task}")
    assert await _thread_keys(redis_saver, thread_id) == {"checkpoint": 1, "checkpoint_write": 11_000}
    await redis_saver.adelete_thread(thread_id)
    assert await _thread_keys(redis_saver, thread_id) == {"checkpoint": 0, "checkpoint_write": 0}


async def test_a_delete_round_that_removes_nothing_raises(redis_saver: Any, monkeypatch) -> None:
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    from tai42_kit.llm.checkpoint.checkpoint import CheckpointDeleteError

    thread_id = f"delete-{uuid.uuid4().hex}"
    saved = await redis_saver.aput(
        _config(thread_id), empty_checkpoint(), {"source": "input", "step": 0, "parents": {}}, {}
    )
    await redis_saver.aput_writes(saved, [("v", 1)], task_id="task-1")

    async def _removes_nothing(self: Any, thread_id: str) -> None:
        return None

    with monkeypatch.context() as patch:
        patch.setattr(AsyncRedisSaver, "adelete_thread", _removes_nothing)
        with pytest.raises(CheckpointDeleteError) as stuck:
            await redis_saver.adelete_thread(thread_id)
    assert str(stuck.value) == (
        f"checkpoint thread {thread_id!r} still holds 1 checkpoint(s) and 1 write(s) after a delete round "
        "that did not reduce them"
    )
    await redis_saver.adelete_thread(thread_id)
    assert await _thread_keys(redis_saver, thread_id) == {"checkpoint": 0, "checkpoint_write": 0}
