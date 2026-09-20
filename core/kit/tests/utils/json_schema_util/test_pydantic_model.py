"""pydantic_model: JSON-Schema -> pydantic model conversion.

Exercises every structural branch (objects, arrays, refs, $defs, anyOf/allOf/
oneOf, enum/const, nullable unions, additionalProperties maps, name sanitization,
'not'/typeless fallbacks, empty-union rejection, recursion depth) by building the
model and checking it validates as the schema describes.
"""

import pytest
from pydantic import BaseModel, ValidationError

from tai42_kit.utils.data.json_schema_util import json_schema_to_pydantic_model as build


def test_scalar_types_map():
    assert build({"type": "string"}) is str
    assert build({"type": "integer"}) is int
    assert build({"type": "number"}) is float
    assert build({"type": "boolean"}) is bool


def test_typeless_and_not_fall_back_to_any():
    from typing import Any

    assert build({}) is Any
    assert build({"not": {"type": "string"}}) is Any


def test_object_required_and_optional_fields():
    from typing import Any

    model = build(
        {
            "type": "object",
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name"],
        },
        "Person",
    )
    assert issubclass(model, BaseModel)
    # The model is built dynamically from the schema, so its concrete field
    # attributes are not statically known.
    inst: Any = model(name="ada")
    assert inst.name == "ada"
    assert inst.age is None
    with pytest.raises(ValidationError):
        model()  # missing required 'name'


def test_object_default_value_used_for_optional():
    model = build(
        {"type": "object", "properties": {"n": {"type": "integer", "default": 7}}},
        "WithDefault",
    )
    assert model().n == 7


def test_array_of_objects():
    model = build(
        {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "integer"}}}}
            },
            "required": ["items"],
        },
        "HasList",
    )
    inst = model(items=[{"x": 1}, {"x": 2}])
    assert [i.x for i in inst.items] == [1, 2]


def test_array_without_items_is_list_any():
    from typing import Any, get_args, get_origin

    out = build({"type": "array"})
    assert get_origin(out) is list
    assert get_args(out) == (Any,)


def test_additional_properties_map():
    from typing import get_args, get_origin

    out = build({"type": "object", "additionalProperties": {"type": "integer"}}, "Map")
    assert get_origin(out) is dict
    assert get_args(out) == (str, int)


def test_additional_properties_true_sets_extra_allow_config():
    model = build(
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
        "Loose",
    )
    # The additionalProperties=True branch flips the model's extra policy to allow.
    assert model.model_config.get("extra") == "allow"
    assert model(a="x").a == "x"


def test_additional_properties_true_retains_extra_keys():
    model = build(
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
        "LooseRetain",
    )
    # extra='allow' is passed into create_model, so pydantic actually keeps the
    # unknown keys instead of discarding them.
    inst = model.model_validate({"a": "x", "b": 1, "c": [1, 2]})
    assert inst.a == "x"
    assert inst.__pydantic_extra__ == {"b": 1, "c": [1, 2]}
    assert inst.model_dump() == {"a": "x", "b": 1, "c": [1, 2]}


def test_additional_properties_false_forbids_extra_keys():
    model = build(
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
        "Forbid",
    )
    # Explicit additionalProperties:false maps to pydantic extra='forbid'.
    assert model.model_config.get("extra") == "forbid"
    assert model.model_validate({"a": "x"}).a == "x"
    with pytest.raises(ValidationError):
        model.model_validate({"a": "x", "b": 1})


def test_additional_properties_absent_ignores_extra_keys():
    model = build(
        {"type": "object", "properties": {"a": {"type": "string"}}},
        "Loose",
    )
    # additionalProperties absent keeps the pragmatic 'ignore' default, so
    # unknown keys are dropped rather than retained or rejected.
    assert model.model_config.get("extra") == "ignore"
    inst = model.model_validate({"a": "x", "b": 1})
    assert inst.__pydantic_extra__ is None
    assert inst.model_dump() == {"a": "x"}


def test_enum_and_const_become_literals():
    from typing import Literal, get_args

    assert set(get_args(build({"enum": ["a", "b"]}))) == {"a", "b"}
    assert build({"const": "fixed"}) == Literal["fixed"]


def test_nullable_single_type():
    from typing import get_args

    out = build({"type": ["string", "null"]})
    assert type(None) in get_args(out)
    assert str in get_args(out)


def test_nullable_multi_type_union():
    from typing import get_args

    out = build({"type": ["string", "integer", "null"]})
    args = get_args(out)
    assert str in args
    assert int in args
    assert type(None) in args


def test_type_list_without_null_is_union():
    from typing import get_args

    out = build({"type": ["string", "integer"]})
    assert set(get_args(out)) == {str, int}


def test_nullable_object_is_optional_model():
    from typing import get_args

    out = build({"type": ["object", "null"], "properties": {"x": {"type": "integer"}}}, "MaybeObj")
    non_null = [a for a in get_args(out) if a is not type(None)]
    assert issubclass(non_null[0], BaseModel)


def test_nullable_array():
    from typing import get_args

    out = build({"type": ["array", "null"], "items": {"type": "string"}}, "MaybeArr")
    assert type(None) in get_args(out)


def test_anyof_union():
    from typing import get_args

    out = build({"anyOf": [{"type": "string"}, {"type": "integer"}]}, "AnyVal")
    assert set(get_args(out)) == {str, int}


def test_oneof_union():
    from typing import get_args

    out = build({"oneOf": [{"type": "string"}, {"type": "boolean"}]}, "OneVal")
    assert set(get_args(out)) == {str, bool}


def test_allof_merges_base_models():
    schema = {
        "allOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]},
        ]
    }
    model = build(schema, "Merged")
    inst = model(a="x", b=3)
    assert inst.a == "x"
    assert inst.b == 3


def test_allof_without_models_falls_back_to_any():
    from typing import Any

    # allOf members that aren't object models leave nothing to merge -> Any.
    assert build({"allOf": [{"type": "string"}]}, "NoBase") is Any


def test_ref_and_defs_resolve():
    schema = {
        "type": "object",
        "$defs": {"Inner": {"type": "object", "properties": {"v": {"type": "integer"}}, "required": ["v"]}},
        "properties": {"inner": {"$ref": "#/$defs/Inner"}},
        "required": ["inner"],
    }
    model = build(schema, "Outer")
    inst = model(inner={"v": 5})
    assert inst.inner.v == 5


def test_unresolved_ref_uses_forward_ref():
    from typing import ForwardRef

    out = build({"$ref": "#/$defs/Missing"})
    assert isinstance(out, ForwardRef)


@pytest.mark.filterwarnings("ignore:Field name .schema.:UserWarning")
def test_property_name_sanitized_with_alias():
    model = build(
        {"type": "object", "properties": {"$schema": {"type": "string"}}, "required": ["$schema"]},
        "Sani",
    )
    # The leading '$' is stripped for the python field name; the alias keeps the
    # original wire name so validation/round-trip uses "$schema".
    assert "schema" in model.model_fields
    inst = model.model_validate({"$schema": "v"})
    assert inst.model_dump(by_alias=True)["$schema"] == "v"


def test_property_name_reserved_word_gets_underscore_alias():
    model = build(
        {"type": "object", "properties": {"class": {"type": "string"}}, "required": ["class"]},
        "Reserved",
    )
    # 'class' is a python keyword -> field renamed to 'class_', alias preserves wire name.
    assert "class_" in model.model_fields
    inst = model.model_validate({"class": "c"})
    assert inst.model_dump(by_alias=True)["class"] == "c"


def test_digit_first_property_name_builds_and_round_trips():
    # A digit-first key sanitizes to a leading-underscore name, which pydantic
    # rejects; it must instead get a letter prefix while the alias keeps "1st".
    model = build(
        {"type": "object", "properties": {"1st": {"type": "string"}}, "required": ["1st"]},
        "DigitFirst",
    )
    assert "field_1st" in model.model_fields
    inst = model.model_validate({"1st": "v"})
    assert inst.field_1st == "v"
    assert inst.model_dump(by_alias=True)["1st"] == "v"


def test_sanitized_property_name_collision_raises():
    # "a-b" and "a_b" both sanitize to the field name "a_b"; keying fields by the
    # sanitized name would drop one property, so the collision must raise, naming
    # both original keys.
    schema = {
        "type": "object",
        "properties": {"a-b": {"type": "string"}, "a_b": {"type": "integer"}},
        "required": ["a-b", "a_b"],
    }
    with pytest.raises(ValueError, match=r"'a-b'.*'a_b'.*'a_b'"):
        build(schema, "Collide")


def test_leading_underscore_property_name_builds_and_round_trips():
    # A leading-underscore key is equally illegal as a pydantic field name and
    # gets the same letter prefix, with the alias preserving the wire key.
    model = build(
        {"type": "object", "properties": {"_meta": {"type": "string"}}, "required": ["_meta"]},
        "LeadingUnderscore",
    )
    assert "field__meta" in model.model_fields
    inst = model.model_validate({"_meta": "v"})
    assert inst.field__meta == "v"
    assert inst.model_dump(by_alias=True)["_meta"] == "v"


# --- empty-union rejection + recursion depth bound --------------------------


def test_empty_anyof_raises_named():
    # An empty anyOf would otherwise reach ``Union[()]`` -> opaque TypeError;
    # instead it raises a named ValueError citing the keyword.
    with pytest.raises(ValueError, match="anyOf"):
        build({"anyOf": []}, "EmptyAny")


def test_empty_oneof_raises_named():
    with pytest.raises(ValueError, match="oneOf"):
        build({"oneOf": []}, "EmptyOne")


def _nest_object(depth: int) -> dict:
    schema: dict = {"type": "object", "properties": {"leaf": {"type": "integer"}}}
    for _ in range(depth):
        schema = {"type": "object", "properties": {"child": schema}}
    return schema


def test_depth_bound_raises_named():
    deep = _nest_object(20)
    with pytest.raises(ValueError, match="max_depth"):
        build(deep, "Deep", max_depth=3)


def test_within_depth_bound_converts_fine():
    # The same schema converts cleanly when the bound is generous.
    model = build(_nest_object(20), "OkDeep", max_depth=100)
    assert issubclass(model, BaseModel)


def test_oneof_ceiling_is_plain_union_not_xor():
    # The documented structural ceiling: oneOf collapses to a plain Union, so the
    # converter-built model does NOT enforce exactly-one — a value matching BOTH
    # branches validates. The faithful validator is what enforces XOR.
    from typing import get_args

    out = build({"oneOf": [{"type": "string"}, {"type": "integer"}]}, "OneCeil")
    assert set(get_args(out)) == {str, int}
