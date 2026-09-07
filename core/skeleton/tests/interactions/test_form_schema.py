"""The channel-deliverable form-schema subset — ``validate_channel_form_schema``
and the shared ``channel_form_fields`` walk that both ``ask_user`` and the callback
form renderer use as the ONE definition of the subset.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from tai42_skeleton.interactions.form_schema import (
    channel_form_fields,
    effective_answer_schema,
    validate_channel_form_schema,
)


def test_effective_answer_schema_replaces_enum_with_per_send_options():
    schema = {"type": "object", "properties": {"color": {"type": "string", "enum": ["red", "blue"]}}}
    data = {"options": {"color": [{"value": "green"}, {"value": "amber", "label": "Amber"}]}}
    effective = effective_answer_schema(schema, data)
    assert effective["properties"]["color"]["enum"] == ["green", "amber"]
    # The original schema is not mutated.
    assert schema["properties"]["color"]["enum"] == ["red", "blue"]


def test_effective_answer_schema_applies_array_options_to_items():
    # For a multi-select (array) property the per-send choices belong on ``items``,
    # not the array itself — an ``enum`` on the array would demand the whole submitted
    # list equal one option, rejecting every real multi-select answer.
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    }
    data = {"options": {"tags": [{"value": "a"}, {"value": "b", "label": "Bee"}]}}
    effective = effective_answer_schema(schema, data)
    prop = effective["properties"]["tags"]
    assert "enum" not in prop
    assert prop["items"] == {"type": "string", "enum": ["a", "b"]}
    # The original schema is not mutated.
    assert schema["properties"]["tags"]["items"] == {"type": "string"}


def test_effective_answer_schema_no_options_returns_schema_unchanged():
    schema = {"type": "object", "properties": {"color": {"type": "string"}}}
    assert effective_answer_schema(schema, None) is schema
    assert effective_answer_schema(schema, {"values": {"color": "x"}}) is schema


class _OptForm(BaseModel):
    # ``str | None`` renders as ``anyOf`` (no scalar ``type``) in JSON schema — a
    # nullable/optional pydantic field is outside the subset.
    name: str | None = None


def _valid_subset() -> dict:
    return {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "title": "Name"},
            "color": {"type": "string", "enum": ["red", "blue"]},
            "agree": {"type": "boolean"},
            "count": {"type": "integer"},
            "score": {"type": "number"},
        },
    }


def test_accepts_the_full_valid_subset():
    schema = _valid_subset()
    assert validate_channel_form_schema(schema) is None
    fields = channel_form_fields(schema)
    assert [(name, is_required) for name, _prop, is_required in fields] == [
        ("name", True),
        ("color", False),
        ("agree", False),
        ("count", False),
        ("score", False),
    ]


def test_rejects_non_object_root():
    with pytest.raises(ValueError, match="top-level type must be 'object'"):
        validate_channel_form_schema({"type": "array", "properties": {"x": {"type": "string"}}})


def test_rejects_empty_properties():
    with pytest.raises(ValueError, match="non-empty object 'properties'"):
        validate_channel_form_schema({"type": "object", "properties": {}})


def test_rejects_missing_properties():
    with pytest.raises(ValueError, match="non-empty object 'properties'"):
        validate_channel_form_schema({"type": "object"})


def test_rejects_array_property():
    with pytest.raises(ValueError, match="property 'tags' has type 'array'"):
        validate_channel_form_schema({"type": "object", "properties": {"tags": {"type": "array"}}})


def test_rejects_nested_object_property():
    with pytest.raises(ValueError, match="property 'sub' has type 'object'"):
        validate_channel_form_schema({"type": "object", "properties": {"sub": {"type": "object", "properties": {}}}})


def test_rejects_missing_type_property():
    # A pydantic ``str | None`` -> ``anyOf`` with no scalar ``type``.
    with pytest.raises(ValueError, match="property 'name' has type None"):
        validate_channel_form_schema(_OptForm.model_json_schema())


def test_rejects_ref_property():
    with pytest.raises(ValueError, match="property 'sub' has type None"):
        validate_channel_form_schema({"type": "object", "properties": {"sub": {"$ref": "#/$defs/Sub"}}})


def test_rejects_enum_on_non_string():
    with pytest.raises(ValueError, match="property 'n' has an enum but is not a 'string'"):
        validate_channel_form_schema({"type": "object", "properties": {"n": {"type": "integer", "enum": [1, 2]}}})


def test_rejects_empty_enum():
    with pytest.raises(ValueError, match="property 'c' enum must be a non-empty list"):
        validate_channel_form_schema({"type": "object", "properties": {"c": {"type": "string", "enum": []}}})


def test_rejects_non_string_enum_values():
    with pytest.raises(ValueError, match="property 'c' enum must contain only strings"):
        validate_channel_form_schema({"type": "object", "properties": {"c": {"type": "string", "enum": ["a", 2]}}})


def test_rejects_required_naming_undeclared_property():
    with pytest.raises(ValueError, match="'required' names undeclared properties: \\['ghost'\\]"):
        validate_channel_form_schema({"type": "object", "required": ["ghost"], "properties": {"x": {"type": "string"}}})


def test_rejects_non_list_required():
    with pytest.raises(ValueError, match="'required' must be a list"):
        validate_channel_form_schema({"type": "object", "required": "x", "properties": {"x": {"type": "string"}}})


def test_rejects_non_object_property():
    with pytest.raises(ValueError, match="property 'x' must be an object"):
        validate_channel_form_schema({"type": "object", "properties": {"x": "string"}})
