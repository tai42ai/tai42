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
