"""The one form-shape check: every failing field with a typed kind, its bound and a plain sentence.

``form_shape_errors`` is the callable behind the ``tai42_app.interactions.form_shape_errors`` facet;
the answer door refuses a FORM answer with its first error, so the in-form checks and the door share
one owner of the rules and of the words.
"""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.interactions import AnswerFormat, FormShapeError, FormShapeKind, QuestionFormat

from tai42_skeleton.app.instance import app
from tai42_skeleton.interactions.form_shape_words import shape_sentence
from tai42_skeleton.interactions.form_shapes import form_shape_errors


def _form(schema: dict[str, Any], **payload: Any) -> QuestionFormat:
    return QuestionFormat(answer_format=AnswerFormat.FORM, format_payload={"schema": schema, **payload})


def _kinds(errors: list[FormShapeError]) -> dict[str, tuple[FormShapeKind, dict[str, Any], str]]:
    return {error.field: (error.kind, error.params, error.message) for error in errors}


_EVERY_KIND = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "ratio": {"type": "number"},
        "code": {"type": "string", "pattern": "^[0-9]{4}$"},
        "email": {"type": "string", "format": "email"},
        "at": {"type": "string", "format": "time"},
        "stamp": {"type": "string", "format": "date-time"},
        "day": {"type": "string", "format": "date"},
        "nick": {"type": "string", "minLength": 3},
        "note": {"type": "string", "maxLength": 4},
        "qty": {"type": "integer", "minimum": 1},
        "cap": {"type": "number", "maximum": 9.5},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2},
        "few": {"type": "array", "items": {"type": "string"}, "maxItems": 1},
        "size": {"type": "string", "enum": ["s", "m"]},
        "after": {"type": "string", "format": "date", "minDate": "2026-01-01"},
        "before": {"type": "string", "format": "date", "maxDate": "2026-12-31"},
        "open": {"type": "string", "format": "date", "unavailableDates": ["sunday"]},
        "start": {"type": "string", "format": "date"},
        "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 2, "maxDays": 5},
        "even": {"type": "integer", "multipleOf": 2},
    },
    "required": ["name"],
}

_EVERY_BAD_VALUE = {
    "count": 1.5,
    "ratio": "lots",
    "code": "12a4",
    "email": "dhdhdh",
    "at": "9:30",
    "stamp": "2026-01-02T03:04:05",
    "day": "2026-02-30",
    "nick": "ab",
    "note": "toolong",
    "qty": 0,
    "cap": 10,
    "tags": ["one"],
    "few": ["a", "b"],
    "size": "xl",
    "after": "2025-12-31",
    "before": "2027-01-01",
    "open": "2026-10-18",
    "start": "2026-03-10",
    "end": "2026-03-20",
    "even": 3,
}


def test_one_error_per_field_with_its_kind_bound_and_sentence():
    errors = form_shape_errors(_form(_EVERY_KIND), _EVERY_BAD_VALUE)
    # Every field but the range start (its end carries the range's error) fails, in schema order.
    assert [error.field for error in errors] == [name for name in _EVERY_KIND["properties"] if name != "start"]
    assert _kinds(errors) == {
        "name": (FormShapeKind.REQUIRED, {}, "This field is required"),
        "count": (FormShapeKind.TYPE, {"type": "integer"}, "Enter a whole number"),
        "ratio": (FormShapeKind.TYPE, {"type": "number"}, "Enter a number"),
        "code": (FormShapeKind.PATTERN, {"pattern": "^[0-9]{4}$"}, "This value is not in the expected form"),
        "email": (FormShapeKind.FORMAT, {"format": "email"}, "Enter a valid email address"),
        "at": (FormShapeKind.FORMAT, {"format": "time"}, "Enter a time as HH:MM"),
        "stamp": (FormShapeKind.FORMAT, {"format": "date-time"}, "Enter a date and time with its time zone"),
        "day": (FormShapeKind.FORMAT, {"format": "date"}, "Enter a date as YYYY-MM-DD"),
        "nick": (FormShapeKind.MIN_LENGTH, {"minLength": 3}, "Enter at least 3 characters"),
        "note": (FormShapeKind.MAX_LENGTH, {"maxLength": 4}, "Enter at most 4 characters"),
        "qty": (FormShapeKind.MINIMUM, {"minimum": 1}, "Enter a number of at least 1"),
        "cap": (FormShapeKind.MAXIMUM, {"maximum": 9.5}, "Enter a number of at most 9.5"),
        "tags": (FormShapeKind.MIN_ITEMS, {"minItems": 2}, "Choose at least 2 options"),
        "few": (FormShapeKind.MAX_ITEMS, {"maxItems": 1}, "Choose at most 1 option"),
        "size": (FormShapeKind.ENUM, {}, "Choose one of the offered options"),
        "after": (FormShapeKind.MIN_DATE, {"minDate": "2026-01-01"}, "Choose a date on or after 2026-01-01"),
        "before": (FormShapeKind.MAX_DATE, {"maxDate": "2026-12-31"}, "Choose a date on or before 2026-12-31"),
        "open": (FormShapeKind.DATE_UNAVAILABLE, {"date": "2026-10-18"}, "That date is unavailable, choose another"),
        "end": (
            FormShapeKind.RANGE_SPAN,
            {"minDays": 2, "maxDays": 5, "days": 11},
            "Choose between 2 and 5 days",
        ),
        "even": (FormShapeKind.OTHER, {"keyword": "multipleOf"}, "Not an accepted value"),
    }


def test_a_conforming_answer_has_no_error():
    good = {
        "name": "alice",
        "count": 2,
        "ratio": 0.5,
        "code": "1234",
        "email": "alice@example.org",
        "at": "09:30",
        "stamp": "2026-01-02T03:04:05Z",
        "day": "2026-02-28",
        "nick": "abc",
        "note": "ok",
        "qty": 1,
        "cap": 9.5,
        "tags": ["a", "b"],
        "few": ["a"],
        "size": "s",
        "after": "2026-01-01",
        "before": "2026-12-31",
        "open": "2026-10-19",
        "start": "2026-03-10",
        "end": "2026-03-12",
        "even": 4,
    }
    assert form_shape_errors(_form(_EVERY_KIND), good) == []


def test_range_order_and_the_one_sided_span_sentences():
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 3},
            "back": {"type": "string", "format": "date"},
            "until": {"type": "string", "format": "date", "rangeStart": "back", "maxDays": 2},
        },
    }
    errors = form_shape_errors(
        _form(schema), {"start": "2026-03-10", "end": "2026-03-09", "back": "2026-03-01", "until": "2026-03-05"}
    )
    assert _kinds(errors) == {
        "end": (
            FormShapeKind.RANGE_ORDER,
            {"rangeStart": "start", "by": "date"},
            "The end date must be on or after the start date",
        ),
        "until": (FormShapeKind.RANGE_SPAN, {"maxDays": 2, "days": 5}, "Choose at most 2 days"),
    }
    errors = form_shape_errors(_form(schema), {"start": "2026-03-10", "end": "2026-03-11"})
    assert _kinds(errors)["end"] == (FormShapeKind.RANGE_SPAN, {"minDays": 3, "days": 2}, "Choose at least 3 days")


def test_singular_counts_read_as_one():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "minLength": 1},
            "b": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
    }
    assert [error.message for error in form_shape_errors(_form(schema), {"a": "", "b": []})] == [
        "Enter at least 1 character",
        "Choose at least 1 option",
    ]


def test_an_array_item_fault_is_the_array_fields_error():
    schema = {"type": "object", "properties": {"picks": {"type": "array", "items": {"type": "string", "enum": ["x"]}}}}
    assert _kinds(form_shape_errors(_form(schema), {"picks": ["x", "y"]})) == {
        "picks": (FormShapeKind.ENUM, {}, "Choose one of the offered options")
    }


def test_partial_demands_nothing_and_checks_only_what_is_present():
    errors = form_shape_errors(_form(_EVERY_KIND), {"email": "dhdhdh"}, partial=True)
    assert _kinds(errors) == {"email": (FormShapeKind.FORMAT, {"format": "email"}, "Enter a valid email address")}
    assert form_shape_errors(_form(_EVERY_KIND), {}, partial=True) == []


def test_fields_restrict_the_check_to_one_step():
    errors = form_shape_errors(_form(_EVERY_KIND), _EVERY_BAD_VALUE, fields=["qty", "email"])
    assert [error.field for error in errors] == ["email", "qty"]
    # ``required`` is restricted to the named fields too.
    assert form_shape_errors(_form(_EVERY_KIND), {}, fields=["qty"]) == []


def test_a_range_end_on_this_step_reads_its_start_from_an_earlier_step():
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start"},
        },
    }
    errors = form_shape_errors(_form(schema), {"start": "2026-03-10", "end": "2026-03-01"}, fields=["end"])
    assert [(error.field, error.kind) for error in errors] == [("end", FormShapeKind.RANGE_ORDER)]


_TIMED_RANGE = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "format": "date"},
        "end": {"type": "string", "format": "date", "rangeStart": "start"},
        "start_time": {"type": "string", "format": "time", "timeOf": "start"},
        "end_time": {"type": "string", "format": "time", "timeOf": "end"},
    },
}


def test_a_same_day_earlier_end_time_is_the_end_times_error_with_the_time_sentence():
    answer = {"start": "2026-03-10", "end": "2026-03-10", "start_time": "10:00", "end_time": "09:00"}
    assert _kinds(form_shape_errors(_form(_TIMED_RANGE), answer)) == {
        "end_time": (
            FormShapeKind.RANGE_ORDER,
            {"rangeStart": "start", "by": "time"},
            "The end time must be on or after the start time",
        )
    }
    errors = form_shape_errors(_form(_TIMED_RANGE), answer, fields=["end", "end_time"])
    assert [(error.field, error.kind) for error in errors] == [("end_time", FormShapeKind.RANGE_ORDER)]


def test_shape_sentence_reads_the_order_by_date_or_by_time():
    by_date = "The end date must be on or after the start date"
    assert shape_sentence(FormShapeKind.RANGE_ORDER, {"by": "date"}) == by_date
    assert (
        shape_sentence(FormShapeKind.RANGE_ORDER, {"by": "time"}) == "The end time must be on or after the start time"
    )
    assert shape_sentence(FormShapeKind.RANGE_ORDER, {}) == by_date


def test_a_hidden_field_is_neither_demanded_nor_checked():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["mail", "none"]},
            "email": {"type": "string", "format": "email", "visibleWhen": {"field": "mode", "equals": "mail"}},
        },
        "required": ["mode", "email"],
    }
    assert form_shape_errors(_form(schema), {"mode": "none", "email": "dhdhdh"}) == []
    assert [error.kind for error in form_shape_errors(_form(schema), {"mode": "mail"})] == [FormShapeKind.REQUIRED]


def test_per_send_options_and_reaction_fed_choices():
    schema = {
        "type": "object",
        "properties": {"slot": {"type": "string"}, "fed": {"type": "string"}},
    }
    data = {"options": {"slot": [{"value": "9am"}], "fed": [{"value": "x"}]}}
    question = _form(schema, data=data, reactions={"choices": ["fed"], "submitted": True})
    errors = form_shape_errors(question, {"slot": "10am", "fed": "anything"})
    assert _kinds(errors) == {"slot": (FormShapeKind.ENUM, {}, "Choose one of the offered options")}


def test_an_exclusive_bound_is_another_keyword():
    schema = {"type": "object", "properties": {"n": {"type": "number", "exclusiveMinimum": 0}}}
    assert _kinds(form_shape_errors(_form(schema), {"n": 0})) == {
        "n": (FormShapeKind.OTHER, {"keyword": "exclusiveMinimum"}, "Not an accepted value")
    }


def test_a_question_that_is_not_a_form_with_a_schema_is_refused():
    with pytest.raises(ValueError, match="FORM question"):
        form_shape_errors(QuestionFormat(answer_format=AnswerFormat.FREE, format_payload={"schema": {}}), {})
    with pytest.raises(ValueError, match="object schema"):
        form_shape_errors(QuestionFormat(answer_format=AnswerFormat.FORM, format_payload={}), {})


@pytest.mark.parametrize("kind", list(FormShapeKind))
def test_every_kind_has_a_plain_sentence_even_without_its_bound(kind):
    sentence = shape_sentence(kind, {})
    assert isinstance(sentence, str)
    assert sentence
    assert "{" not in sentence


class _StepSurface:
    """A neutral, test-only form surface: it holds a stepped form and asks the platform facet alone.

    It is no copy of any channel: it knows nothing of a vendor's controls or caps. It checks one step
    before advancing and the whole answer before submitting, and words each failing field with the
    caller's sentence when one is given, else the platform's.
    """

    def __init__(self, question: QuestionFormat, steps: list[list[str]], words: dict[str, str]) -> None:
        self.question = question
        self.steps = steps
        self.words = words

    def advance(self, step: int, values: dict[str, Any]) -> dict[str, str]:
        errors = tai42_app.interactions.form_shape_errors(self.question, values, fields=self.steps[step], partial=True)
        return {error.field: self.words.get(error.field, error.message) for error in errors}

    def submit(self, values: dict[str, Any]) -> dict[str, tuple[FormShapeKind, dict[str, Any]]]:
        return {
            error.field: (error.kind, error.params)
            for error in tai42_app.interactions.form_shape_errors(self.question, values)
        }


def test_a_neutral_surface_checks_a_stepped_form_through_the_facet_alone():
    surface = _StepSurface(
        _form(_EVERY_KIND),
        [
            ["name", "email", "qty"],
            [name for name in _EVERY_KIND["properties"] if name not in ("name", "email", "qty")],
        ],
        {"email": "Use your work address"},
    )
    with tai42_app.bound(app):
        assert surface.advance(0, {"email": "dhdhdh", "qty": 0}) == {
            "email": "Use your work address",
            "qty": "Enter a number of at least 1",
        }
        assert surface.advance(0, {"email": "alice@example.org", "qty": 2}) == {}
        submitted = surface.submit({"email": "alice@example.org", "qty": 2, **_EVERY_BAD_VALUE})
    assert submitted["name"] == (FormShapeKind.REQUIRED, {})
    assert submitted["qty"] == (FormShapeKind.MINIMUM, {"minimum": 1})
    assert submitted["end"] == (FormShapeKind.RANGE_SPAN, {"minDays": 2, "maxDays": 5, "days": 11})
    assert len(submitted) == len(_EVERY_KIND["properties"]) - 1
