"""``to_portable_schema`` / ``check_native_representable`` — the native-portable rewrite.

A synthetic nested schema (objects, arrays of objects, many nullable ``type`` arrays,
enums, a ``$defs`` reference; no business words) is rewritten so no list-valued
``type`` survives, each nullable array becomes an ``anyOf`` of single-type members
carrying their own keywords, and ``$defs`` is recursed — all WITHOUT changing the set
of instances the schema accepts (validation equivalence over a corpus). A type-less
node raises; a map object is reported non-representable; the Anthropic transform
accepts the portable form (oracle, skipped when the SDK is not installed).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tai42_kit.utils.data.json_schema_util import (
    JsonSchemaValidationError,
    NonPortableSchemaError,
    check_native_representable,
    to_portable_schema,
    validate_against_json_schema,
)


def _turn_intake_schema() -> dict[str, Any]:
    """A ``TurnIntake``-shaped schema: 35 nullable type arrays, nested objects/arrays, enums, a $ref."""
    nullable_ints = {f"field_{index:02d}": {"type": ["integer", "null"], "minimum": 0} for index in range(33)}
    return {
        "title": "TurnIntake",
        "type": "object",
        "properties": {
            **nullable_ints,
            "label": {"type": ["string", "null"], "enum": ["alpha", "beta", None]},
            "score": {"type": ["number", "null"]},
            "status": {"enum": ["open", "closed"]},
            "detail": {
                "type": "object",
                "title": "Detail",
                "properties": {"note": {"type": ["string", "null"], "maxLength": 20}},
            },
            "items": {"type": "array", "items": {"$ref": "#/$defs/Entry"}},
        },
        "required": ["status"],
        "$defs": {
            "Entry": {
                "type": "object",
                "title": "Entry",
                "properties": {"n": {"type": ["integer", "null"]}},
            }
        },
    }


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every dict node in a schema tree."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_no_list_valued_type_survives() -> None:
    portable = to_portable_schema(_turn_intake_schema())
    offenders = [node for node in _walk(portable) if isinstance(node.get("type"), list)]
    assert offenders == [], offenders


def test_each_nullable_array_becomes_anyof_with_per_member_keywords() -> None:
    portable = to_portable_schema(_turn_intake_schema())
    field = portable["properties"]["field_00"]
    assert "type" not in field
    assert "anyOf" in field
    members = field["anyOf"]
    assert {member["type"] for member in members} == {"integer", "null"}
    integer_member = next(member for member in members if member["type"] == "integer")
    # The integer member keeps its own numeric keyword; the null member is the bare node.
    assert integer_member["minimum"] == 0
    assert {"type": "null"} in members


def test_defs_are_recursed() -> None:
    portable = to_portable_schema(_turn_intake_schema())
    entry = portable["$defs"]["Entry"]["properties"]["n"]
    assert "anyOf" in entry
    assert not isinstance(entry.get("type"), list)


def test_type_less_enum_and_const_infer_their_type() -> None:
    portable = to_portable_schema(_turn_intake_schema())
    status = portable["properties"]["status"]
    assert status["type"] == "string"
    assert status["enum"] == ["open", "closed"]

    const_portable = to_portable_schema({"title": "C", "const": 7})
    assert const_portable["type"] == "integer"
    assert const_portable["enum"] == [7]


_CORPUS: list[dict[str, Any]] = [
    {"status": "open"},
    {"status": "closed", "field_00": 5, "label": "alpha", "score": 1.5},
    {"status": "open", "label": None, "score": None},
    {"status": "open", "detail": {"note": "short"}},
    {"status": "open", "items": [{"n": 3}, {"n": None}]},
    # Non-conforming instances:
    {"status": "invalid"},  # enum violation
    {},  # missing required 'status'
    {"status": "open", "field_00": -1},  # minimum violation
    {"status": "open", "field_00": "x"},  # integer/null type violation
    {"status": "open", "label": "gamma"},  # enum (string member) violation
    {"status": "open", "detail": {"note": "this note is far too long to pass"}},  # maxLength violation
]


def _rejects(schema: dict[str, Any], instance: Any) -> bool:
    try:
        validate_against_json_schema(instance, schema)
    except JsonSchemaValidationError:
        return True
    return False


@pytest.mark.parametrize("instance", _CORPUS)
def test_validation_equivalence(instance: Any) -> None:
    original = _turn_intake_schema()
    portable = to_portable_schema(original)
    assert _rejects(original, instance) == _rejects(portable, instance), instance


def test_type_less_node_raises_naming_the_path() -> None:
    schema = {"title": "Bad", "type": "object", "properties": {"mystery": {"description": "no type at all"}}}
    with pytest.raises(NonPortableSchemaError) as excinfo:
        to_portable_schema(schema)
    assert "mystery" in excinfo.value.path


def test_map_additional_properties_is_not_representable() -> None:
    map_schema = to_portable_schema(
        {"title": "M", "type": "object", "additionalProperties": {"type": ["integer", "null"]}}
    )
    assert check_native_representable(map_schema) is False
    # A plain closed object (no schema-valued additionalProperties) is representable.
    assert check_native_representable(to_portable_schema(_turn_intake_schema())) is True


def test_anthropic_transform_accepts_the_portable_form() -> None:
    transform = pytest.importorskip("anthropic.lib._parse._transform")
    portable = to_portable_schema(_turn_intake_schema())
    # The Anthropic native converter accepts the portable form (no assert_never on a list type).
    transform.transform_schema(portable)
