"""The ``ask`` argument-shaping logic: ``build_payload`` per answer format,
and the unknown-format guard at the top of ``ask``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import BaseModel
from tai42_contract.interactions import AnswerFormat

from tai42_skeleton.interactions import ask
from tai42_skeleton.interactions.ask.payload import build_payload


class _Form(BaseModel):
    name: str


def test_text_format_has_no_payload():
    assert build_payload(AnswerFormat.TEXT, None, None) is None
    assert build_payload(AnswerFormat.CONFIRM, None, None) is None


def test_free_format_carries_its_schema_when_given():
    assert build_payload(AnswerFormat.FREE, None, {"type": "integer"}) == {"schema": {"type": "integer"}}
    assert build_payload(AnswerFormat.FREE, None, None) is None


def test_select_requires_options():
    assert build_payload(AnswerFormat.SELECT, ["a", "b"], None) == {"options": ["a", "b"]}
    with pytest.raises(ValueError, match="requires options"):
        build_payload(AnswerFormat.SELECT, None, None)


def test_form_from_pydantic_model():
    payload = build_payload(AnswerFormat.FORM, None, _Form)
    assert payload == {"schema": _Form.model_json_schema()}


def test_form_from_json_schema_dict():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert build_payload(AnswerFormat.FORM, None, schema) == {"schema": schema}


def test_form_requires_a_schema():
    with pytest.raises(ValueError, match="requires a schema"):
        build_payload(AnswerFormat.FORM, None, None)


def test_form_rejects_bad_schema_type():
    with pytest.raises(ValueError, match="pydantic model or a JSON-schema dict"):
        # Deliberately feed an invalid runtime type to exercise the
        # bad-schema-type guard; cast satisfies the static schema-param type.
        build_payload(AnswerFormat.FORM, None, cast("type[BaseModel]", 123))


def test_form_payload_carries_data_and_pages():
    from tai42_contract.interactions import FormData, FormOption, FormPage

    schema = {"type": "object", "properties": {"c": {"type": "string"}}}
    data = FormData(values={"c": "x"}, options={"c": [FormOption(value="x", label="X")]})
    pages = [FormPage(title="A", fields=["c"])]
    payload = build_payload(AnswerFormat.FORM, None, schema, data=data, pages=pages)
    assert payload is not None
    assert payload["schema"] == schema
    assert payload["data"] == {
        "values": {"c": "x"},
        "options": {"c": [{"value": "x", "label": "X", "description": None}]},
    }
    assert payload["pages"] == [{"title": "A", "fields": ["c"], "display": [], "kind": "input", "button_label": None}]


def test_form_payload_accepts_dict_data_and_pages():
    schema = {"type": "object", "properties": {"c": {"type": "string"}}}
    payload = build_payload(
        AnswerFormat.FORM,
        None,
        schema,
        data={"values": {"c": "x"}, "options": {}},
        pages=[{"title": "A", "fields": ["c"]}],
    )
    assert payload is not None
    assert payload["data"] == {"values": {"c": "x"}, "options": {}}
    assert payload["pages"] == [{"title": "A", "fields": ["c"], "display": [], "kind": "input", "button_label": None}]


def test_form_payload_omits_absent_data_and_pages():
    schema = {"type": "object", "properties": {"c": {"type": "string"}}}
    assert build_payload(AnswerFormat.FORM, None, schema) == {"schema": schema}


async def test_ask_rejects_data_on_non_form():
    with pytest.raises(ValueError, match="data and pages are not valid"):
        await ask("q", answer_format="text", data={"values": {}})
    with pytest.raises(ValueError, match="data and pages are not valid"):
        await ask("q", answer_format="select", options=["a"], pages=[{"title": "A", "fields": ["a"]}])


async def test_ask_rejects_unknown_format():
    with pytest.raises(ValueError, match="unknown answer_format"):
        await ask("q", answer_format="telepathy")


def test_form_payload_carries_reactions():
    schema = {"type": "object", "properties": {"c": {"type": "string", "enum": ["x", "y"]}}}
    payload = build_payload(
        AnswerFormat.FORM, None, schema, reactions={"field_changed": ["c"], "choices": ["c"], "submitted": True}
    )
    assert payload is not None
    assert payload["reactions"] == {
        "field_changed": ["c"],
        "page_advanced": [],
        "submitted": True,
        "choices": ["c"],
    }


def test_delivery_frame_carries_reactions():
    from datetime import UTC, datetime, timedelta

    from tai42_contract.interactions import AnswerMismatchPolicy, FormReactions

    from tai42_skeleton.interactions.ask.delivery import build_delivery_frame

    schema = {"type": "object", "properties": {"c": {"type": "string"}}}
    payload = build_payload(AnswerFormat.FORM, None, schema, reactions={"field_changed": ["c"]})

    frame = build_delivery_frame(
        interaction_id="i1",
        recipient=None,
        question="?",
        fmt=AnswerFormat.FORM,
        options=None,
        format_payload=payload,
        stored_media=None,
        on_mismatch=AnswerMismatchPolicy.RETRY,
        callback_url="https://cb.example/x",
        timeout_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    assert frame.reactions == FormReactions(field_changed=["c"])


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "integer", "minimum": True},
        {"type": "string", "pattern": "("},
        {"type": "number", "minimum": float("nan")},
        {"type": "integer", "minimum": 5, "maximum": 1},
        {"type": ["number", "null"], "minimum": float("inf")},
        {"type": "string", "pattern": "a{4294967296}"},
    ],
)
async def test_ask_refuses_an_inbox_form_no_answer_could_meet(prop):
    # A form asked with no channel is answered in the in-app inbox through the same answer door,
    # so a schema that door could never pass an answer through is refused when the form is asked.
    with pytest.raises(ValueError, match="form schema property 'qty'"):
        await ask("q", answer_format="form", schema={"type": "object", "properties": {"qty": prop}})


@pytest.mark.parametrize(
    "extra",
    [
        {"answer_format": "free", "to": "caller", "mode": "async", "expiry_at": datetime.now(UTC) + timedelta(hours=1)},
        {"answer_format": "external", "link": "{callback_url}"},
    ],
)
async def test_ask_refuses_an_answer_schema_breaking_the_json_schema_rules(extra):
    with pytest.raises(ValueError, match=re.escape("answer schema breaks the JSON Schema rules at type: ")):
        await ask("q", schema={"type": "bogus"}, **extra)


_FREE = {"answer_format": "free", "to": "caller", "mode": "async", "expiry_at": datetime.now(UTC) + timedelta(hours=1)}
_EXTERNAL = {"answer_format": "external", "link": "{callback_url}"}


@pytest.mark.parametrize("extra", [_FREE, _EXTERNAL])
@pytest.mark.parametrize("dialect", [5, ["x"], None, "http://[unclosed"])
async def test_ask_refuses_an_answer_schema_naming_no_dialect_string(extra, dialect):
    with pytest.raises(ValueError, match=re.escape("answer schema breaks the JSON Schema rules at $schema: ")):
        await ask("q", schema={"$schema": dialect, "type": "string"}, **extra)


@pytest.mark.parametrize("extra", [_FREE, _EXTERNAL])
@pytest.mark.parametrize(
    ("schema", "where"),
    [
        ({"type": "number", "minimum": float("inf")}, "at minimum"),
        ({"type": "array", "items": {"enum": [1, float("nan")]}}, "at items/enum/1"),
        (
            {"type": "object", "properties": {"n": {"type": "number", "maximum": float("-inf")}}},
            "property 'n' has a value at maximum",
        ),
    ],
)
async def test_ask_refuses_an_answer_schema_carrying_a_non_finite_number(extra, schema, where):
    with pytest.raises(ValueError, match=re.escape(where)) as caught:
        await ask("q", schema=schema, **extra)
    assert "that is not a finite number" in str(caught.value)


def _cyclic_schema() -> dict:
    # A schema dict that holds itself: only an in-process caller can pass one (JSON has no cycles).
    schema: dict = {"type": "object"}
    schema["properties"] = {"x": schema}
    return schema


@pytest.mark.parametrize("extra", [_FREE, _EXTERNAL, {"answer_format": "form"}])
async def test_ask_refuses_a_schema_that_holds_itself(extra):
    subject = "form schema" if extra.get("answer_format") == "form" else "answer schema"
    with pytest.raises(
        ValueError,
        match=re.escape(
            f"{subject} property 'x' has a value that loops back to a value it sits inside; "
            "a schema must be plain JSON, which has no cycles"
        ),
    ):
        await ask("q", schema=_cyclic_schema(), **extra)


_DRAFT_04 = "http://json-schema.org/draft-04/schema#"
_DRAFT_03 = "http://json-schema.org/draft-03/schema#"


@pytest.mark.parametrize("extra", [_FREE, _EXTERNAL])
@pytest.mark.parametrize("dialect", [_DRAFT_04, _DRAFT_03])
@pytest.mark.parametrize("key", ["[", "a{4294967296}"])
async def test_ask_refuses_a_pattern_properties_key_that_does_not_compile(extra, dialect, key):
    schema = {"$schema": dialect, "type": "object", "patternProperties": {key: {}}}
    with pytest.raises(
        ValueError,
        match=re.escape(f"answer schema breaks the JSON Schema rules at patternProperties: {key!r} is not a 'regex'"),
    ):
        await ask("q", schema=schema, **extra)


@pytest.mark.parametrize("extra", [_FREE, _EXTERNAL])
@pytest.mark.parametrize("dialect", [None, _DRAFT_04, _DRAFT_03])
@pytest.mark.parametrize("keys", [["a", "(?i)b"], ["(?P<n>a)", "(?P<n>b)"]])
async def test_ask_refuses_pattern_properties_keys_additional_properties_cannot_join(extra, dialect, keys):
    # Each key compiles alone, but ``additionalProperties`` matches every other answer key against
    # the keys joined with ``|``, which does not compile.
    schema = {"type": "object", "patternProperties": {key: {} for key in keys}, "additionalProperties": False}
    if dialect is not None:
        schema["$schema"] = dialect
    with pytest.raises(
        ValueError,
        match=re.escape(
            "answer schema breaks the JSON Schema rules at patternProperties: the keys joined with '|', "
            "which additionalProperties matches every other key against, do not compile: "
        ),
    ):
        await ask("q", schema=schema, **extra)


async def test_ask_refuses_an_inbox_form_property_whose_pattern_properties_cannot_join():
    prop = {"type": "object", "patternProperties": {"a": {}, "(?i)b": {}}, "additionalProperties": False}
    with pytest.raises(ValueError, match=re.escape("form schema property 'qty' breaks the JSON Schema rules: ")):
        await ask("q", answer_format="form", schema={"type": "object", "properties": {"qty": prop}})


@pytest.mark.parametrize("extra", [_FREE, _EXTERNAL])
@pytest.mark.parametrize("dialect", [_DRAFT_04, _DRAFT_03])
async def test_ask_refuses_a_nested_pattern_properties_key_naming_its_property(extra, dialect):
    nested = {"type": "array", "items": {"type": "object", "patternProperties": {"[": {}}}}
    schema = {"$schema": dialect, "type": "object", "properties": {"rows": nested}}
    with pytest.raises(
        ValueError, match=re.escape("answer schema property 'rows' breaks the JSON Schema rules: '[' is not a 'regex'")
    ):
        await ask("q", schema=schema, **extra)
