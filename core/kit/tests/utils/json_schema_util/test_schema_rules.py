"""The rules check walks every subschema an instance check evaluates, on every dialect.

The subschema table is proven against ``jsonschema`` itself: every keyword each validator class
evaluates is classified, and at every place the table says a subschema is evaluated the instance
check really evaluates it (a nested ``$schema`` there is read, and one that does not parse crashes
the instance check), so the rules check refuses a fault there.
"""

from __future__ import annotations

import math
import re
import time
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import Any

import jsonschema
import pytest
from referencing import Specification
from referencing.jsonschema import specification_with

from tai42_kit.utils.data.json_schema_util import evaluated_subschemas
from tai42_kit.utils.data.json_schema_util.plain_json import check_plain_json
from tai42_kit.utils.data.json_schema_util.schema_rules import (
    _APPLICATORS,
    _IGNORES_REF_SIBLINGS,
    _REFERENCES,
    _SPECIFICATIONS,
    check_schema_rules,
)

from ._dialect_places import _CASE_IDS, _CASES, _CLASSES, _D3, _D4, _D6, _D7, _D2019, _D2020, _PLACES, _at, _check

_D4_PLAIN = {
    "enum",
    "format",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "pattern",
    "required",
    "type",
    "uniqueItems",
}
_D6_PLAIN = _D4_PLAIN | {"const", "exclusiveMaximum", "exclusiveMinimum"}

# The keywords each class evaluates whose value never holds a subschema.
_PLAIN_KEYWORDS: dict[type, set[str]] = {
    _D3: {
        "divisibleBy",
        "enum",
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "uniqueItems",
    },
    _D4: _D4_PLAIN,
    _D6: _D6_PLAIN,
    _D7: _D6_PLAIN,
    _D2019: _D6_PLAIN | {"dependentRequired"},
    _D2020: _D6_PLAIN | {"dependentRequired"},
}


@pytest.mark.parametrize("cls", _CLASSES, ids=lambda cls: cls.__name__)
def test_every_keyword_a_validator_evaluates_is_classified(cls):
    applicators = set(_APPLICATORS[cls])
    references = set(_REFERENCES[cls])
    assert not applicators & _PLAIN_KEYWORDS[cls]
    assert not references & (applicators | _PLAIN_KEYWORDS[cls])
    assert applicators | references | _PLAIN_KEYWORDS[cls] == set(cls.VALIDATORS)


@pytest.mark.parametrize("cls", _CLASSES, ids=lambda cls: cls.__name__)
def test_the_ref_sibling_rule_is_the_validators_own(cls):
    # A ``$ref`` beside ``type`` either hides it (the class evaluates the reference alone) or not.
    schema = {"$schema": cls.META_SCHEMA["$schema"], "$ref": "#/x-any", "x-any": {}, "type": "string"}
    siblings_ignored = cls(schema).is_valid(1)
    assert siblings_ignored is (cls in _IGNORES_REF_SIBLINGS)


@pytest.mark.parametrize("cls", _CLASSES, ids=lambda cls: cls.__name__)
def test_the_places_proven_cover_every_applicator(cls):
    assert {place.split("/")[0] for place in _PLACES[cls]} == set(_APPLICATORS[cls])


@pytest.mark.parametrize(("cls", "place"), _CASES, ids=_CASE_IDS)
def test_the_instance_check_evaluates_the_subschema_at_each_place(cls, place):
    schema, instance = _at(cls, place, {"$schema": "http://["})
    with pytest.raises(ValueError, match="Invalid IPv6 URL"):
        list(cls(schema).iter_errors(instance))


@pytest.mark.parametrize(("cls", "place"), _CASES, ids=_CASE_IDS)
def test_the_rules_check_refuses_a_nested_schema_that_does_not_parse_at_each_place(cls, place):
    schema, _ = _at(cls, place, {"$schema": "http://["})
    with pytest.raises(ValueError, match=re.escape("'http://[' does not parse as a URI: Invalid IPv6 URL")):
        _check(schema)


@pytest.mark.parametrize(("cls", "place"), _CASES, ids=_CASE_IDS)
def test_the_rules_check_refuses_a_pattern_properties_key_that_does_not_compile_at_each_place(cls, place):
    # Where the dialect's meta-schema checks the key and the place sits inside an ``anyOf`` of
    # the meta-schema (draft-06 and draft-07 ``items``), its first fault names that ``anyOf``.
    schema, _ = _at(cls, place, {"type": "object", "patternProperties": {"(": {}}})
    faults = f"{re.escape("'(' is not a 'regex'")}|is not valid under any of the given schemas"
    with pytest.raises(ValueError, match=faults):
        _check(schema)


_D3_URI = _D3.META_SCHEMA["$schema"]


def test_the_rules_check_admits_a_draft_03_extends_holding_one_schema():
    _check({"$schema": _D3_URI, "extends": {"type": "string"}})


@pytest.mark.parametrize("dialect", [5, None, ["x"], {"a": 1}])
def test_the_rules_check_refuses_a_nested_schema_that_is_not_a_string(dialect):
    schema = {"type": "object", "properties": {"x": {"allOf": [{"$schema": dialect}]}}}
    with pytest.raises(
        ValueError,
        match=re.escape(f"schema property 'x' breaks the JSON Schema rules: {dialect!r} is not of type 'string'"),
    ):
        _check(schema)


def test_a_nested_dialect_is_held_to_its_own_meta_schema():
    # The instance check evaluates a subschema naming draft-03 with the draft-03 keywords, so
    # ``extends: 5`` crashes it; the draft-03 meta-schema refuses it up front.
    schema = {"type": "object", "properties": {"x": {"$schema": _D3_URI, "extends": 5}}}
    with pytest.raises(TypeError):
        list(jsonschema.Draft202012Validator(schema).iter_errors({"x": 1}))
    with pytest.raises(ValueError, match=re.escape("schema property 'x' breaks the JSON Schema rules: ")):
        _check(schema)


@pytest.mark.parametrize(
    "container",
    [
        lambda value: (value,),
        lambda value: {value},
        lambda value: frozenset([value]),
        lambda value: [(1, value)],
    ],
    ids=["tuple", "set", "frozenset", "tuple-in-list"],
)
def test_plain_json_walks_every_container_a_caller_can_pass(container):
    schema = {"type": "object", "properties": {"x": {"default": container(math.inf)}}}
    with pytest.raises(ValueError, match="that is not a finite number"):
        check_plain_json(schema, subject="schema")


def test_plain_json_refuses_an_iterator_reading_it_would_use_up():
    schema = {"type": "object", "properties": {"x": {"default": (value for value in [1])}}}
    with pytest.raises(ValueError, match=re.escape("schema property 'x' has a value at default that is an iterator")):
        check_plain_json(schema, subject="schema")


def test_plain_json_refuses_a_cycle_through_a_tuple():
    inner: list[Any] = []
    held = (inner,)
    inner.append(held)
    schema = {"type": "object", "properties": {"x": {"default": held}}}
    with pytest.raises(ValueError, match="loops back to a value it sits inside"):
        check_plain_json(schema, subject="schema")


def _doubled(depth: int) -> list[Any]:
    # A value holding the one value below it twice, ``depth`` times: 2**depth paths through
    # ``depth`` containers.
    value: list[Any] = []
    for _ in range(depth):
        value = [value, value]
    return value


def test_plain_json_walks_a_shared_value_once():
    started = time.perf_counter()
    check_plain_json({"default": _doubled(60)}, subject="schema")
    assert time.perf_counter() - started < 1.0


def test_the_rules_check_walks_a_shared_value_once():
    started = time.perf_counter()
    _check({"default": _doubled(60), "x-pair": _doubled(60)})
    assert time.perf_counter() - started < 1.0


def test_plain_json_walks_a_deep_nesting_without_recursing():
    value: list[Any] = []
    for _ in range(200_000):
        value = [value]
    check_plain_json({"default": value}, subject="schema")


def test_the_rules_check_refuses_a_nesting_too_deep_to_check():
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(100_000):
        schema = {"items": schema}
    with pytest.raises(ValueError, match="schema nests too deeply to check"):
        check_schema_rules(schema, jsonschema.Draft202012Validator, subject="schema")


@pytest.mark.parametrize("cls", _CLASSES, ids=lambda cls: cls.__name__)
def test_each_class_builds_its_resources_with_the_specification_jsonschema_picks(cls):
    picked = specification_with(cls.ID_OF(cls.META_SCHEMA) or "urn:unknown-dialect", default=Specification.OPAQUE)
    assert _SPECIFICATIONS[cls] is picked


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (Decimal("NaN"), "NaN"),
        (Decimal("-Infinity"), "-Infinity"),
        (Decimal("sNaN"), "sNaN"),
        (complex(math.inf, 0), "(inf+0j)"),
        (complex(0, math.nan), "nanj"),
    ],
    ids=["decimal-nan", "decimal-infinity", "decimal-snan", "complex-inf", "complex-nan"],
)
def test_plain_json_refuses_every_number_type_that_is_not_finite(value, shown):
    with pytest.raises(ValueError, match=re.escape(f"at maximum that is not a finite number ({shown})")):
        check_plain_json({"type": "number", "maximum": value}, subject="schema")


def test_plain_json_admits_finite_numbers_of_every_type():
    check_plain_json({"enum": [Decimal("1.5"), Fraction(1, 3), 10**400, complex(1, 2), True, 2.5]}, subject="schema")


def test_plain_json_walks_a_mapping_that_is_not_a_dict():
    with pytest.raises(ValueError, match=re.escape("at const/x that is not a finite number (inf)")):
        check_plain_json({"const": MappingProxyType({"x": math.inf})}, subject="schema")


def _evaluated_schema() -> dict[str, Any]:
    # A subschema reached through each reference form, one under an unknown keyword reached by
    # nothing, one inside data, and a reference into a meta-schema.
    return {
        "$id": "https://example.com/s",
        "type": "object",
        "properties": {
            "pointer": {"$ref": "#/x-pointer"},
            "absolute": {"$ref": "https://example.com/s#/x-absolute"},
            "anchor": {"$ref": "#t"},
            "meta": {"$ref": "https://json-schema.org/draft/2020-12/schema"},
            "never": True,
        },
        "x-pointer": {"type": "string"},
        "x-absolute": {"type": "integer"},
        "$defs": {"anchored": {"$anchor": "t", "type": "boolean"}},
        "x-unreached": {"type": "null"},
        "const": {"type": "array"},
    }


def test_evaluated_subschemas_lists_every_subschema_each_reference_form_reaches_once():
    schema = _evaluated_schema()
    evaluated = evaluated_subschemas(schema, jsonschema.Draft202012Validator, subject="schema")
    assert evaluated[0] is schema
    assert sorted(map(id, evaluated)) == sorted(set(map(id, evaluated)))
    reached = [schema["properties"][key] for key in ("pointer", "absolute", "anchor", "meta")]
    reached += [schema["x-pointer"], schema["x-absolute"], schema["$defs"]["anchored"]]
    assert {id(sub) for sub in evaluated} == {id(schema), *map(id, reached)}


def test_evaluated_subschemas_checks_the_schema_as_the_rules_check_does():
    schema = {"type": "object", "properties": {"a": {"$ref": "#/missing"}}}
    refusal = re.escape("schema property 'a' breaks the JSON Schema rules: $ref '#/missing' does not resolve")
    with pytest.raises(ValueError, match=refusal):
        evaluated_subschemas(schema, jsonschema.Draft202012Validator, subject="schema")
    with pytest.raises(ValueError, match=refusal):
        check_schema_rules(schema, jsonschema.Draft202012Validator, subject="schema")


def _not_an_index(keyword: str, ref: str, segment: str) -> str:
    return (
        f"{keyword} {ref!r} indexes an array with {segment!r}, which is not a JSON Pointer array index: "
        "write the index as a whole number like 0 or 2"
    )


@pytest.mark.parametrize("segment", ["-1", "+1", " 1", "1 ", "01", "00", "1_0", "\uff11", "\u0661", "%2D1"])
def test_a_pointer_indexes_an_array_only_with_a_json_pointer_array_index(segment):
    # RFC 6901: an array index is ``0`` or ASCII digits without a leading zero. Python's ``int``,
    # which resolves the pointer, reads most of these as some index.
    ref = f"#/properties/a/anyOf/{segment}"
    schema = {
        "type": "object",
        "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "integer"}]}, "b": {"$ref": ref}},
    }
    fault = _not_an_index("$ref", ref, segment.replace("%2D", "-"))
    with pytest.raises(ValueError, match=re.escape(f"schema property 'b' breaks the JSON Schema rules: {fault}")):
        _check(schema)


def test_a_pointer_into_a_meta_schema_indexes_an_array_only_with_a_json_pointer_array_index():
    ref = "https://json-schema.org/draft/2020-12/schema#/allOf/-1"
    with pytest.raises(ValueError, match=re.escape(f"at $ref: {_not_an_index('$ref', ref, '-1')}")):
        _check({"$ref": ref})


@pytest.mark.parametrize("segment", ["0", "1"])
def test_a_pointer_indexing_an_array_with_a_json_pointer_array_index_resolves(segment):
    schema = {
        "type": "object",
        "properties": {
            "a": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            "b": {"$ref": f"#/properties/a/anyOf/{segment}"},
        },
    }
    _check(schema)
    _check({"$ref": f"https://json-schema.org/draft/2020-12/schema#/allOf/{segment}"})


def test_a_dialect_jsonschema_does_not_know_is_read_as_the_dialect_holding_it():
    # ``jsonschema`` evaluates a subschema whose ``$schema`` names no dialect it knows with the
    # class evaluating its holder, so the rules check holds it to that class's rules.
    schema = {
        "$schema": _D7.META_SCHEMA["$schema"],
        "properties": {"a": {"$schema": "https://example.com/x", "type": "string"}},
    }
    _check(schema)
    validator = _D7(schema)
    assert validator.is_valid({"a": "s"})
    assert not validator.is_valid({"a": 1})
    with pytest.raises(ValueError, match=re.escape("schema property 'a' breaks the JSON Schema rules: 5 is not valid")):
        _check(
            {
                "$schema": _D7.META_SCHEMA["$schema"],
                "properties": {"a": {"$schema": "https://example.com/x", "type": 5}},
            }
        )
