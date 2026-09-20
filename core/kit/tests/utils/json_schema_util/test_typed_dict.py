"""typed_dict: a validating TypedDict tree that round-trips a value to plain
nested dicts while enforcing every representable constraint at every value site,
including injected int64 bounds; titled object naming and recursive/dangling ref
handling.
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from tai42_kit.utils.data.json_schema_util import (
    INT64_MAX,
    inject_int64_bounds,
    json_schema_to_typed_dict,
)


def test_typed_dict_round_trips_nested_value_to_plain_dicts():
    schema = {
        "title": "Answer",
        "type": "object",
        "properties": {
            "value": {"type": "integer"},
            "nested": {"type": "object", "properties": {"k": {"type": "integer"}}, "required": ["k"]},
            "arr": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["value"],
    }
    adapter = TypeAdapter(json_schema_to_typed_dict(inject_int64_bounds(schema), name="Answer"))
    out = adapter.validate_python({"value": 7, "nested": {"k": 1}, "arr": [1, 2]})
    assert out == {"value": 7, "nested": {"k": 1}, "arr": [1, 2]}
    assert type(out) is dict
    assert type(out["nested"]) is dict


def test_typed_dict_rejects_oversized_integer_at_every_depth():
    schema = inject_int64_bounds(
        {
            "title": "Answer",
            "type": "object",
            "properties": {
                "value": {"type": "integer"},
                "nested": {"type": "object", "properties": {"k": {"type": "integer"}}, "required": ["k"]},
                "arr": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["value"],
        }
    )
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Answer"))
    over = INT64_MAX + 1
    for bad in ({"value": over}, {"value": 1, "nested": {"k": over}}, {"value": 1, "arr": [over]}):
        with pytest.raises(ValidationError):
            adapter.validate_python(bad)


def test_typed_dict_rejects_oversized_integer_at_anyof_member_and_map_value():
    # An injected int64 bound on an integer that sits inside an anyOf member, or as
    # an additionalProperties map value, must be ENFORCED by the TypeAdapter parse
    # (the tools door reprompts off this schema-level bound) — not just on
    # object-property/array-item positions.
    schema = inject_int64_bounds(
        {
            "title": "Answer",
            "type": "object",
            "properties": {
                "choice": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
            },
            "required": ["choice", "counts"],
        }
    )
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Answer"))
    over = INT64_MAX + 1
    # A conforming value round-trips at both positions.
    assert adapter.validate_python({"choice": 3, "counts": {"a": 1}}) == {"choice": 3, "counts": {"a": 1}}
    # The oversized integer is rejected at the anyOf integer member ...
    with pytest.raises(ValidationError):
        adapter.validate_python({"choice": over, "counts": {"a": 1}})
    # ... and at the additionalProperties integer map value.
    with pytest.raises(ValidationError):
        adapter.validate_python({"choice": 3, "counts": {"a": over}})


def test_typed_dict_names_object_by_title():
    schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
    assert json_schema_to_typed_dict(schema, name="fallback").__name__ == "Answer"


def test_typed_dict_top_level_oneof_fans_out_to_titled_variants():
    schema = {
        "title": "Top",
        "oneOf": [
            {"title": "A", "type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
            {"title": "B", "type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]},
        ],
    }
    from typing import get_args

    variants = get_args(json_schema_to_typed_dict(schema, name="Top"))
    assert {v.__name__ for v in variants} == {"A", "B"}


def test_typed_dict_recursive_ref_raises_loudly():
    schema = {
        "$defs": {"Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}},
        "$ref": "#/$defs/Node",
    }
    with pytest.raises(ValueError, match="recursive"):
        json_schema_to_typed_dict(schema, name="Node")


def test_typed_dict_dangling_ref_raises_loudly():
    with pytest.raises(ValueError, match="no matching"):
        json_schema_to_typed_dict({"$ref": "#/$defs/Missing"}, name="X")
