"""Oracles for the shared ``check_answer`` — the one answer check every interactions door reaches.

Each format's rule, the field-located schema mismatch, and FREE/EXTERNAL's schema-only behavior
are pinned directly against :func:`tai42_skeleton.interactions.answer_check.check_answer`, the
callable behind the ``tai42_app.interactions.check_answer`` facet.
"""

from __future__ import annotations

import pytest
from tai42_contract.interactions import AnswerFormat, AnswerMismatchError, QuestionFormat

from tai42_skeleton.interactions.answer_check import check_answer, schema_mismatch


def _qf(fmt: AnswerFormat, payload: dict | None = None) -> QuestionFormat:
    return QuestionFormat(answer_format=fmt, format_payload=payload)


def test_text_requires_a_string():
    check_answer(_qf(AnswerFormat.TEXT), "hello")
    with pytest.raises(AnswerMismatchError, match="must be a string"):
        check_answer(_qf(AnswerFormat.TEXT), 5)


def test_confirm_requires_a_boolean():
    check_answer(_qf(AnswerFormat.CONFIRM), True)
    with pytest.raises(AnswerMismatchError, match="must be a boolean"):
        check_answer(_qf(AnswerFormat.CONFIRM), "yes")


def test_select_must_be_an_offered_option():
    qf = _qf(AnswerFormat.SELECT, {"options": ["a", "b"]})
    check_answer(qf, "a")
    with pytest.raises(AnswerMismatchError, match="must be one of"):
        check_answer(qf, "z")


def test_form_must_conform_and_names_the_failing_field():
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    qf = _qf(AnswerFormat.FORM, {"schema": schema})
    check_answer(qf, {"count": 3})
    with pytest.raises(AnswerMismatchError, match="at count:") as exc:
        check_answer(qf, {"count": "abc"})
    assert exc.value.field == "count"


def test_form_rejects_a_non_object_and_a_missing_schema():
    with pytest.raises(AnswerMismatchError, match="must be an object"):
        check_answer(_qf(AnswerFormat.FORM, {"schema": {"type": "object"}}), "not-a-dict")
    with pytest.raises(AnswerMismatchError, match="schema is invalid"):
        check_answer(_qf(AnswerFormat.FORM, {}), {"x": 1})


@pytest.mark.parametrize(
    ("fmt", "good", "bad"),
    [
        ("date", "2024-01-15", "2024-02-30"),
        ("time", "09:30", "24:00"),
        ("date-time", "2024-01-15T10:30:00Z", "2024-01-15T10:30:00"),
    ],
)
def test_form_asserts_the_string_formats_on_the_answer(fmt, good, bad):
    schema = {"type": "object", "properties": {"when": {"type": "string", "format": fmt}}}
    qf = _qf(AnswerFormat.FORM, {"schema": schema})
    check_answer(qf, {"when": good})
    with pytest.raises(AnswerMismatchError, match="at when:") as exc:
        check_answer(qf, {"when": bad})
    assert exc.value.field == "when"


def test_form_date_time_rejects_a_missing_offset_and_a_bad_time():
    # An RFC 3339 date-time needs a Z/offset; a time out of range fails its own format.
    dt = _qf(
        AnswerFormat.FORM,
        {"schema": {"type": "object", "properties": {"at": {"type": "string", "format": "date-time"}}}},
    )
    check_answer(dt, {"at": "2024-01-15T10:30:00+01:00"})
    with pytest.raises(AnswerMismatchError, match="at at:"):
        check_answer(dt, {"at": "2024-01-15T10:30:00"})  # no offset
    tm = _qf(
        AnswerFormat.FORM, {"schema": {"type": "object", "properties": {"t": {"type": "string", "format": "time"}}}}
    )
    check_answer(tm, {"t": "09:30:00"})
    with pytest.raises(AnswerMismatchError, match="at t:"):
        check_answer(tm, {"t": "24:00"})


@pytest.mark.parametrize(
    ("fmt", "bad"),
    [
        # Regex-matching values whose calendar/wall-clock is invalid, so the shape passes
        # but the stdlib parse rejects them: the format checker returns False either way.
        ("time", "9:30"),  # single-digit hour: fails the HH:MM regex
        ("date-time", "2024-02-30T10:30:00Z"),  # shape ok, calendar impossible
    ],
)
def test_form_format_rejects_shape_ok_but_invalid_value(fmt, bad):
    qf = _qf(AnswerFormat.FORM, {"schema": {"type": "object", "properties": {"v": {"type": "string", "format": fmt}}}})
    with pytest.raises(AnswerMismatchError, match="at v:"):
        check_answer(qf, {"v": bad})


@pytest.mark.parametrize("fmt", ["date", "time", "date-time"])
def test_form_format_leaves_a_non_string_to_the_type_check(fmt):
    # A format checker only judges strings; a non-string value is a plain type mismatch,
    # never a format complaint, so the checker passes it through to the ``type`` keyword.
    qf = _qf(AnswerFormat.FORM, {"schema": {"type": "object", "properties": {"v": {"type": "string", "format": fmt}}}})
    with pytest.raises(AnswerMismatchError, match="not of type 'string'"):
        check_answer(qf, {"v": 5})


def test_schema_only_asserts_a_declared_format():
    # A FREE/EXTERNAL schema that declares one of the three formats has it enforced too,
    # since every answer door flows through the same ``schema_mismatch`` seam.
    schema = {"type": "object", "properties": {"d": {"type": "string", "format": "date"}}, "required": ["d"]}
    check_answer(_qf(AnswerFormat.FREE, {"schema": schema}), {"d": "2024-01-15"})
    with pytest.raises(AnswerMismatchError, match="does not match schema"):
        check_answer(_qf(AnswerFormat.FREE, {"schema": schema}), {"d": "nope"})


def test_free_accepts_any_json_without_a_schema():
    check_answer(_qf(AnswerFormat.FREE), "anything")
    check_answer(_qf(AnswerFormat.FREE), {"a": [1, 2, 3]})
    check_answer(_qf(AnswerFormat.FREE, {}), 42)


def test_free_checks_only_against_a_given_schema():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    qf = _qf(AnswerFormat.FREE, {"schema": schema})
    check_answer(qf, {"n": 7})
    with pytest.raises(AnswerMismatchError, match="does not match schema"):
        check_answer(qf, {"n": "x"})


def test_external_checks_only_against_a_given_schema():
    # EXTERNAL is verbatim unless the question declared a schema, exactly like FREE.
    check_answer(_qf(AnswerFormat.EXTERNAL, {"url": "https://x"}), {"any": 1})
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    qf = _qf(AnswerFormat.EXTERNAL, {"url": "https://x", "schema": schema})
    check_answer(qf, {"ok": True})
    with pytest.raises(AnswerMismatchError, match="does not match schema"):
        check_answer(qf, {"ok": "nope"})


def test_schema_mismatch_field_paths():
    # ``schema_mismatch`` returns ``(message, field)``: a field-level error names the field
    # (``$``-root and leading ``.`` stripped) in BOTH the message and the bare ``field``; a
    # root-level error and a pathless validator carry ``field=None`` and fall back to the bare
    # message.
    root = schema_mismatch({"y": 1}, {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}})
    assert root == ("answer does not match schema: 'x' is a required property", None)
    field = schema_mismatch({"count": "abc"}, {"type": "object", "properties": {"count": {"type": "integer"}}})
    assert field == ("answer does not match schema at count: 'abc' is not of type 'integer'", "count")
    nested = schema_mismatch(
        {"a": {"b": "x"}},
        {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "integer"}}}}},
    )
    assert nested == ("answer does not match schema at a.b: 'x' is not of type 'integer'", "a.b")
    # A malformed stored schema raises SchemaError, whose json_path points into the schema
    # (``properties.x.type``) — a location no answering human owns; the message carries NO
    # "at ..." path, only the bare reason, and the field is ``None``.
    schema_error = schema_mismatch({"x": 1}, {"type": "object", "properties": {"x": {"type": "bogus"}}})
    assert schema_error is not None
    message, field_path = schema_error
    assert field_path is None
    assert "at " not in message
    assert message.startswith("answer does not match schema: ")


# === date bounds / range enforced at the one facet ==========================


def _form(schema: dict, **payload_extra) -> QuestionFormat:
    return _qf(AnswerFormat.FORM, {"schema": schema, **payload_extra})


def test_form_date_bound_raises_naming_the_field():
    schema = {
        "type": "object",
        "properties": {"d": {"type": "string", "format": "date", "minDate": "2024-01-01", "maxDate": "2024-12-31"}},
    }
    check_answer(_form(schema), {"d": "2024-06-01"})
    with pytest.raises(AnswerMismatchError) as exc:
        check_answer(_form(schema), {"d": "2023-01-01"})
    assert exc.value.field == "d"


def test_form_unavailable_day_raises():
    schema = {
        "type": "object",
        "properties": {"d": {"type": "string", "format": "date", "unavailableDates": ["sunday"]}},
    }
    with pytest.raises(AnswerMismatchError) as exc:
        check_answer(_form(schema), {"d": "2024-07-07"})  # a Sunday
    assert exc.value.field == "d"


def test_form_range_order_and_span_enforced():
    schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date"},
            "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 2, "maxDays": 3},
        },
    }
    check_answer(_form(schema), {"start": "2024-01-01", "end": "2024-01-02"})
    with pytest.raises(AnswerMismatchError) as exc:
        check_answer(_form(schema), {"start": "2024-01-05", "end": "2024-01-01"})
    assert exc.value.field == "end"


# === conditional hidden-field removal =======================================


def _conditional_schema(required: list[str] | None = None) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"]},
            "detail": {"type": "string", "visibleWhen": {"field": "mode", "equals": "a"}},
        },
    }
    if required is not None:
        schema["required"] = required
    return schema


def test_hidden_field_value_is_dropped_not_faulted():
    # ``detail`` is hidden when mode != "a"; a value submitted for it is DROPPED from the
    # answer the consumer receives, never an error (the degraded show-all path).
    result = check_answer(_form(_conditional_schema()), {"mode": "b", "detail": "leftover"})
    assert result == {"mode": "b"}


def test_visible_field_is_kept():
    result = check_answer(_form(_conditional_schema()), {"mode": "a", "detail": "kept"})
    assert result == {"mode": "a", "detail": "kept"}


def test_hidden_required_field_is_not_demanded():
    # ``detail`` is required but hidden by its predicate — it must not be demanded.
    result = check_answer(_form(_conditional_schema(required=["detail"])), {"mode": "b"})
    assert result == {"mode": "b"}


# === reaction-fed choice: type-only at submit ===============================


def _reacting_choice_form(schema: dict) -> QuestionFormat:
    return _qf(AnswerFormat.FORM, {"schema": schema, "reactions": {"choices": ["slot"], "submitted": True}})


def test_reaction_fed_choice_accepts_a_value_outside_the_send_enum():
    schema = {"type": "object", "properties": {"slot": {"type": "string", "enum": ["9am", "10am"]}}}
    # A value NOT in the send-time enum passes — membership is the consumer's at submit.
    check_answer(_reacting_choice_form(schema), {"slot": "11am"})


def test_reaction_fed_choice_still_type_checks():
    schema = {"type": "object", "properties": {"slot": {"type": "string", "enum": ["9am"]}}}
    with pytest.raises(AnswerMismatchError):
        check_answer(_reacting_choice_form(schema), {"slot": 5})
