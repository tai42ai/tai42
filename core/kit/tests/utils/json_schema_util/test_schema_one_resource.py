"""A schema the rules check accepts is one JSON Schema resource, so each reference has one target.

Only the root may carry an identifier the resolver reads (``$id``, or ``id`` in draft-04 and
draft-03); anchor names are unique. Every reference then resolves to the same subschema however
an instance check reaches it, the walk checks each subschema once per class, and an instance check
of an accepted schema raises nothing but ``ValidationError``.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from typing import Any, Literal

import jsonschema
import pytest
import referencing.exceptions
from jsonschema import FormatChecker
from pydantic import BaseModel

from tai42_kit.utils.data.json_schema_util import SCHEMA_REFS, schema_rules
from tai42_kit.utils.data.json_schema_util.schema_rules import check_schema_rules

from ._dialect_places import _CASE_IDS, _CASES, _CLASSES, _D3, _D4, _D6, _D7, _D2019, _D2020, _at, _check

_D7_URI = _D7.META_SCHEMA["$schema"]
_D4_URI = _D4.META_SCHEMA["$schema"]
_D2019_URI = _D2019.META_SCHEMA["$schema"]
_FAR = "https://example.com/a.json"
_M20 = _D2020.META_SCHEMA["$schema"]
_META_2020 = "https://json-schema.org/draft/2020-12/meta/"


def _below(key: str) -> str:
    # The refusal of an identifier read from ``key`` below the root, after the identifier.
    return (
        "below the root is not allowed: the schema is one resource, so every reference resolves inside it "
        f"— drop the {key} and point references at '#/…' or at an anchor"
    )


# The key each class reads an identifier from.
_ID_KEY: dict[type, str] = {_D3: "id", _D4: "id", _D6: "$id", _D7: "$id", _D2019: "$id", _D2020: "$id"}


def _evaluates_cleanly(schema: dict[str, Any], instances: list[Any]) -> None:
    # The instance check the answer doors build, raising only ``ValidationError`` on each instance.
    cls = schema_rules.schema_validator_class(schema, _D2020, subject="schema")
    for instance in instances:
        list(cls(schema, format_checker=FormatChecker(), registry=SCHEMA_REFS).iter_errors(instance))


@pytest.mark.parametrize(("cls", "place"), _CASES, ids=_CASE_IDS)
def test_an_identifier_below_the_root_is_refused_at_each_place(cls, place):
    key = _ID_KEY[cls]
    schema, _ = _at(cls, place, {key: _FAR, "type": "object"})
    with pytest.raises(ValueError, match=re.escape(f"{key} {_FAR!r} {_below(key)}")):
        _check(schema)


@pytest.mark.parametrize(("cls", "place"), _CASES, ids=_CASE_IDS)
def test_an_identifier_the_class_ignores_beside_a_ref_is_accepted_at_each_place(cls, place):
    if cls in (_D2019, _D2020):
        pytest.skip("from draft 2019-09 on a $id beside $ref is read")
    schema, instance = _at(cls, place, {_ID_KEY[cls]: _FAR, "$ref": "#/definitions/q"})
    schema["definitions"] = {"q": {"type": "object"}}
    _check(schema)
    _evaluates_cleanly(schema, [instance])


# The draft-03 places where the resolver's registry finds no anchor: it does not look
# under ``type`` or ``disallow`` entries, and a single ``extends`` schema breaks its crawl.
_D3_UNSEARCHED = {"extends", "type/0", "disallow/0"}


@pytest.mark.parametrize(("cls", "place"), _CASES, ids=_CASE_IDS)
def test_a_legacy_anchor_resolves_where_the_registry_finds_it(cls, place):
    if cls in (_D2019, _D2020):
        pytest.skip("from draft 2019-09 on an anchor is $anchor")
    schema, instance = _at(cls, place, {_ID_KEY[cls]: "#inner", "type": "object"})
    schema["properties"] = {**schema.get("properties", {}), "z": {"$ref": "#inner"}}
    if cls is _D3 and place in _D3_UNSEARCHED:
        with pytest.raises(ValueError, match=re.escape("$ref '#inner' does not resolve")):
            _check(schema)
        with pytest.raises((referencing.exceptions.Unresolvable, AttributeError)):
            list(cls(schema, registry=SCHEMA_REFS).iter_errors({"z": {}}))
        return
    _check(schema)
    _evaluates_cleanly(schema, [instance, {"z": 1}, {"z": {}}])


def test_an_identifier_read_by_a_nested_dialect_is_refused():
    schema = {
        "$defs": {"old": {"$schema": _D4_URI, "id": _FAR, "type": "string"}},
        "properties": {"p": {"$ref": "#/$defs/old"}},
    }
    with pytest.raises(
        ValueError, match=re.escape(f"schema breaks the JSON Schema rules at $defs/old/id: id {_FAR!r} {_below('id')}")
    ):
        _check(schema)


def test_an_identifier_in_a_definition_no_reference_reaches_is_refused():
    with pytest.raises(ValueError, match=re.escape(f"at $defs/x/$id: $id {_FAR!r} {_below('$id')}")):
        _check({"$defs": {"x": {"$id": _FAR}}})


def test_an_identifier_in_a_reference_target_under_an_unknown_keyword_is_refused():
    with pytest.raises(ValueError, match=re.escape(f"at x-target/$id: $id {_FAR!r} {_below('$id')}")):
        _check({"x-target": {"$id": _FAR, "type": "string"}, "properties": {"p": {"$ref": "#/x-target"}}})


def test_the_root_may_carry_an_identifier_and_references_under_it():
    schema = {
        "$id": "https://example.com/r",
        "$defs": {"a": {"type": "string"}},
        "properties": {"p": {"$ref": "https://example.com/r#/$defs/a"}},
    }
    _check(schema)
    _evaluates_cleanly(schema, [{"p": "s"}, {"p": 1}])


def _twice(message_at: str, keyword: str) -> str:
    return (
        f"schema breaks the JSON Schema rules at {message_at}: "
        f"anchor 'x' ({keyword}) is declared more than once, so '#x' could resolve to either"
    )


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"$defs": {"a": {"$anchor": "x"}, "b": {"$anchor": "x"}}}, _twice("$defs/b/$anchor", "$anchor")),
        (
            {"$defs": {"a": {"$anchor": "x"}, "b": {"$dynamicAnchor": "x"}}},
            _twice("$defs/b/$dynamicAnchor", "$dynamicAnchor"),
        ),
        ({"$anchor": "x", "$defs": {"b": {"$anchor": "x"}}}, _twice("$defs/b/$anchor", "$anchor")),
        ({"$defs": {"b": {"$anchor": "x"}}, "$dynamicAnchor": "x"}, _twice("$defs/b/$anchor", "$anchor")),
        (
            {"$schema": _D7_URI, "definitions": {"a": {"$id": "#x"}, "b": {"$id": "#x"}}},
            _twice("definitions/b/$id", "$id"),
        ),
        (
            {"$schema": _D4_URI, "definitions": {"a": {"id": "#x"}, "b": {"id": "#x"}}},
            _twice("definitions/b/id", "id"),
        ),
    ],
    ids=["anchor-anchor", "anchor-dynamic", "root-defs", "defs-then-root", "draft-07", "draft-04"],
)
def test_an_anchor_declared_twice_is_refused_at_its_second_declaration(schema, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        _check(schema)


def test_an_anchor_under_an_unknown_keyword_is_not_one():
    _check({"x-note": {"$anchor": "x"}, "$defs": {"a": {"$anchor": "x", "type": "string"}}, "$ref": "#x"})
    with pytest.raises(ValueError, match=re.escape("$ref '#x' does not resolve: no $anchor or $dynamicAnchor 'x'")):
        _check({"x-note": {"$anchor": "x"}, "properties": {"p": {"$ref": "#x"}}})


@pytest.mark.parametrize(
    "schema",
    [
        {
            "$defs": {"q": {"type": "string"}},
            "properties": {"p": {"oneOf": [{"$id": "https://e.com/x/", "$ref": "#/$defs/q"}, {"type": "integer"}]}},
        },
        {
            "type": "object",
            "unevaluatedProperties": False,
            "allOf": [
                {
                    "$id": "https://e.com/sub/",
                    "$defs": {"q": {"properties": {"a": {"type": "string"}}}},
                    "$ref": "#/$defs/q",
                }
            ],
        },
        {
            "$schema": _D2019_URI,
            "type": "object",
            "unevaluatedProperties": False,
            "allOf": [{"$id": "https://e.com/sub/", "$defs": {"q": {}}, "$ref": "#/$defs/q"}],
        },
        {
            "$defs": {"q": {"type": "string"}},
            "properties": {"p": {"not": {"$id": "https://e.com/n/", "$ref": "#/$defs/q"}}},
        },
    ],
    ids=["oneOf", "unevaluatedProperties", "2019-09-unevaluatedProperties", "not"],
)
def test_a_branch_the_instance_check_would_rebase_inconsistently_is_refused(schema):
    with pytest.raises(ValueError, match=re.escape(_below("$id"))):
        _check(schema)


def _resources(count: int, mode: str) -> dict[str, Any]:
    # ``count`` resources each referencing every other by its ``$id``, each one a recursive or a
    # dynamic anchor holder: a dynamic scope the walk would otherwise follow in every order.
    names = [f"r{i}" for i in range(count)]
    defs: dict[str, Any] = {}
    for i, name in enumerate(names):
        held: dict[str, Any] = {
            "$id": f"https://e.com/{name}",
            "properties": {f"p{j}": {"$ref": f"https://e.com/{o}"} for j, o in enumerate(names) if o != name},
        }
        if mode == "recursive":
            held["$recursiveAnchor"] = True
            held["properties"]["self"] = {"$recursiveRef": "#"}
        else:
            held["$defs"] = {f"a{j}": {"$dynamicAnchor": f"a{j}"} for j in range(i + 1)}
            held["properties"].update({f"d{j}": {"$dynamicRef": f"#a{j}"} for j in range(i + 1)})
        defs[name] = held
    schema: dict[str, Any] = {
        "$id": "https://e.com/root",
        "$defs": defs,
        "properties": {"x": {"$ref": "https://e.com/r0"}},
    }
    if mode == "recursive":
        schema.update({"$schema": _D2019_URI, "$recursiveAnchor": True})
    return schema


@pytest.mark.parametrize(("count", "mode"), [(5, "recursive"), (12, "dynamic")])
def test_many_resources_reading_the_dynamic_scope_are_refused_at_once(count, mode):
    started = time.perf_counter()
    with pytest.raises(ValueError, match=re.escape(_below("$id"))):
        _check(_resources(count, mode))
    assert time.perf_counter() - started < 1.0


def test_a_long_chain_of_dynamic_anchors_in_one_resource_is_accepted_at_once():
    depth = 12
    defs = {
        f"n{i}": {
            "$dynamicAnchor": f"a{i}",
            "type": "object",
            "properties": {f"d{j}": {"$dynamicRef": f"#a{j}"} for j in range(depth)},
        }
        for i in range(depth)
    }
    schema = {"$defs": defs, "properties": {"x": {"$ref": "#/$defs/n0"}}}
    started = time.perf_counter()
    _check(schema)
    assert time.perf_counter() - started < 1.0
    _evaluates_cleanly(schema, [{"x": {"d3": {"d7": {}}}}, {"x": {"d1": 5}}])


_ONE_RESOURCE: dict[str, tuple[dict[str, Any], list[Any]]] = {
    "dynamic-tree": (
        {
            "$dynamicAnchor": "node",
            "type": "object",
            "properties": {"data": True, "children": {"type": "array", "items": {"$dynamicRef": "#node"}}},
            "unevaluatedProperties": False,
        },
        [{"children": [{"x": 1}]}, {"children": [{"children": [{"data": 1}]}]}],
    ),
    "recursive-tree-2019": (
        {
            "$schema": _D2019_URI,
            "$recursiveAnchor": True,
            "type": "object",
            "properties": {"data": True, "children": {"type": "array", "items": {"$recursiveRef": "#"}}},
            "unevaluatedProperties": False,
        },
        [{"children": [{"x": 1}]}, {"children": [{"children": [{"data": 1}]}]}],
    ),
    "anchor": (
        {"$defs": {"s": {"$anchor": "S", "type": "string"}}, "properties": {"p": {"$ref": "#S"}}},
        [{"p": "a"}, {"p": 1}],
    ),
    "oneOf-refs": (
        {
            "$defs": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "properties": {"p": {"oneOf": [{"$ref": "#/$defs/a"}, {"$ref": "#/$defs/b"}]}},
        },
        [{"p": "a"}, {"p": 1}, {"p": True}],
    ),
    "unevaluated-ref": (
        {
            "$defs": {"base": {"properties": {"a": {}}}},
            "allOf": [{"$ref": "#/$defs/base"}],
            "properties": {"b": {}},
            "unevaluatedProperties": False,
        },
        [{"a": 1, "b": 2}, {"a": 1, "c": 2}],
    ),
    "if-then-ref": (
        {
            "$defs": {"t": {"properties": {"k": {"const": "x"}}, "required": ["k"]}},
            "if": {"$ref": "#/$defs/t"},
            "then": {"required": ["v"]},
        },
        [{"k": "x"}, {"k": "x", "v": 1}, {"k": "y"}],
    ),
    "draft-07-ref-hides-id": (
        {
            "$schema": _D7_URI,
            "definitions": {"q": {"type": "string"}},
            "properties": {"a": {"$id": "https://e.com/x/", "$ref": "#/definitions/q"}},
        },
        [{"a": "s"}, {"a": 1}],
    ),
    "draft-07-anchor": (
        {
            "$schema": _D7_URI,
            "definitions": {"q": {"$id": "#q", "type": "string"}},
            "properties": {"a": {"$ref": "#q"}},
        },
        [{"a": "s"}, {"a": 1}],
    ),
    "nested-dialect-ref": (
        {
            "$defs": {"q": {"type": "string"}, "old": {"$schema": _D7_URI, "properties": {"z": {"$ref": "#/$defs/q"}}}},
            "properties": {"p": {"$ref": "#/$defs/old"}},
        },
        [{"p": {"z": "s"}}, {"p": {"z": 1}}],
    ),
    "root-id-absolute-ref": (
        {
            "$id": "https://e.com/r",
            "$defs": {"a": {"type": "string"}},
            "properties": {"p": {"$ref": "https://e.com/r#/$defs/a"}},
        },
        [{"p": "s"}, {"p": 1}],
    ),
    "root-id-dynamic-tree": (
        {
            "$id": "https://e.com/r",
            "$dynamicAnchor": "node",
            "type": "object",
            "properties": {"children": {"type": "array", "items": {"$dynamicRef": "#node"}}},
            "unevaluatedProperties": False,
        },
        [{"children": [{"x": 1}]}, {"children": [{"children": []}]}],
    ),
    "meta-schema": ({"$ref": "https://json-schema.org/draft/2020-12/schema"}, [{"type": "string"}, {"type": 5}]),
    "meta-vocabulary": ({"$ref": "https://json-schema.org/draft/2020-12/meta/core"}, [{"$id": "x"}]),
    # A meta-schema's dynamic anchor name declared on the root under its ``$id``: the meta-schema's
    # ``$dynamicRef`` re-enters the root, which carries the base its references resolve against.
    "root-dynamic-anchor-meta": (
        {
            "$id": "https://e.com/r",
            "$dynamicAnchor": "meta",
            "$ref": _M20,
            "properties": {"x-k": {"$ref": "#/$defs/y"}},
            "$defs": {"y": {"type": "string"}},
        },
        [{"properties": {"a": {"x-k": "s"}}}, {"properties": {"a": {"x-k": 1}}}],
    ),
    # Below the root, with no reference into a meta-schema: the schema's own ``$dynamicRef`` binds it.
    "dynamic-anchor-meta-no-meta-reference": (
        {
            "$id": "https://e.com/r",
            "properties": {"a": {"$dynamicRef": "#meta"}},
            "$defs": {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "string"}},
        },
        [{"a": "s"}, {"a": 1}],
    ),
    # Below the root, with no root ``$id``: the meta-schema's ``$dynamicRef`` never reaches it.
    "dynamic-anchor-meta-no-root-id": (
        {
            "$ref": _M20,
            "properties": {"x-k": {"$ref": "#/$defs/x"}},
            "$defs": {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "string"}},
        },
        [{"properties": {"a": {"x-k": "s"}}}, {"properties": {"a": {"x-k": 1}}}],
    ),
    "root-id-empty-dynamic-anchor-meta": (
        {"$id": "", "$ref": _M20, "$defs": {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {}}},
        [{"properties": {"a": {}}}],
    ),
    # A dynamic anchor name no meta-schema binds, below the root beside a meta-schema reference.
    "other-dynamic-anchor-meta-reference": (
        {
            "$id": "https://e.com/r",
            "$ref": _M20,
            "properties": {"x-k": {"$dynamicRef": "#t"}},
            "$defs": {"x": {"$dynamicAnchor": "t", "$ref": "#/$defs/y"}, "y": {"type": "string"}},
        },
        [{"properties": {"a": {"x-k": "s"}}}, {"properties": {"a": {"x-k": 1}}}],
    ),
    "static-anchor-meta": (
        {"$id": "https://e.com/r", "$ref": _M20, "$defs": {"x": {"$anchor": "meta", "$ref": "#/$defs/y"}, "y": {}}},
        [{"type": "string"}, {"type": 5}],
    ),
    "recursive-anchor-root-2019-meta": (
        {
            "$schema": _D2019_URI,
            "$id": "https://e.com/r",
            "$recursiveAnchor": True,
            "$ref": _D2019_URI,
            "properties": {"x-k": {"$ref": "#/$defs/y"}},
            "$defs": {"y": {"type": "string"}},
        },
        [{"properties": {"a": {"x-k": "s"}}}, {"properties": {"a": {"x-k": 1}}}],
    ),
    "recursive-anchor-below-root-2019-meta": (
        {
            "$schema": _D2019_URI,
            "$id": "https://e.com/r",
            "$ref": _D2019_URI,
            "$defs": {"x": {"$recursiveAnchor": True, "$ref": "#/$defs/y"}, "y": {"type": "string"}},
        },
        [{"type": "string"}, {"properties": {"a": {"type": 5}}}],
    ),
    "meta-boolean-target": (
        {"$ref": _M20 + "#/$vocabulary/https:~1~1json-schema.org~1draft~12020-12~1vocab~1core"},
        [1, "x", {"a": 1}],
    ),
    # A meta-schema reference beside the 2020-12 meta-schemas' dynamic anchor name below the root,
    # where the meta-schema reached holds no reference naming it: nothing re-enters the schema.
    "draft-07-meta-reference-dynamic-anchor-meta": (
        {
            "$id": "https://e.com/r",
            "$ref": _D7_URI,
            "$defs": {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}},
        },
        [{"properties": {"a": {}}}, {"type": 5}],
    ),
    "validation-vocabulary-dynamic-anchor-meta": (
        {
            "$id": "https://e.com/r",
            "$ref": _META_2020 + "validation",
            "$defs": {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}},
        },
        [{"type": "string"}, {"type": 5}],
    ),
    "validation-pointer-dynamic-anchor-meta": (
        {
            "$id": "https://e.com/r",
            "$ref": _META_2020 + "validation#/$defs/nonNegativeInteger",
            "$defs": {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}},
        },
        [3, -1],
    ),
    "root-id-own-dynamic-anchor": (
        {
            "$id": "https://e.com/r",
            "properties": {"a": {"$dynamicRef": "#t"}},
            "$defs": {"t": {"$dynamicAnchor": "t", "type": "string"}},
        },
        [{"a": "s"}, {"a": 1}],
    ),
}


@pytest.mark.parametrize(("schema", "instances"), list(_ONE_RESOURCE.values()), ids=list(_ONE_RESOURCE))
def test_the_instance_check_agrees_with_the_rules_check_on_one_resource(schema, instances):
    _check(schema)
    _evaluates_cleanly(schema, instances)


_DEFINITIONS: dict[type, tuple[str, ...]] = {
    _D3: (),
    _D4: ("definitions",),
    _D6: ("definitions",),
    _D7: ("definitions",),
    _D2019: ("$defs", "definitions"),
    _D2020: ("$defs", "definitions"),
}


def _seeded_cases() -> list[tuple[type, str, str]]:
    cases: list[tuple[type, str, str]] = []
    for cls, place in _CASES:
        for container in _DEFINITIONS[cls]:
            cases.append((cls, place, container))
        cases.append((cls, place, place))
    return cases


@pytest.mark.parametrize(
    ("cls", "outer", "inner"), _seeded_cases(), ids=lambda value: getattr(value, "__name__", value)
)
def test_every_subschema_seeded_as_meta_checked_is_one_the_root_check_meta_checks(cls, outer, inner):
    # Two levels deep: ``inner`` (an applicator place or a definitions container) inside the
    # subschema at ``outer``. A fault there fails the class's own ``check_schema``.
    marker: dict[str, Any] = {"type": 5}
    if inner in _DEFINITIONS[cls]:
        held: dict[str, Any] = {inner: {"m": marker}}
    else:
        held, _ = _at(cls, inner, marker)
        del held["$schema"]
    schema, _ = _at(cls, outer, held)
    assert id(marker) in {subschema for subschema, _ in schema_rules._meta_checked_by_root(schema, cls)}
    with pytest.raises(jsonschema.SchemaError):
        cls.check_schema(schema)


def test_a_reference_target_under_an_unknown_keyword_is_still_held_to_the_rules():
    with pytest.raises(
        ValueError, match=re.escape("schema breaks the JSON Schema rules at x-target/type: 5 is not valid")
    ):
        _check({"x-target": {"type": 5}, "$ref": "#/x-target"})


class _Address(BaseModel):
    street: str
    zip: str | None = None


class _Item(BaseModel):
    name: str
    qty: int
    address: _Address | None = None


class _Node(BaseModel):
    label: str
    children: list[_Node] = []


class _Reply(BaseModel):
    title: str
    items: list[_Item]
    tree: _Node
    kind: Literal["a", "b"]
    extra: dict[str, _Address] = {}


def _counted_meta_checks(monkeypatch) -> list[tuple[int, type]]:
    calls: list[tuple[int, type]] = []
    meta_check = schema_rules._meta_check

    def counted(subschema: Any, validator: type, place: Any) -> None:
        calls.append((id(subschema), validator))
        meta_check(subschema, validator, place)

    monkeypatch.setattr(schema_rules, "_meta_check", counted)
    return calls


def test_each_subschema_is_meta_checked_once_per_class(monkeypatch):
    calls = _counted_meta_checks(monkeypatch)
    schema = _Reply.model_json_schema()
    check_schema_rules(schema, _D2020, subject="schema")
    assert calls == [(id(schema), _D2020)]
    assert set(Counter(calls).values()) == {1}


def test_a_definition_many_references_share_is_covered_by_the_root_meta_check(monkeypatch):
    calls = _counted_meta_checks(monkeypatch)
    big = {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(200)}}
    schema = {"properties": {f"p{i}": {"$ref": "#/$defs/big"} for i in range(300)}, "$defs": {"big": big}}
    check_schema_rules(schema, _D2020, subject="schema")
    assert calls == [(id(schema), _D2020)]


def test_a_target_under_an_unknown_keyword_is_meta_checked_once(monkeypatch):
    calls = _counted_meta_checks(monkeypatch)
    target = {"type": "object", "properties": {"c": {"type": "string"}}}
    schema = {"properties": {f"p{i}": {"$ref": "#/x/t"} for i in range(300)}, "x": {"t": target}}
    check_schema_rules(schema, _D2020, subject="schema")
    assert calls == [(id(schema), _D2020), (id(target), _D2020)]


@pytest.mark.parametrize("cls", _CLASSES, ids=lambda cls: cls.__name__)
def test_the_root_seeds_only_itself_when_it_holds_no_subschema(cls):
    schema = {"$schema": cls.META_SCHEMA["$schema"]}
    assert set(schema_rules._meta_checked_by_root(schema, cls)) == {(id(schema), cls)}


# The refusal of the 2020-12 meta-schema's dynamic anchor name below the root, after the path.
_REENTRY = (
    "$dynamicAnchor 'meta' below the root is not allowed when the schema has an $id and refers into a JSON "
    "Schema meta-schema: the meta-schema's $dynamicRef '#meta' would re-enter the schema here and resolve its "
    "references against the meta-schema — rename the anchor"
)
_X_TO_Y = {"x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {"type": "object"}}
_REENTERED: dict[str, tuple[dict[str, Any], str]] = {
    "pointer-nowhere": ({"$id": "https://e.com/r", "$ref": _M20, "$defs": _X_TO_Y}, "$defs/x"),
    "pointer-wrong-target": (
        {
            "$id": "https://e.com/r",
            "$ref": _M20,
            "$defs": {
                "x": {"$dynamicAnchor": "meta", "$ref": "#/$defs/schemaArray"},
                "schemaArray": {"type": "string"},
            },
        },
        "$defs/x",
    ),
    "pattern-keys-joined": (
        {
            "$id": "https://e.com/r",
            "$ref": _M20,
            "$defs": {
                "x": {
                    "$dynamicAnchor": "meta",
                    "patternProperties": {"x": {}, "(?i)y": {}},
                    "additionalProperties": False,
                }
            },
        },
        "$defs/x",
    ),
    "statically-reached": (
        {"$id": "https://e.com/r", "$ref": _M20, "properties": {"p": {"$ref": "#/$defs/x"}}, "$defs": _X_TO_Y},
        "$defs/x",
    ),
    "own-dynamic-ref-inside": (
        {
            "$id": "https://e.com/r",
            "$ref": _M20,
            "$defs": {
                "x": {"$dynamicAnchor": "meta", "properties": {"k": {"$dynamicRef": "#t"}}},
                "t": {"$dynamicAnchor": "t", "type": "string"},
            },
        },
        "$defs/x",
    ),
    "vocabulary": (
        {"$id": "https://e.com/r", "$ref": "https://json-schema.org/draft/2020-12/meta/applicator", "$defs": _X_TO_Y},
        "$defs/x",
    ),
    "relative-root-id": ({"$id": "r.json", "$ref": _M20, "$defs": _X_TO_Y}, "$defs/x"),
    "urn-root-id": ({"$id": "urn:x:y", "$ref": _M20, "$defs": _X_TO_Y}, "$defs/x"),
    "root-id-empty-fragment": ({"$id": "https://e.com/r#", "$ref": _M20, "$defs": _X_TO_Y}, "$defs/x"),
    "meta-reference-nested": (
        {"$id": "https://e.com/r", "properties": {"s": {"$ref": _M20}}, "$defs": _X_TO_Y},
        "$defs/x",
    ),
    "draft-07-root": (
        {
            "$schema": _D7_URI,
            "$id": "https://e.com/r#foo",
            "allOf": [{"$ref": _M20}],
            "definitions": {"x": {"$schema": _M20, "$dynamicAnchor": "meta", "$ref": "#/definitions/y"}, "y": {}},
        },
        "definitions/x",
    ),
    "draft-2019-root": (
        {
            "$schema": _D2019_URI,
            "$id": "https://e.com/r",
            "allOf": [{"$ref": _M20}],
            "$defs": {"x": {"$schema": _M20, "$dynamicAnchor": "meta", "$ref": "#/$defs/y"}, "y": {}},
        },
        "$defs/x",
    ),
}


@pytest.mark.parametrize(("schema", "place"), list(_REENTERED.values()), ids=list(_REENTERED))
def test_a_meta_schema_dynamic_anchor_name_below_the_root_is_refused_when_a_meta_schema_could_re_enter(schema, place):
    with pytest.raises(ValueError, match=re.escape(f"at {place}/$dynamicAnchor: {_REENTRY}")):
        _check(schema)


def test_a_meta_schema_dynamic_anchor_name_under_a_property_names_the_property():
    schema = {"$id": "https://e.com/r", "$ref": _M20, "properties": {"p": {"$dynamicAnchor": "meta", "type": "object"}}}
    with pytest.raises(ValueError, match=re.escape(f"schema property 'p' breaks the JSON Schema rules: {_REENTRY}")):
        _check(schema)


def _entered(keyword: str, ref: str) -> str:
    # The refusal of the 2020-12 meta-schemas' dynamic anchor name below the root, after the path,
    # when the reference ``ref`` under ``keyword`` names that dynamic anchor in a meta-schema.
    return (
        "$dynamicAnchor 'meta' below the root is not allowed when the schema has an $id and refers into a JSON "
        f"Schema meta-schema: {keyword} {ref!r} names the meta-schema's $dynamicAnchor 'meta', which would re-enter "
        "the schema here and resolve its references against the meta-schema — rename the anchor"
    )


_X_ALONE = {"x": {"$dynamicAnchor": "meta", "type": "object"}}
# A reference naming a meta-schema's dynamic anchor, which resolves to the schema's own dynamic
# anchor of that name: the lookup lands inside the schema with the meta-schema's base URI.
_ENTERED: dict[str, tuple[dict[str, Any], str, str]] = {
    "ref-meta-anchor": ({"$id": "https://e.com/r", "$ref": _M20 + "#meta", "$defs": _X_TO_Y}, "$ref", _M20 + "#meta"),
    "dynamic-ref-meta-anchor": (
        {"$id": "https://e.com/r", "$dynamicRef": _M20 + "#meta", "$defs": _X_TO_Y},
        "$dynamicRef",
        _M20 + "#meta",
    ),
    "applicator-meta-anchor": (
        {"$id": "https://e.com/r", "$ref": _META_2020 + "applicator#meta", "$defs": _X_TO_Y},
        "$ref",
        _META_2020 + "applicator#meta",
    ),
    "validation-meta-anchor": (
        {"$id": "https://e.com/r", "$ref": _META_2020 + "validation#meta", "$defs": _X_TO_Y},
        "$ref",
        _META_2020 + "validation#meta",
    ),
    "anchor-without-references": (
        {"$id": "https://e.com/r", "$ref": _M20 + "#meta", "$defs": _X_ALONE},
        "$ref",
        _M20 + "#meta",
    ),
    "under-a-property": (
        {"$id": "https://e.com/r", "properties": {"m": {"$ref": _M20 + "#meta"}}, "$defs": _X_TO_Y},
        "$ref",
        _M20 + "#meta",
    ),
}


@pytest.mark.parametrize(("schema", "keyword", "ref"), list(_ENTERED.values()), ids=list(_ENTERED))
def test_a_reference_naming_a_meta_schema_dynamic_anchor_is_refused_beside_that_name_below_the_root(
    schema, keyword, ref
):
    with pytest.raises(ValueError, match=re.escape(f"at $defs/x/$dynamicAnchor: {_entered(keyword, ref)}")):
        _check(schema)


@pytest.mark.parametrize(
    ("schema", "instance"),
    [
        ({"$id": "https://e.com/r", "$ref": _M20 + "#meta", "$defs": _X_TO_Y}, {"a": 1}),
        ({"$id": "https://e.com/r", "$ref": _META_2020 + "validation#meta", "$defs": _X_TO_Y}, {"a": 1}),
    ],
    ids=["schema", "validation-vocabulary"],
)
def test_the_instance_check_resolves_a_re_entered_anchor_against_the_meta_schema(schema, instance):
    # What the refusal prevents: ``$defs/x``'s own reference resolved inside the meta-schema.
    validator = _D2020(schema, format_checker=FormatChecker(), registry=SCHEMA_REFS)
    with pytest.raises(referencing.exceptions.Unresolvable):
        list(validator.iter_errors(instance))


def test_a_re_entered_anchor_without_references_gives_the_anchor_s_verdict():
    # What the refusal prevents: the anchor answers for the meta-schema, so ``{"type": 5}`` fits.
    schema = {"$id": "https://e.com/r", "$ref": _M20 + "#meta", "$defs": _X_ALONE}
    assert _D2020(schema, registry=SCHEMA_REFS).is_valid({"type": 5})
    assert not _D2020({"$ref": _M20 + "#meta"}, registry=SCHEMA_REFS).is_valid({"type": 5})
