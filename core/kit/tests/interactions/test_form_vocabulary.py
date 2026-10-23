"""The platform's form vocabulary: the ``visibleWhen`` evaluator, the hidden-field walk and the shared constants."""

from __future__ import annotations

import datetime
import re

import pytest

from tai42_kit.interactions import (
    DATE_CONSTRAINT_KEYS,
    FORM_WEEKDAYS,
    FORMAT_PATTERNS,
    RADIO_OPTION_THRESHOLD,
    TIME_OF_KEY,
    VISIBLE_WHEN_OPS,
    drawing_order,
    drawn_number_bounds,
    evaluate_visible_when,
    hidden_fields,
)


def _properties(predicate: dict[str, object]) -> dict[str, object]:
    return {
        "mode": {"type": "string", "enum": ["a", "b"]},
        "detail": {"type": "string", "visibleWhen": predicate},
    }


def test_evaluate_visible_when():
    assert evaluate_visible_when({"field": "mode", "equals": "a"}, {"mode": "a"}) is True
    assert evaluate_visible_when({"field": "mode", "equals": "a"}, {"mode": "b"}) is False
    assert evaluate_visible_when({"field": "mode", "in": ["a", "b"]}, {"mode": "b"}) is True
    assert evaluate_visible_when({"field": "mode", "notEmpty": True}, {"mode": ""}) is False
    assert evaluate_visible_when({"field": "mode", "notEmpty": True}, {}) is False
    assert evaluate_visible_when({"field": "mode", "notEmpty": True}, {"mode": "x"}) is True


def test_hidden_fields():
    properties = _properties({"field": "mode", "equals": "a"})
    assert hidden_fields(properties, {"mode": "a"}) == set()
    assert hidden_fields(properties, {"mode": "b"}) == {"detail"}


@pytest.mark.parametrize(
    ("predicate", "values", "visible"),
    [
        ({"field": "kind", "equals": 3}, {"kind": 3}, True),
        ({"field": "kind", "equals": 3}, {"kind": "3"}, False),
        ({"field": "kind", "equals": True}, {}, False),
        ({"field": "kind", "in": ["x", "y"]}, {"kind": "x"}, True),
        ({"field": "kind", "in": ["x", "y"]}, {"kind": "z"}, False),
        ({"field": "kind", "in": ["x", "y"]}, {}, False),
        ({"field": "kind", "notEmpty": True}, {"kind": []}, False),
        ({"field": "kind", "notEmpty": True}, {"kind": {}}, False),
        ({"field": "kind", "notEmpty": True}, {"kind": None}, False),
        ({"field": "kind", "notEmpty": True}, {"kind": 0}, True),
        ({"field": "kind", "notEmpty": True}, {"kind": ["v"]}, True),
    ],
)
def test_each_operator_reads_the_controlling_value(predicate, values, visible):
    assert evaluate_visible_when(predicate, values) is visible


def test_a_property_without_a_predicate_or_with_a_non_mapping_entry_is_never_hidden():
    properties = {"plain": {"type": "string"}, "odd": "not-a-mapping", "flagged": {"visibleWhen": "nope"}}
    assert hidden_fields(properties, {}) == set()


def test_the_shared_constants():
    assert set(VISIBLE_WHEN_OPS) == {"equals", "in", "notEmpty"}
    assert RADIO_OPTION_THRESHOLD == 5
    assert DATE_CONSTRAINT_KEYS == ("minDate", "maxDate", "unavailableDates", "rangeStart", "minDays", "maxDays")
    # Indexed by ``date.weekday()``: 2024-01-01 is a Monday.
    assert FORM_WEEKDAYS[datetime.date(2024, 1, 1).weekday()] == "monday"
    assert len(FORM_WEEKDAYS) == 7


@pytest.mark.parametrize(
    ("form_format", "good", "bad"),
    [
        ("date", ["2026-01-02"], ["2026-1-02", "20260102", "2026-01-02T00:00:00Z"]),
        ("email", ["a@b.co", "first.last@sub.example.org"], ["dhdhdh", "a@b", "a b@c.de", "a@@b.co", "@b.co", ""]),
        ("time", ["09:30", "23:59:59"], ["9:30", "09:30:", "0930", "09:30\n"]),
        (
            "date-time",
            ["2026-01-02T03:04:05Z", "2026-01-02t03:04:05.5+02:00"],
            ["2026-01-02 03:04:05Z", "2026-01-02T03:04:05", "2026-01-02T03:04Z"],
        ),
    ],
)
def test_format_patterns_are_one_regex_per_string_format(form_format, good, bad):
    # Anchored with ``^``/``$`` and written in the syntax ECMAScript and Python share, so a browser's
    # ``new RegExp(pattern).test(value)`` and the server's ``re.fullmatch`` decide the same shape.
    pattern = re.compile(FORMAT_PATTERNS[form_format])
    for value in good:
        assert pattern.fullmatch(value), value
    for value in bad:
        assert not pattern.fullmatch(value), value
    assert FORMAT_PATTERNS[form_format].startswith("^")
    assert FORMAT_PATTERNS[form_format].endswith("$")
    assert "\\d" not in FORMAT_PATTERNS[form_format]


def test_format_patterns_cover_the_typed_string_formats():
    assert set(FORMAT_PATTERNS) == {"date", "email", "time", "date-time"}


@pytest.mark.parametrize(
    ("prop", "bounds"),
    [
        ({"type": "integer", "minimum": 1.5, "maximum": 9.5}, (2, 9)),
        ({"type": "integer", "minimum": 1.0, "maximum": 3.0}, (1, 3)),
        ({"type": "integer", "minimum": -2.5}, (-2, None)),
        ({"type": "integer", "maximum": -2.5}, (None, -3)),
        ({"type": "number", "minimum": 0.5, "maximum": 1e20}, (0.5, 1e20)),
        ({"type": "number", "minimum": True, "maximum": "9"}, (None, None)),
        ({"type": "integer", "exclusiveMinimum": 0}, (None, None)),
    ],
)
def test_drawn_number_bounds_round_an_integers_bounds_inward(prop, bounds):
    # A control stepping in whole numbers from its minimum admits exactly the schema's whole
    # numbers only when that minimum is itself whole, so an integer's bounds round inward.
    drawn = drawn_number_bounds(prop)
    assert drawn == bounds
    if prop["type"] == "integer":
        assert all(bound is None or type(bound) is int for bound in drawn)


def test_the_date_key_names_are_the_contract_objects():
    from tai42_contract.interactions.models import form_dates

    assert DATE_CONSTRAINT_KEYS is form_dates.DATE_CONSTRAINT_KEYS
    assert FORM_WEEKDAYS is form_dates.FORM_WEEKDAYS
    assert TIME_OF_KEY is form_dates.TIME_OF_KEY
    assert TIME_OF_KEY == "timeOf"


_DATE = {"type": "string", "format": "date"}
_TIME = {"type": "string", "format": "time"}
_TIMED = {
    "start": _DATE,
    "end": {**_DATE, "rangeStart": "start"},
    "start_time": {**_TIME, "timeOf": "start"},
    "end_time": {**_TIME, "timeOf": "end"},
}


def test_drawing_order_puts_each_time_right_after_its_date():
    assert drawing_order(["start", "end", "start_time", "end_time"], _TIMED) == [
        "start",
        "start_time",
        "end",
        "end_time",
    ]


def test_drawing_order_moves_a_time_declared_before_its_date():
    assert drawing_order(["end_time", "start", "end"], _TIMED) == ["start", "end", "end_time"]


def test_drawing_order_keeps_a_form_without_time_of_unchanged():
    properties = {"b": _DATE, "a": {"type": "string"}, "t": _TIME}
    assert drawing_order(["b", "a", "t"], properties) == ["b", "a", "t"]


def test_drawing_order_keeps_a_time_whose_date_is_not_listed_in_place():
    assert drawing_order(["note", "end_time", "start"], {**_TIMED, "note": {"type": "string"}}) == [
        "note",
        "end_time",
        "start",
    ]


def test_drawing_order_is_idempotent():
    once = drawing_order(["end_time", "end", "start_time", "start"], _TIMED)
    assert once == ["end", "end_time", "start", "start_time"]
    assert drawing_order(once, _TIMED) == once


_CHAIN = {"d": _DATE, "t1": {**_TIME, "timeOf": "d"}, "t2": {**_TIME, "timeOf": "t1"}}
_CYCLE = {"a": {**_TIME, "timeOf": "b"}, "b": {**_TIME, "timeOf": "a"}, "c": {"type": "string"}}


def test_drawing_order_keeps_a_time_pointing_at_a_moving_name_in_place():
    # t2 names t1, which itself moves after d: t2 keeps its place, and no name is lost.
    assert drawing_order(["d", "t1", "t2"], _CHAIN) == ["d", "t1", "t2"]
    assert drawing_order(["t2", "t1", "d"], _CHAIN) == ["t2", "d", "t1"]


def test_drawing_order_keeps_a_time_of_cycle_in_place():
    assert drawing_order(["a", "b", "c"], _CYCLE) == ["a", "b", "c"]
    assert drawing_order(["c", "b", "a"], _CYCLE) == ["c", "b", "a"]


def test_drawing_order_of_a_chain_or_cycle_is_idempotent():
    for names, properties in ((["t2", "t1", "d"], _CHAIN), (["c", "b", "a"], _CYCLE)):
        once = drawing_order(names, properties)
        assert drawing_order(once, properties) == once
