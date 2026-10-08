"""Reference resolution over a neutral synthetic reader (no backend, no consumer's data)."""

from __future__ import annotations

from typing import Any, cast

import orjson
import pytest
from tai42_contract.monitoring import (
    MonitoringObservation,
    MonitoringReader,
    ObservationNotFoundError,
    PayloadRef,
    PayloadRefUnresolvedError,
    TraceNotFoundError,
)

from tai42_kit.monitoring import (
    PayloadRefValue,
    encode_payload,
    escape_pointer_token,
    is_payload_ref,
    payload_ref,
    unrecorded,
)
from tai42_kit.monitoring import (
    resolve_refs as _resolve_refs,
)

_TRACE = "a" * 32
_OTHER_TRACE = "c" * 32


def _decoded(value: Any) -> Any:
    return orjson.loads(encode_payload(value))


class _FakeReader:
    """Holds observations per (trace, span) and counts every fetch."""

    def __init__(self, observations: dict[tuple[str, str], MonitoringObservation]) -> None:
        self._observations = observations
        self.calls: list[tuple[str, str]] = []

    async def get_observation(self, trace_id: str, observation_id: str) -> MonitoringObservation:
        self.calls.append((trace_id, observation_id))
        if not any(t == trace_id for t, _ in self._observations):
            raise TraceNotFoundError(trace_id)
        try:
            return self._observations[(trace_id, observation_id)]
        except KeyError:
            raise ObservationNotFoundError(observation_id) from None


async def resolve_refs(value: Any, reader: _FakeReader, *, trace_id: str) -> Any:
    return await _resolve_refs(value, cast("MonitoringReader", reader), trace_id=trace_id)


def _obs(span_id: str, *, trace_id: str = _TRACE, **fields: Any) -> tuple[tuple[str, str], MonitoringObservation]:
    return (trace_id, span_id), MonitoringObservation(id=span_id, trace_id=trace_id, **fields)


@pytest.fixture
def chain_reader() -> _FakeReader:
    return _FakeReader(
        dict(
            [
                _obs("A", output={"x": [1, 2]}),
                _obs("B", input=_decoded({"y": payload_ref("A", "output", "/x/1")})),
                _obs("C", input=_decoded({"z": payload_ref("B", "input")})),
            ]
        )
    )


async def test_chain_resolves_through_every_reference(chain_reader: _FakeReader):
    c_input = (await chain_reader.get_observation(_TRACE, "C")).input
    chain_reader.calls.clear()
    assert await resolve_refs(c_input, chain_reader, trace_id=_TRACE) == {"z": {"y": 2}}
    assert sorted(chain_reader.calls) == [(_TRACE, "A"), (_TRACE, "B")]


async def test_each_record_is_fetched_once_per_call():
    reader = _FakeReader(dict([_obs("A", output={"x": [1, 2]})]))
    value = _decoded(
        [payload_ref("A", "output", "/x/0"), payload_ref("A", "output", "/x/1"), payload_ref("A", "output")]
    )
    assert await resolve_refs(value, reader, trace_id=_TRACE) == [1, 2, {"x": [1, 2]}]
    assert reader.calls == [(_TRACE, "A")]


async def test_a_producer_side_reference_resolves_too(chain_reader: _FakeReader):
    assert await resolve_refs({"k": payload_ref("A", "output", "/x")}, chain_reader, trace_id=_TRACE) == {"k": [1, 2]}


async def test_reference_to_another_trace():
    reader = _FakeReader(
        dict(
            [
                _obs("O", trace_id=_OTHER_TRACE, output={"v": payload_ref_decoded("P", "output")}),
                _obs("P", trace_id=_OTHER_TRACE, output="origin"),
                _obs("Q", output="wrong trace"),
            ]
        )
    )
    value = _decoded({"r": payload_ref("O", "output", "/v", trace_id=_OTHER_TRACE)})
    assert await resolve_refs(value, reader, trace_id=_TRACE) == {"r": "origin"}


def payload_ref_decoded(span_id: str, field: str) -> Any:
    return _decoded(payload_ref(span_id, field))  # type: ignore[arg-type]


async def test_pointer_runs_through_a_reference_on_its_way():
    reader = _FakeReader(
        dict(
            [
                _obs("M", output={"outputs": {"n": payload_ref_decoded("O", "output")}}),
                _obs("O", output={"deep": {"leaf": 7}}),
            ]
        )
    )
    value = _decoded(payload_ref("M", "output", "/outputs/n/deep/leaf"))
    assert await resolve_refs(value, reader, trace_id=_TRACE) == 7


async def test_missing_span_is_unresolved(chain_reader: _FakeReader):
    with pytest.raises(PayloadRefUnresolvedError, match=r"\(not yet available or lost\)$") as info:
        await resolve_refs(_decoded(payload_ref("missing", "output")), chain_reader, trace_id=_TRACE)
    assert isinstance(info.value.__cause__, ObservationNotFoundError)


async def test_missing_trace_is_unresolved(chain_reader: _FakeReader):
    value = _decoded(payload_ref("A", "output", trace_id=_OTHER_TRACE))
    with pytest.raises(PayloadRefUnresolvedError, match=r"\(not yet available or lost\)$") as info:
        await resolve_refs(value, chain_reader, trace_id=_TRACE)
    assert isinstance(info.value.__cause__, TraceNotFoundError)


@pytest.mark.parametrize("pointer", ["/x/5", "/nope", "/x/01", "/x/a", "/x/0/deeper"])
async def test_missing_pointer_token_is_unresolved(chain_reader: _FakeReader, pointer: str):
    with pytest.raises(PayloadRefUnresolvedError, match=r"names a path the record does not hold"):
        await resolve_refs(_decoded(payload_ref("A", "output", pointer)), chain_reader, trace_id=_TRACE)


async def test_self_referencing_chain_is_bounded():
    reader = _FakeReader(dict([_obs("S", input={"again": payload_ref_decoded("S", "input")})]))
    with pytest.raises(PayloadRefUnresolvedError, match="reference chain deeper than 32"):
        await resolve_refs(payload_ref_decoded("S", "input"), reader, trace_id=_TRACE)
    assert reader.calls == [(_TRACE, "S")]


async def test_malformed_reference_is_refused(chain_reader: _FakeReader):
    with pytest.raises(PayloadRefUnresolvedError, match="malformed reference"):
        await resolve_refs({"$tai42_ref": {"span_id": "A", "field": "usage"}}, chain_reader, trace_id=_TRACE)


async def test_pointer_without_leading_slash_is_malformed(chain_reader: _FakeReader):
    with pytest.raises(PayloadRefUnresolvedError, match="malformed reference"):
        await resolve_refs(
            {"$tai42_ref": {"span_id": "A", "field": "output", "pointer": "x"}}, chain_reader, trace_id=_TRACE
        )


async def test_unrecorded_marker_and_plain_values_are_unchanged(chain_reader: _FakeReader):
    value = {"u": _decoded(unrecorded()), "n": [1, "two", None], "s": "$tai42_ref"}
    assert await resolve_refs(value, chain_reader, trace_id=_TRACE) == value
    assert chain_reader.calls == []


def test_pointer_token_escaping():
    assert escape_pointer_token("a/b~c") == "a~1b~0c"
    assert escape_pointer_token("~1") == "~01"


async def test_escaped_pointer_tokens_resolve():
    reader = _FakeReader(dict([_obs("E", output={"a/b~c": {"~1": "found"}})]))
    pointer = "/" + escape_pointer_token("a/b~c") + "/" + escape_pointer_token("~1")
    assert await resolve_refs(_decoded(payload_ref("E", "output", pointer)), reader, trace_id=_TRACE) == "found"


async def test_metadata_reference_resolves_into_the_metadata():
    message = {"content": "hi", "tool_calls": [{"name": "echo", "args": {"t": 1}, "id": "c1", "type": "tool_call"}]}
    reader = _FakeReader(dict([_obs("G", metadata={"tai42.message": message})]))
    value = _decoded(payload_ref("G", "metadata", "/tai42.message/tool_calls/0"))
    assert await resolve_refs(value, reader, trace_id=_TRACE) == message["tool_calls"][0]


def test_is_payload_ref():
    ref = payload_ref("s", "input")
    assert is_payload_ref(ref)
    assert is_payload_ref(_decoded(ref))
    assert not is_payload_ref({"$tai42_ref": {}, "other": 1})
    assert not is_payload_ref(_decoded(unrecorded()))
    assert not is_payload_ref("s")


def test_payload_ref_value_equality_and_repr():
    a = payload_ref("s", "input", "/p")
    assert isinstance(a, PayloadRefValue)
    assert a == payload_ref("s", "input", "/p")
    assert a != payload_ref("s", "output", "/p")
    assert hash(a) == hash(payload_ref("s", "input", "/p"))
    assert a.ref == PayloadRef(span_id="s", field="input", pointer="/p")
    assert "PayloadRef" in repr(a)
    assert a != "not a ref"


def test_unrecorded_is_one_shared_instance():
    assert unrecorded() is unrecorded()
