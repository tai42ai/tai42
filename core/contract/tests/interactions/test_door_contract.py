"""The door contract: the two result parsers and the parkable-door jq annotations."""

from __future__ import annotations

from typing import Any, cast

import pytest

from tai42_contract.interactions import (
    DoorContractError,
    ParkableDoorMixin,
    ResumeItem,
    TakeItem,
    parse_cancel_result,
    parse_resume_result,
)
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY


def test_parse_cancel_result_accepts_null_id_and_list():
    assert parse_cancel_result(None) == []
    assert parse_cancel_result("i-1") == ["i-1"]
    assert parse_cancel_result(["i-1", "i-2"]) == ["i-1", "i-2"]
    assert parse_cancel_result([]) == []


@pytest.mark.parametrize("bad", [5, 1.5, True, {"id": "i-1"}, ["i-1", 2], ["i-1", None], "", "   ", [""]])
def test_parse_cancel_result_raises_on_malformed(bad: object):
    with pytest.raises(DoorContractError):
        parse_cancel_result(bad)


def test_parse_resume_result_accepts_take_resume_and_mix():
    assert parse_resume_result(None) == []
    assert parse_resume_result("i-1") == [TakeItem(id="i-1")]
    assert parse_resume_result({"id": "i-1", "payload": {"answer": "yes"}}) == [
        ResumeItem(id="i-1", payload={"answer": "yes"})
    ]
    # A null payload is a real answer value, distinct from a take (a bare id).
    assert parse_resume_result({"id": "i-1", "payload": None}) == [ResumeItem(id="i-1", payload=None)]
    assert parse_resume_result([{"id": "a", "payload": 1}, "b"]) == [
        ResumeItem(id="a", payload=1),
        TakeItem(id="b"),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        5,
        1.5,
        {"id": "i-1"},  # a mapping is a resume — it needs a payload; a bare id is the take form
        {"payload": 1},  # no id
        {"id": "i-1", "payload": 1, "extra": 2},  # a resume mapping is exactly {id, payload}
        {"id": "", "payload": 1},  # blank id
        {"id": 7, "payload": 1},  # non-string id
        ["a", 2],  # a list element that is neither an id string nor a mapping
        "",
    ],
)
def test_parse_resume_result_raises_on_malformed(bad: object):
    with pytest.raises(DoorContractError):
        parse_resume_result(bad)


def test_parkable_door_jqs_declare_the_parked_variable():
    # Every one of the four door-contract expressions receives the run's parked interactions as the
    # jq variable ``$parked``, declared in its expression annotation's ``variables`` slot.
    for field in ("cancel_expr", "resume_expr", "start_expr", "extras_expr"):
        info = ParkableDoorMixin.model_fields[field]
        extra = info.json_schema_extra
        assert isinstance(extra, dict)
        annotation = cast("dict[str, Any]", extra[EXPRESSION_ANNOTATION_KEY])
        variables = cast("list[dict[str, Any]]", annotation["variables"])
        assert [v["name"] for v in variables] == ["parked"], field
        assert variables[0]["blurb"]  # a non-empty gloss for the editor
        assert variables[0]["sample"]  # a representative sample document


def test_parkable_door_fields_default_to_none():
    mixin = ParkableDoorMixin()
    assert mixin.cancel_expr is None
    assert mixin.resume_expr is None
    assert mixin.start_expr is None
    assert mixin.extras_expr is None
