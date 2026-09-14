"""constraints: value constraints (numeric bounds, string length/pattern, array
length) carried onto generated pydantic model fields and re-emitted into the
model's JSON schema.
"""

import pytest
from pydantic import ValidationError

from tai42_kit.utils.data.json_schema_util import json_schema_to_pydantic_model as build


def test_numeric_constraints_enforced_and_reemitted():
    model = build(
        {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 10, "multipleOf": 2}},
            "required": ["n"],
        },
        "Num",
    )
    model(n=4)  # within bounds and a multiple of 2
    for bad in (0, 12, 3):  # below min, above max, not a multiple
        with pytest.raises(ValidationError):
            model(n=bad)
    prop = model.model_json_schema()["properties"]["n"]
    assert prop["minimum"] == 1
    assert prop["maximum"] == 10
    assert prop["multipleOf"] == 2


def test_exclusive_numeric_bounds_enforced_and_reemitted():
    model = build(
        {
            "type": "object",
            "properties": {"n": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 10}},
            "required": ["n"],
        },
        "Excl",
    )
    model(n=5.0)
    for bad in (0, 10):  # boundary values are excluded
        with pytest.raises(ValidationError):
            model(n=bad)
    prop = model.model_json_schema()["properties"]["n"]
    assert prop["exclusiveMinimum"] == 0
    assert prop["exclusiveMaximum"] == 10


def test_string_constraints_enforced_and_reemitted():
    model = build(
        {
            "type": "object",
            "properties": {"s": {"type": "string", "minLength": 2, "maxLength": 5, "pattern": "^a", "format": "email"}},
            "required": ["s"],
        },
        "Str",
    )
    model(s="abc")
    for bad in ("b", "abcdef", "xyz"):  # too short, too long, pattern miss
        with pytest.raises(ValidationError):
            model(s=bad)
    prop = model.model_json_schema()["properties"]["s"]
    assert prop["minLength"] == 2
    assert prop["maxLength"] == 5
    assert prop["pattern"] == "^a"
    # 'format' has no validation semantics but survives via json_schema_extra.
    assert prop["format"] == "email"


def test_array_constraints_enforced_and_reemitted():
    model = build(
        {
            "type": "object",
            "properties": {"a": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 3}},
            "required": ["a"],
        },
        "Arr",
    )
    model(a=[1, 2])
    for bad in ([], [1, 2, 3, 4]):  # below minItems, above maxItems
        with pytest.raises(ValidationError):
            model(a=bad)
    prop = model.model_json_schema()["properties"]["a"]
    assert prop["minItems"] == 1
    assert prop["maxItems"] == 3


def test_nullable_type_list_carries_constraint_and_accepts_none():
    # A ``["string", "null"]`` property still carries the string constraint on its
    # string branch while accepting None.
    model = build(
        {"type": "object", "properties": {"s": {"type": ["string", "null"], "maxLength": 3}}},
        "MaybeStr",
    )
    assert model(s=None).s is None
    model(s="ab")
    with pytest.raises(ValidationError):
        model(s="abcd")  # maxLength enforced on the string branch


def test_optional_constrained_property_accepts_none_and_enforces_bound():
    model = build(
        {"type": "object", "properties": {"n": {"type": "integer", "minimum": 5}}},
        "OptNum",
    )
    assert model().n is None  # optional -> default None accepted
    model(n=5)
    with pytest.raises(ValidationError):
        model(n=1)  # below minimum


def test_nested_object_constraints_enforced_and_reemitted():
    # A constraint on a property of a NESTED object survives into the nested
    # model and is enforced there, and re-emits into the $defs entry.
    model = build(
        {
            "type": "object",
            "properties": {
                "addr": {
                    "type": "object",
                    "properties": {"zip": {"type": "string", "pattern": "^[0-9]{5}$"}},
                    "required": ["zip"],
                }
            },
            "required": ["addr"],
        },
        "Nested",
    )
    model(addr={"zip": "12345"})
    with pytest.raises(ValidationError):
        model(addr={"zip": "abc"})  # pattern miss on the nested field
    inner = model.model_json_schema()["$defs"]["Nested_addr"]["properties"]["zip"]
    assert inner["pattern"] == "^[0-9]{5}$"


def test_array_item_constraints_enforced_and_reemitted():
    # A constraint on the ARRAY ITEM schema (not the array itself) is carried onto
    # the element type and enforced per element, and re-emits under items.
    model = build(
        {
            "type": "object",
            "properties": {"tags": {"type": "array", "items": {"type": "string", "minLength": 3, "pattern": "^a"}}},
            "required": ["tags"],
        },
        "Tagged",
    )
    model(tags=["abc", "axyz"])
    for bad in (["ab"], ["bcd"]):  # too short, pattern miss
        with pytest.raises(ValidationError):
            model(tags=bad)
    item = model.model_json_schema()["properties"]["tags"]["items"]
    assert item["minLength"] == 3
    assert item["pattern"] == "^a"
