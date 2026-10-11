"""The platform's form vocabulary: the ``visibleWhen`` evaluator, the hidden-field walk and the shared constants."""

from __future__ import annotations

import datetime

import pytest

from tai42_kit.interactions import (
    DATE_CONSTRAINT_KEYS,
    FORM_WEEKDAYS,
    RADIO_OPTION_THRESHOLD,
    VISIBLE_WHEN_OPS,
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
