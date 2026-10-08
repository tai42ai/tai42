"""typed_dict: a validating TypedDict tree that round-trips a value to plain
nested dicts while enforcing every representable constraint at every value site,
including injected int64 bounds; titled object naming and recursive/dangling ref
handling.
"""

from typing import Any

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


def test_typed_dict_type_list_becomes_a_union_of_each_member():
    schema = {
        "type": ["object", "array", "string", "null"],
        "title": "Mixed",
        "properties": {"k": {"type": "integer"}},
        "items": {"type": "integer"},
    }
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Mixed"))
    assert adapter.validate_python({"k": 1}) == {"k": 1}
    assert adapter.validate_python([1, 2]) == [1, 2]
    assert adapter.validate_python("s") == "s"
    assert adapter.validate_python(None) is None
    with pytest.raises(ValidationError):
        adapter.validate_python(3.5)


def test_typed_dict_const_enum_not_and_untyped_nodes():
    schema = {
        "type": "object",
        "properties": {
            "fixed": {"const": "on"},
            "pick": {"enum": ["a", "b"]},
            "neg": {"not": {"type": "string"}},
            "free": {},
        },
        "required": ["fixed", "pick", "neg", "free"],
    }
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Shapes"))
    value = {"fixed": "on", "pick": "b", "neg": 1, "free": [1]}
    assert adapter.validate_python(value) == value
    with pytest.raises(ValidationError):
        adapter.validate_python({**value, "fixed": "off"})
    with pytest.raises(ValidationError):
        adapter.validate_python({**value, "pick": "c"})


def test_typed_dict_allof_merges_object_members_and_falls_back_for_others():
    merged = {
        "title": "Merged",
        "allOf": [
            {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
            {"properties": {"b": {"type": "string"}}},
        ],
    }
    adapter = TypeAdapter(json_schema_to_typed_dict(merged, name="Merged"))
    assert adapter.validate_python({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}
    with pytest.raises(ValidationError):
        adapter.validate_python({"b": "x"})
    scalar_member = {"allOf": [{"type": "object", "properties": {"a": {"type": "integer"}}}, {"minimum": 1}]}
    assert json_schema_to_typed_dict(scalar_member, name="Loose") is Any


def test_typed_dict_reuses_a_ref_resolved_once():
    schema = {
        "$defs": {"Leaf": {"type": "object", "properties": {"v": {"type": "integer"}}, "required": ["v"]}},
        "type": "object",
        "properties": {"left": {"$ref": "#/$defs/Leaf"}, "right": {"$ref": "#/$defs/Leaf"}},
        "required": ["left", "right"],
    }
    annotation = json_schema_to_typed_dict(schema, name="Pair")
    hints = annotation.__annotations__
    assert hints["left"] is hints["right"]
    assert TypeAdapter(annotation).validate_python({"left": {"v": 1}, "right": {"v": 2}}) == {
        "left": {"v": 1},
        "right": {"v": 2},
    }


def test_typed_dict_open_map_and_prefix_items():
    schema = {
        "type": "object",
        "properties": {
            "bag": {"type": "object", "additionalProperties": True},
            "pair": {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "string", "maxLength": 2}]},
            "anything": {"type": "array"},
        },
        "required": ["bag", "pair", "anything"],
    }
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Open"))
    value = {"bag": {"x": [1]}, "pair": [1, "ab"], "anything": [None, "z"]}
    assert adapter.validate_python(value) == value
    with pytest.raises(ValidationError):
        adapter.validate_python({**value, "pair": [1, "abc"]})


def test_typed_dict_refuses_an_empty_union_and_excess_depth():
    with pytest.raises(ValueError, match="at least one subschema"):
        json_schema_to_typed_dict({"anyOf": []}, name="Empty")
    deep: dict = {"type": "integer"}
    for _ in range(3):
        deep = {"type": "object", "properties": {"n": deep}}
    with pytest.raises(ValueError, match="max_depth=2"):
        json_schema_to_typed_dict(deep, name="Deep", max_depth=2)


def test_typed_dict_type_list_builds_a_union_of_each_member():
    schema = {
        "type": ["object", "array", "integer", "null"],
        "title": "Mixed",
        "properties": {"k": {"type": "integer"}},
        "required": ["k"],
        "items": {"type": "string"},
    }
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Mixed"))
    assert adapter.validate_python({"k": 1}) == {"k": 1}
    assert adapter.validate_python(["a", "b"]) == ["a", "b"]
    assert adapter.validate_python(5) == 5
    assert adapter.validate_python(None) is None
    with pytest.raises(ValidationError):
        adapter.validate_python("text")


def test_typed_dict_const_and_enum_admit_only_their_values():
    schema = {
        "title": "Choice",
        "type": "object",
        "properties": {"kind": {"const": "fixed"}, "level": {"enum": ["low", "high"]}},
        "required": ["kind", "level"],
    }
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Choice"))
    assert adapter.validate_python({"kind": "fixed", "level": "high"}) == {"kind": "fixed", "level": "high"}
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "other", "level": "high"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "fixed", "level": "mid"})


def test_typed_dict_not_and_typeless_nodes_accept_any_value():
    schema = {
        "title": "Loose",
        "type": "object",
        "properties": {"neg": {"not": {"type": "string"}}, "free": {}},
        "required": ["neg", "free"],
    }
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Loose"))
    value = {"neg": [1, {"x": None}], "free": "anything"}
    assert adapter.validate_python(value) == value


def test_typed_dict_allof_of_objects_merges_properties_and_required():
    schema = {
        "title": "Merged",
        "allOf": [
            {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
            {"properties": {"b": {"type": "string"}}, "required": ["b"]},
        ],
    }
    annotation = json_schema_to_typed_dict(schema, name="Merged")
    assert annotation.__name__ == "Merged"
    adapter = TypeAdapter(annotation)
    assert adapter.validate_python({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}
    with pytest.raises(ValidationError):
        adapter.validate_python({"a": 1})


def test_typed_dict_allof_with_a_non_object_member_accepts_any_value():
    from typing import Any

    schema = {"allOf": [{"type": "object", "properties": {"a": {"type": "integer"}}}, {"$ref": "#/$defs/X"}]}
    assert json_schema_to_typed_dict(schema, name="Mixed") is Any


def test_typed_dict_shared_ref_is_built_once_and_enforced_at_each_use():
    schema = {
        "$defs": {
            "Point": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
            }
        },
        "title": "Segment",
        "type": "object",
        "properties": {"start": {"$ref": "#/$defs/Point"}, "end": {"$ref": "#/$defs/Point"}},
        "required": ["start", "end"],
    }
    annotation = json_schema_to_typed_dict(schema, name="Segment")
    hints = annotation.__annotations__
    assert hints["start"] is hints["end"]
    adapter = TypeAdapter(annotation)
    value = {"start": {"x": 0, "y": 0}, "end": {"x": 3, "y": 4}}
    assert adapter.validate_python(value) == value
    with pytest.raises(ValidationError):
        adapter.validate_python({"start": {"x": 0, "y": 0}, "end": {"x": 3}})


def test_typed_dict_max_depth_bounds_nesting_loudly():
    schema = {"type": "array", "items": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}}
    assert TypeAdapter(json_schema_to_typed_dict(schema, max_depth=3)).validate_python([[[1]]]) == [[[1]]]
    with pytest.raises(ValueError, match="max_depth=2"):
        json_schema_to_typed_dict(schema, max_depth=2)


def test_typed_dict_empty_union_raises_loudly():
    with pytest.raises(ValueError, match="at least one subschema"):
        json_schema_to_typed_dict({"anyOf": []}, name="Empty")


def test_typed_dict_open_map_without_value_schema_accepts_any_values():
    schema = {"type": "object", "additionalProperties": True}
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Bag"))
    value = {"a": 1, "b": [None], "c": {"d": "e"}}
    assert adapter.validate_python(value) == value


def test_typed_dict_prefix_items_admit_each_position_type_and_enforce_bounds():
    schema = inject_int64_bounds({"type": "array", "prefixItems": [{"type": "integer"}, {"type": "string"}]})
    adapter = TypeAdapter(json_schema_to_typed_dict(schema, name="Pair"))
    assert adapter.validate_python([1, "a"]) == [1, "a"]
    with pytest.raises(ValidationError):
        adapter.validate_python([1.5])
    with pytest.raises(ValidationError):
        adapter.validate_python([INT64_MAX + 1])


def test_typed_dict_array_without_item_schema_accepts_any_items():
    adapter = TypeAdapter(json_schema_to_typed_dict({"type": "array"}, name="Items"))
    assert adapter.validate_python([1, "a", None]) == [1, "a", None]
