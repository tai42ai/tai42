"""typed_dict: the remaining schema shapes — type lists, unions, allOf, const/enum/not,
maps, prefixItems, and the loud refusals (depth, unresolved and recursive ``$ref``)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from tai42_kit.utils.data.json_schema_util import json_schema_to_typed_dict


def _adapter(schema: dict[str, Any], **kwargs: Any) -> TypeAdapter[Any]:
    return TypeAdapter(json_schema_to_typed_dict(schema, **kwargs))


def test_a_type_list_accepts_each_member() -> None:
    adapter = _adapter(
        {
            "type": ["object", "array", "integer", "null"],
            "title": "Multi",
            "properties": {"k": {"type": "integer"}},
            "required": ["k"],
            "items": {"type": "string"},
        }
    )
    assert adapter.validate_python(None) is None
    assert adapter.validate_python(3) == 3
    assert adapter.validate_python(["a"]) == ["a"]
    assert adapter.validate_python({"k": 1}) == {"k": 1}


def test_any_of_and_one_of_build_unions() -> None:
    any_of = _adapter({"anyOf": [{"type": "integer", "maximum": 5}, {"type": "string"}]})
    assert any_of.validate_python(4) == 4
    assert any_of.validate_python("x") == "x"
    with pytest.raises(ValidationError):
        any_of.validate_python(9)
    one_of = _adapter({"oneOf": [{"type": "boolean"}, {"type": "null"}]})
    assert one_of.validate_python(True) is True


def test_an_empty_union_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one subschema"):
        json_schema_to_typed_dict({"anyOf": []})


def test_all_of_objects_merge_and_a_non_object_member_is_any() -> None:
    merged = _adapter(
        {
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
                {"properties": {"b": {"type": "string"}}},
            ]
        }
    )
    assert merged.validate_python({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}
    with pytest.raises(ValidationError):
        merged.validate_python({"b": "x"})
    assert json_schema_to_typed_dict({"allOf": [{"type": "integer"}]}) is Any


def test_const_enum_not_and_untyped() -> None:
    assert _adapter({"const": "on"}).validate_python("on") == "on"
    with pytest.raises(ValidationError):
        _adapter({"enum": ["a", "b"]}).validate_python("c")
    assert json_schema_to_typed_dict({"not": {"type": "string"}}) is Any
    assert json_schema_to_typed_dict({"description": "anything"}) is Any


def test_additional_properties_maps() -> None:
    bounded = _adapter({"type": "object", "additionalProperties": {"type": "integer", "maximum": 3}})
    assert bounded.validate_python({"x": 2}) == {"x": 2}
    with pytest.raises(ValidationError):
        bounded.validate_python({"x": 4})
    assert _adapter({"type": "object", "additionalProperties": True}).validate_python({"x": [1]}) == {"x": [1]}


def test_prefix_items_and_untyped_arrays() -> None:
    prefixed = _adapter({"type": "array", "prefixItems": [{"type": "integer", "minimum": 0}, {"type": "string"}]})
    assert prefixed.validate_python([1, "a"]) == [1, "a"]
    with pytest.raises(ValidationError):
        prefixed.validate_python([-1])
    assert _adapter({"type": "array"}).validate_python([1, "a"]) == [1, "a"]


def test_refs_resolve_once_and_refuse_unresolved_or_recursive() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/Leaf"}, "b": {"$ref": "#/$defs/Leaf"}},
        "$defs": {"Leaf": {"type": "integer"}},
    }
    assert _adapter(schema).validate_python({"a": 1, "b": 2}) == {"a": 1, "b": 2}
    with pytest.raises(ValueError, match="no matching \\$defs entry"):
        json_schema_to_typed_dict({"$ref": "#/$defs/Missing"})
    recursive = {"$ref": "#/$defs/Node", "$defs": {"Node": {"anyOf": [{"$ref": "#/$defs/Node"}]}}}
    with pytest.raises(ValueError, match="is recursive"):
        json_schema_to_typed_dict(recursive)


def test_nesting_past_max_depth_is_refused() -> None:
    schema = {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}
    with pytest.raises(ValueError, match="exceeds max_depth=1"):
        json_schema_to_typed_dict(schema, max_depth=1)
