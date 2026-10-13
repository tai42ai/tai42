"""The run answer is discriminated on ``kind``: each kind parses to its own model, an unknown one is refused."""

from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from tai42_contract.interactions import (
    ParkedEntry,
    RunAnswer,
    RunAsksAnswer,
    RunNoneAnswer,
    RunParkedAnswer,
    RunResultAnswer,
    SuspendedInteraction,
)

_ADAPTER: TypeAdapter[RunAnswer] = TypeAdapter(RunAnswer)
_SUSPENDED = {"interaction_id": "i-1", "interaction_ids": ["i-1"], "caller_interaction_ids": ["i-1"]}


@pytest.mark.parametrize(
    ("body", "model"),
    [
        ({"kind": "result", "result": {"kind": "parked"}}, RunResultAnswer),
        ({"kind": "result", "result": None}, RunResultAnswer),
        ({"kind": "asks", "asks": [{"id": "i-1", "status": "asking"}], "suspended": _SUSPENDED}, RunAsksAnswer),
        ({"kind": "parked", "suspended": _SUSPENDED}, RunParkedAnswer),
        ({"kind": "none"}, RunNoneAnswer),
    ],
)
def test_each_kind_parses_to_its_own_model_and_round_trips(body: dict[str, object], model: type) -> None:
    answer = _ADAPTER.validate_python(body)

    assert type(answer) is model
    assert _ADAPTER.validate_json(_ADAPTER.dump_json(answer)) == answer


def test_a_parked_answer_carries_the_sentinel() -> None:
    answer = _ADAPTER.validate_python({"kind": "parked", "suspended": _SUSPENDED})

    assert isinstance(answer, RunParkedAnswer)
    assert answer.suspended == SuspendedInteraction.model_validate(_SUSPENDED)


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "receipt", "result": 1},
        {"result": 1},
        {"kind": "result"},
        {"kind": "parked"},
        {"kind": "asks", "asks": []},
    ],
    ids=["unknown-kind", "no-kind", "result-without-value", "parked-without-sentinel", "asks-without-sentinel"],
)
def test_a_body_of_any_other_shape_is_refused(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(body)


def test_an_asks_answer_dumps_each_entry_without_its_unset_fields() -> None:
    # An entry's unset optional fields are absent from the dumped answer, never null keys; a set
    # value keeps its own nested nulls.
    answer = RunAsksAnswer(
        asks=[ParkedEntry(id="i-1", status="asking", to="caller", payload={"note": None})],
        suspended=SuspendedInteraction.model_validate(_SUSPENDED),
    )

    for dumped in (answer.model_dump(mode="json"), _ADAPTER.dump_python(answer, mode="json")):
        assert dumped["asks"] == [{"id": "i-1", "status": "asking", "to": "caller", "payload": {"note": None}}]
    assert _ADAPTER.validate_json(_ADAPTER.dump_json(answer)) == answer


def test_a_parked_entry_dumps_only_its_set_fields_in_every_mode() -> None:
    # The entry itself leaves every unset field out, so every door that dumps one sends the same shape.
    entry = ParkedEntry(id="i-1", status="finished", result={"value": None})
    expected = {"id": "i-1", "status": "finished", "result": {"value": None}}

    assert entry.model_dump() == expected
    assert entry.model_dump(mode="json") == expected
    assert json.loads(entry.model_dump_json()) == expected
    assert ParkedEntry.model_validate_json(entry.model_dump_json()) == entry
