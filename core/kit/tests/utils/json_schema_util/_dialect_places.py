"""Every place each JSON Schema dialect evaluates a subschema, and the rules check run on a schema.

Shared by the rules-check tests: :data:`CASES` pairs each validator class with every place its
instance check evaluates a subschema, and :func:`at` builds a schema holding a subschema there.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import jsonschema

from tai42_kit.utils.data.json_schema_util.schema_rules import check_schema_rules, schema_validator_class


def _check(schema: dict[str, Any]) -> None:
    # The rules check with the class the schema's own ``$schema`` names, Draft 2020-12 by default.
    check_schema_rules(
        schema, schema_validator_class(schema, jsonschema.Draft202012Validator, subject="schema"), subject="schema"
    )


_D3 = jsonschema.Draft3Validator
_D4 = jsonschema.Draft4Validator
_D6 = jsonschema.Draft6Validator
_D7 = jsonschema.Draft7Validator
_D2019 = jsonschema.Draft201909Validator
_D2020 = jsonschema.Draft202012Validator
_CLASSES = [_D3, _D4, _D6, _D7, _D2019, _D2020]

_Builder = Callable[[dict[str, Any]], tuple[dict[str, Any], Any]]
_OBJ = {"a": 1}

# Every place a subschema is evaluated, per keyword: the schema fragment holding ``inner`` there,
# and an instance that makes the instance check evaluate it.
_POSITIONS: dict[str, _Builder] = {
    "properties": lambda inner: ({"properties": {"k": inner}}, {"k": _OBJ}),
    "patternProperties": lambda inner: ({"patternProperties": {"k": inner}}, {"k": _OBJ}),
    "additionalProperties": lambda inner: ({"additionalProperties": inner}, {"k": _OBJ}),
    "additionalItems": lambda inner: ({"items": [{}], "additionalItems": inner}, [0, _OBJ]),
    "items": lambda inner: ({"items": inner}, [_OBJ]),
    "items/0": lambda inner: ({"items": [inner]}, [_OBJ]),
    "dependencies": lambda inner: ({"dependencies": {"k": inner}}, {"k": 1, "a": 1}),
    "allOf": lambda inner: ({"allOf": [inner]}, _OBJ),
    "anyOf": lambda inner: ({"anyOf": [inner]}, _OBJ),
    "oneOf": lambda inner: ({"oneOf": [inner]}, _OBJ),
    "not": lambda inner: ({"not": inner}, _OBJ),
    "contains": lambda inner: ({"contains": inner}, [_OBJ]),
    "propertyNames": lambda inner: ({"propertyNames": inner}, _OBJ),
    "if": lambda inner: ({"if": inner}, _OBJ),
    "if/then": lambda inner: ({"if": {}, "then": inner}, _OBJ),
    "if/else": lambda inner: ({"if": False, "else": inner}, _OBJ),
    "dependentSchemas": lambda inner: ({"dependentSchemas": {"k": inner}}, {"k": 1, "a": 1}),
    "unevaluatedItems": lambda inner: ({"unevaluatedItems": inner}, [_OBJ]),
    "unevaluatedProperties": lambda inner: ({"unevaluatedProperties": inner}, {"k": _OBJ}),
    "prefixItems": lambda inner: ({"prefixItems": [inner]}, [_OBJ]),
    "extends": lambda inner: ({"extends": inner}, _OBJ),
    "extends/0": lambda inner: ({"extends": [inner]}, _OBJ),
    "type/0": lambda inner: ({"type": [inner]}, _OBJ),
    "disallow/0": lambda inner: ({"disallow": [inner]}, _OBJ),
}

_ITEMS_EITHER = ["items", "items/0"]
_D4_PLACES = [
    "properties",
    "patternProperties",
    "additionalProperties",
    "additionalItems",
    *_ITEMS_EITHER,
    "dependencies",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
]
_D6_PLACES = [*_D4_PLACES, "contains", "propertyNames"]
_D7_PLACES = [*_D6_PLACES, "if", "if/then", "if/else"]
_LATER_PLACES = [
    "properties",
    "patternProperties",
    "additionalProperties",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "contains",
    "propertyNames",
    "if",
    "if/then",
    "if/else",
    "dependentSchemas",
    "unevaluatedItems",
    "unevaluatedProperties",
]
_PLACES: dict[type, list[str]] = {
    _D3: [
        "properties",
        "patternProperties",
        "additionalProperties",
        "additionalItems",
        *_ITEMS_EITHER,
        "dependencies",
        "extends",
        "extends/0",
        "type/0",
        "disallow/0",
    ],
    _D4: _D4_PLACES,
    _D6: _D6_PLACES,
    _D7: _D7_PLACES,
    _D2019: [*_LATER_PLACES, "additionalItems", *_ITEMS_EITHER],
    _D2020: [*_LATER_PLACES, "items", "prefixItems"],
}
_CASES = [(cls, place) for cls in _CLASSES for place in _PLACES[cls]]
_CASE_IDS = [f"{cls.__name__}-{place}" for cls, place in _CASES]


def _at(cls: type, place: str, inner: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    fragment, instance = _POSITIONS[place](inner)
    return {"$schema": cls.META_SCHEMA["$schema"], **fragment}, instance
