"""The platform state-template document: validation of the platform keys, the refusal of
any key outside them, and the pure compose/regime transforms."""

from __future__ import annotations

import dataclasses
import re

import pytest
from jsonschema import Draft202012Validator
from tai42_contract.states.errors import AttachConflictError, SchemaValidationError, TemplateValidationError
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.json_schema_util import evaluated_subschemas

from tai42_skeleton.states import schema as states_schema
from tai42_skeleton.states.schema import _validate_schema
from tai42_skeleton.states.templates import (
    StateTemplate,
    compose_effective_schema,
    regime_for,
    substitute_parameters,
    validate_template,
)


def _doc(**over):
    doc = {
        "kind": "state-template",
        "name": "demo",
        "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
    }
    doc.update(over)
    return doc


def test_validate_minimal_template() -> None:
    template = validate_template(_doc())
    assert isinstance(template, StateTemplate)
    assert template.name == "demo"
    assert template.trace.enabled is False
    assert template.declarations is None
    # round-trips through to_document
    assert validate_template(template.to_document()).name == "demo"


def test_wrong_kind_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template kind"):
        validate_template(_doc(kind="plugin_consumer"))


def test_bad_name_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template name"):
        validate_template(_doc(name="Not A Name"))


@pytest.mark.parametrize("name", ["a--b", "-a", "a-", "ab-"])
def test_template_name_slug_forbids_edge_and_double_hyphens(name: str) -> None:
    # The slug rule keeps the qualified ``tjq_<template>__<jq>`` handle unambiguous: a
    # leading/trailing/consecutive hyphen is refused.
    with pytest.raises(TemplateValidationError, match="template name"):
        validate_template(_doc(name=name))


def test_template_name_accepts_a_single_internal_hyphen() -> None:
    assert validate_template(_doc(name="a-b")).name == "a-b"


@pytest.mark.parametrize("key", ["loop", "bogus", "widgets", "predicates"])
def test_unknown_key_refused_plainly(key: str) -> None:
    # Any key outside the platform set is refused, naming what a state-template document
    # holds (template_jq/reconcile are IN the set; a consumer's own key like ``loop`` is not).
    with pytest.raises(TemplateValidationError, match=f"{key}\n  Extra inputs are not permitted"):
        validate_template(_doc(**{key: {}}))


def test_parameters_and_defaults() -> None:
    # The marker sits at a sub-schema position, so its default is a JSON-Schema fragment.
    doc = _doc(
        schema={"type": "object", "properties": {"cap": {"$parameter": "cap"}}},
        parameters={"cap": {"schema": {"type": "object"}, "default": {"type": "integer"}}},
    )
    template = validate_template(doc)
    assert template.defaults() == {"cap": {"type": "integer"}}


def test_no_default_parameter_must_appear_as_marker() -> None:
    doc = _doc(
        schema={"type": "object", "properties": {"x": {"type": "string"}}},
        parameters={"cap": {"schema": {"type": "integer"}}},
    )
    with pytest.raises(TemplateValidationError, match=r"must appear as a \$parameter marker"):
        validate_template(doc)


def test_schema_references_undeclared_parameter() -> None:
    doc = _doc(schema={"type": "object", "properties": {"c": {"$parameter": "cap"}}})
    with pytest.raises(TemplateValidationError, match="undeclared parameter"):
        validate_template(doc)


def test_regime_valid_and_invalid_path() -> None:
    ok = _doc(
        schema={"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}},
        regimes=[{"path": ["items"], "regime": "composing"}],
    )
    template = validate_template(ok)
    assert template.regimes[0].regime == "composing"
    bad = _doc(regimes=[{"path": ["nope"], "regime": "single"}])
    with pytest.raises(TemplateValidationError, match="not a property of the fragment"):
        validate_template(bad)


def test_regime_wildcard_needs_array() -> None:
    bad = _doc(
        schema={"type": "object", "properties": {"x": {"type": "string"}}},
        regimes=[{"path": ["x", "*"], "regime": "composing"}],
    )
    with pytest.raises(TemplateValidationError):
        validate_template(bad)


def test_declarations_check_compiles() -> None:
    ok = _doc(
        declarations={
            "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
            "check": {"content": ".n > 0"},
        }
    )
    template = validate_template(ok)
    assert template.declarations is not None
    assert template.declarations.check == TemplatedText(content=".n > 0")
    bad = _doc(declarations={"schema": {"type": "object"}, "check": {"content": "this is (not jq"}})
    with pytest.raises(TemplateValidationError, match="not a valid jq"):
        validate_template(bad)


def test_declarations_check_by_id_is_accepted_and_round_trips() -> None:
    # A by-id check is not compiled by the pure validator (it cannot fetch the resource); the
    # save door renders and compiles it. Validate accepts the shape and ``to_document`` emits
    # the nested templated text verbatim.
    doc = _doc(declarations={"schema": {"type": "object"}, "check": {"id": "count-check", "kwargs": {"limit": 3}}})
    template = validate_template(doc)
    assert template.declarations is not None
    assert template.declarations.check == TemplatedText(id="count-check", kwargs={"limit": 3})
    assert template.to_document()["declarations"]["check"] == {"id": "count-check", "kwargs": {"limit": 3}}


def test_trace_enabled() -> None:
    template = validate_template(_doc(trace={"enabled": True}))
    assert template.trace.enabled is True
    assert template.to_document()["trace"] == {"enabled": True}


def test_fragment_must_be_object_schema() -> None:
    with pytest.raises(TemplateValidationError, match="valid object schema"):
        validate_template(_doc(schema={"type": "string"}))


def test_substitute_parameters_is_pure() -> None:
    fragment = {"a": {"$parameter": "p"}, "b": 1}
    out = substitute_parameters(fragment, {"p": {"x": 1}})
    assert out == {"a": {"x": 1}, "b": 1}
    # the input is never mutated
    assert fragment == {"a": {"$parameter": "p"}, "b": 1}
    # a marker with no value is left standing
    assert substitute_parameters({"$parameter": "q"}, {}) == {"$parameter": "q"}


def test_compose_effective_schema_places_fragment() -> None:
    base = {"type": "object", "properties": {"top": {"type": "string"}}}
    template = validate_template(_doc(name="m", schema={"type": "object", "properties": {"y": {"type": "integer"}}}))
    effective = compose_effective_schema(base, [(template, ["sub"], {})])
    assert effective["properties"]["top"] == {"type": "string"}
    assert effective["properties"]["sub"]["properties"]["y"] == {"type": "integer"}


def test_compose_effective_schema_collision_refused() -> None:
    base = {"type": "object", "properties": {"sub": {"type": "string"}}}
    template = validate_template(_doc(name="m"))
    with pytest.raises(AttachConflictError, match="collides"):
        compose_effective_schema(base, [(template, ["sub"], {})])


def test_compose_effective_schema_overlap_refused() -> None:
    template = validate_template(_doc(name="m"))
    base = {"type": "object", "properties": {}}
    with pytest.raises(AttachConflictError, match="overlaps"):
        compose_effective_schema(base, [(template, ["a"], {}), (template, ["a", "b"], {})])


_BASE = {"type": "object", "properties": {"n": {"type": "integer"}}}
_RECURSIVE_FRAGMENT = {
    "type": "object",
    "properties": {"tag": {"$ref": "#/$defs/t"}, "child": {"$ref": "#"}},
    "$defs": {"t": {"type": "string"}},
}


def _paths(effective: dict, doc: dict) -> list[str]:
    return sorted(error.json_path for error in Draft202012Validator(effective).iter_errors(doc))


def test_compose_rebases_fragment_pointer_refs_to_the_attach_path() -> None:
    template = validate_template(_doc(name="m", schema=_RECURSIVE_FRAGMENT))
    effective = compose_effective_schema(_BASE, [(template, ["box"], {})])
    box = effective["properties"]["box"]
    assert box["properties"]["tag"] == {"$ref": "#/properties/box/$defs/t"}
    assert box["properties"]["child"] == {"$ref": "#/properties/box"}
    _validate_schema(effective)
    assert _paths(effective, {"box": {"tag": 1}}) == ["$.box.tag"]
    assert _paths(effective, {"box": {"child": {"tag": 5}}}) == ["$.box.child.tag"]
    # ``#`` is the fragment's own root, never the state's: a base field name inside the child is free.
    assert _paths(effective, {"box": {"child": {"n": "s"}}}) == []


def test_compose_rebases_refs_at_a_nested_path() -> None:
    template = validate_template(_doc(name="m", schema=_RECURSIVE_FRAGMENT))
    effective = compose_effective_schema(_BASE, [(template, ["x", "y"], {})])
    tag = effective["properties"]["x"]["properties"]["y"]["properties"]["tag"]
    assert tag == {"$ref": "#/properties/x/properties/y/$defs/t"}
    _validate_schema(effective)
    assert _paths(effective, {"x": {"y": {"tag": 1}}}) == ["$.x.y.tag"]


def test_compose_rebases_refs_escaping_the_path_segment() -> None:
    template = validate_template(_doc(name="m", schema=_RECURSIVE_FRAGMENT))
    effective = compose_effective_schema(_BASE, [(template, ["a/b c"], {})])
    assert effective["properties"]["a/b c"]["properties"]["tag"] == {"$ref": "#/properties/a~1b%20c/$defs/t"}
    _validate_schema(effective)
    assert _paths(effective, {"a/b c": {"tag": 1}}) == ["$['a/b c'].tag"]


_ANCHOR_FRAGMENT = {
    "type": "object",
    "properties": {"tag": {"$ref": "#t"}},
    "$defs": {"t": {"$anchor": "t", "type": "string"}},
}


def test_compose_leaves_anchor_refs_untouched_and_resolvable() -> None:
    template = validate_template(_doc(name="m", schema=_ANCHOR_FRAGMENT))
    effective = compose_effective_schema(_BASE, [(template, ["box"], {})])
    assert effective["properties"]["box"]["properties"]["tag"] == {"$ref": "#t"}
    _validate_schema(effective)
    assert _paths(effective, {"box": {"tag": 1}}) == ["$.box.tag"]


def test_compose_refuses_a_duplicate_anchor_across_attachments() -> None:
    first = validate_template(_doc(name="a", schema=_ANCHOR_FRAGMENT))
    second = validate_template(_doc(name="b", schema=_ANCHOR_FRAGMENT))
    with pytest.raises(SchemaValidationError, match="declared more than once"):
        compose_effective_schema(_BASE, [(first, ["a"], {}), (second, ["b"], {})])


def test_compose_refuses_a_fragment_id_placed_below_the_root() -> None:
    template = validate_template(
        _doc(name="m", schema={"$id": "urn:example:box", "type": "object", "properties": {"tag": {"type": "string"}}})
    )
    with pytest.raises(SchemaValidationError, match="below the root is not allowed: the schema is one resource"):
        compose_effective_schema(_BASE, [(template, ["box"], {})])


def test_compose_leaves_ref_shaped_data_untouched() -> None:
    fragment = {"type": "object", "properties": {"link": {"const": {"$ref": "#"}}}}
    template = validate_template(_doc(name="m", schema=fragment))
    effective = compose_effective_schema(_BASE, [(template, ["box"], {})])
    assert effective["properties"]["box"]["properties"]["link"] == {"const": {"$ref": "#"}}
    assert _paths(effective, {"box": {"link": {"$ref": "#"}}}) == []


def test_compose_refuses_an_anchor_name_shared_with_a_dynamic_anchor_across_attachments() -> None:
    first = validate_template(_doc(name="a", schema=_ANCHOR_FRAGMENT))
    second = validate_template(
        _doc(
            name="b",
            schema={
                "type": "object",
                "properties": {"tag": {"$ref": "#t"}},
                "$defs": {"t": {"$dynamicAnchor": "t", "type": "integer"}},
            },
        )
    )
    for order in ([(first, ["a"], {}), (second, ["b"], {})], [(second, ["b"], {}), (first, ["a"], {})]):
        with pytest.raises(SchemaValidationError, match="declared more than once"):
            compose_effective_schema(_BASE, order)


@pytest.mark.parametrize("ref", ["#", "#/properties", "#/$defs"])
def test_compose_root_attach_refuses_a_ref_that_cannot_keep_its_meaning(ref: str) -> None:
    template = validate_template(
        _doc(
            name="m",
            schema={
                "type": "object",
                "properties": {"tag": {"type": "string"}, "child": {"$ref": ref}},
                "required": ["tag"],
                "$defs": {"t": {"type": "string"}},
            },
        )
    )
    base = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    with pytest.raises(AttachConflictError, match=re.escape(f"carries $ref '{ref}'") + ".*attach it at a path"):
        compose_effective_schema(base, [(template, [], {})])


def test_compose_root_attach_keeps_property_and_defs_refs() -> None:
    template = validate_template(
        _doc(
            name="m",
            schema={
                "type": "object",
                "properties": {"tag": {"$ref": "#/$defs/t"}, "copy": {"$ref": "#/properties/tag"}},
                "$defs": {"t": {"type": "string"}},
            },
        )
    )
    effective = compose_effective_schema(_BASE, [(template, [], {})])
    assert _paths(effective, {"tag": 1, "copy": 2}) == ["$.copy", "$.tag"]


_POINTER_TARGET_FRAGMENT = {
    "type": "object",
    "properties": {"a": {"$ref": "#/x/t"}},
    "x": {"t": {"properties": {"c": {"$ref": "#"}}}},
}


def test_compose_rebases_refs_inside_a_pointer_target() -> None:
    template = validate_template(_doc(name="m", schema=_POINTER_TARGET_FRAGMENT))
    effective = compose_effective_schema(_BASE, [(template, ["box"], {})])
    box = effective["properties"]["box"]
    assert box["properties"]["a"] == {"$ref": "#/properties/box/x/t"}
    assert box["x"]["t"]["properties"]["c"] == {"$ref": "#/properties/box"}
    # ``c`` is the fragment's root again, never the state's: a base field name inside it is free.
    assert _paths(effective, {"box": {"a": {"c": {"n": "s"}}}}) == []


def test_compose_root_attach_refuses_a_root_ref_inside_a_pointer_target() -> None:
    fragment = {
        "type": "object",
        "properties": {"a": {"$ref": "#/properties/a/x/t", "x": {"t": {"properties": {"c": {"$ref": "#"}}}}}},
    }
    template = validate_template(_doc(name="m", schema=fragment))
    with pytest.raises(AttachConflictError, match=re.escape("carries $ref '#'")):
        compose_effective_schema(_BASE, [(template, [], {})])


def test_compose_refuses_a_fragment_ref_cycle() -> None:
    fragment = {"type": "object", "properties": {"v": {"$ref": "#/$defs/x"}}, "$defs": {"x": {"$ref": "#/$defs/x"}}}
    template = validate_template(_doc(name="m", schema={"type": "object", "properties": {"v": {"type": "string"}}}))
    # Built past the upload check, so the composer itself is what refuses the cycle.
    cyclic = dataclasses.replace(template, schema=fragment)
    with pytest.raises(SchemaValidationError, match="cycle"):
        compose_effective_schema(_BASE, [(cyclic, ["box"], {})])


def test_compose_checks_a_large_fragment_a_bounded_number_of_times(monkeypatch: pytest.MonkeyPatch) -> None:
    big = {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(200)}}
    fragment = {
        "type": "object",
        "properties": {f"p{i}": {"$ref": "#/$defs/big"} for i in range(300)},
        "$defs": {"big": big},
    }
    template = validate_template(_doc(name="m", schema=fragment))
    calls: list[str] = []
    original = Draft202012Validator.check_schema.__func__  # type: ignore[attr-defined]

    def counting(cls, schema, format_checker=None):
        calls.append("fragment")
        return original(cls, schema)

    def rules(schema, validator, *, subject):
        calls.append("composed")
        return evaluated_subschemas(schema, validator, subject=subject)

    monkeypatch.setattr(Draft202012Validator, "check_schema", classmethod(counting))
    monkeypatch.setattr(states_schema, "evaluated_subschemas", rules)
    compose_effective_schema(_BASE, [(template, ["box"], {})])
    assert calls == ["fragment", "composed"]


def test_compose_root_attach_refuses_a_root_type_its_parameters_make_non_object() -> None:
    template = validate_template(
        _doc(
            name="m",
            schema={"type": {"$parameter": "t"}, "properties": {"a": {"type": "string"}}, "required": ["a"]},
            parameters={"t": {"schema": {"type": "string"}, "default": "object"}},
        )
    )
    with pytest.raises(AttachConflictError, match="template 'm' at the root has root type 'array'"):
        compose_effective_schema(_BASE, [(template, [], {"t": "array"})])


def test_validate_template_refuses_a_fragment_in_another_schema_dialect() -> None:
    with pytest.raises(TemplateValidationError, match="draft 2020-12 document"):
        validate_template(
            _doc(
                name="m",
                schema={
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                },
            )
        )


@pytest.mark.parametrize("keyword", ["enum", "const"])
def test_compose_refuses_rebasing_a_ref_inside_a_const_or_enum_value(keyword: str) -> None:
    value = {"$ref": "#/$defs/s"}
    target = "#/properties/k/enum/0" if keyword == "enum" else "#/properties/k/const"
    fragment = {
        "type": "object",
        "properties": {"t": {"$ref": target}, "k": {keyword: [value] if keyword == "enum" else value}},
        "$defs": {"s": {"type": "string"}},
    }
    template = validate_template(_doc(name="m", schema=fragment))
    with pytest.raises(SchemaValidationError, match=re.escape("template 'm'") + ".*" + re.escape("'#/$defs/s'")):
        compose_effective_schema(_BASE, [(template, ["box"], {})])


def test_compose_root_attach_merges_defs_and_drops_annotations() -> None:
    template = validate_template(
        _doc(
            name="m",
            schema={
                "type": "object",
                "description": "a root fragment",
                "properties": {"tag": {"$ref": "#/$defs/t"}},
                "required": ["tag"],
                "$defs": {"t": {"type": "string"}},
            },
        )
    )
    effective = compose_effective_schema(_BASE, [(template, [], {})])
    assert effective == {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "tag": {"$ref": "#/$defs/t"}},
        "required": ["tag"],
        "$defs": {"t": {"type": "string"}},
    }
    assert _paths(effective, {"tag": 1}) == ["$.tag"]


def test_compose_root_attach_refuses_a_colliding_defs_key() -> None:
    base = {**_BASE, "$defs": {"t": {"type": "integer"}}}
    template = validate_template(
        _doc(
            name="m",
            schema={"type": "object", "properties": {"tag": {"$ref": "#/$defs/t"}}, "$defs": {"t": {"type": "string"}}},
        )
    )
    with pytest.raises(AttachConflictError, match="'m' at the root collides with existing \\$defs key 't'"):
        compose_effective_schema(base, [(template, [], {})])


@pytest.mark.parametrize(("keyword", "value"), [("allOf", [{"required": ["tag"]}]), ("additionalProperties", False)])
def test_compose_root_attach_refuses_a_root_keyword_it_cannot_merge(keyword: str, value: object) -> None:
    template = validate_template(
        _doc(name="m", schema={"type": "object", "properties": {"tag": {"type": "string"}}, keyword: value})
    )
    with pytest.raises(AttachConflictError, match=f"carries '{keyword}' at its root"):
        compose_effective_schema(_BASE, [(template, [], {})])


_D2020 = "https://json-schema.org/draft/2020-12/schema"


def _dialect_template() -> StateTemplate:
    return validate_template(
        _doc(
            name="m",
            schema={
                "$schema": {"$parameter": "d"},
                "type": "object",
                "properties": {"a": {"type": "object", "dependencies": {"x": ["y"]}}},
            },
            parameters={"d": {"schema": {"type": "string"}, "default": _D2020}},
        )
    )


def test_compose_root_attach_refuses_a_root_schema_dialect_its_parameters_set() -> None:
    draft_07 = "http://json-schema.org/draft-07/schema#"
    with pytest.raises(
        SchemaValidationError, match=re.escape("template 'm'") + ".*" + re.escape(f"$schema {draft_07!r}")
    ):
        compose_effective_schema(_BASE, [(_dialect_template(), [], {"d": draft_07})])


@pytest.mark.parametrize("dialect", [_D2020, _D2020 + "#"])
def test_compose_root_attach_drops_a_draft_2020_12_root_schema(dialect: str) -> None:
    effective = compose_effective_schema(_BASE, [(_dialect_template(), [], {"d": dialect})])
    assert "$schema" not in effective
    assert _paths(effective, {"a": {"x": 1}}) == []  # ``dependencies`` is not a 2020-12 keyword


def test_compose_injects_trace_under_tracing_mount() -> None:
    base = {"type": "object", "properties": {}}
    template = validate_template(
        _doc(name="m", schema={"type": "object", "properties": {"y": {"type": "integer"}}}, trace={"enabled": True})
    )
    effective = compose_effective_schema(base, [(template, ["sub"], {})])
    sub = effective["properties"]["sub"]
    assert "_trace" in sub["properties"]
    # ``meta`` is a nullable object: a writer with no provenance bag (a hook/schedule/api
    # door, a builtin ``state_*`` tool) stamps ``meta=None``, while ``at`` is always a string.
    assert sub["properties"]["_trace"]["properties"]["meta"] == {"type": ["object", "null"]}
    assert sub["properties"]["_trace"]["properties"]["at"] == {"type": "string"}


def test_regime_for_longest_match() -> None:
    template = validate_template(
        _doc(
            schema={
                "type": "object",
                "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}}}},
            },
            regimes=[{"path": ["a"], "regime": "composing"}, {"path": ["a", "b"], "regime": "single"}],
        )
    )
    assert regime_for(template, ["a", "b"]) == "single"
    assert regime_for(template, ["a"]) == "composing"
    assert regime_for(template, ["z"]) == "free"


# --------------------------------------------------------------------------- #
# template_jq — input programs                                                  #
# --------------------------------------------------------------------------- #
def _planner_doc(**over):
    doc = _doc(
        schema={
            "type": "object",
            "properties": {
                "ledger": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}},
                "phases": {"type": "array", "items": {"type": "object", "properties": {"key": {"type": "string"}}}},
            },
        },
        regimes=[{"path": ["ledger"], "regime": "composing"}, {"path": ["phases"], "regime": "single"}],
    )
    doc.update(over)
    return doc


def test_input_programs_parse_with_params_and_description() -> None:
    template = validate_template(
        _planner_doc(
            template_jq={
                "anything_due": {
                    "purpose": "input",
                    "description": "any work due",
                    "jq": {"content": "(.ledger // []) | length > 0"},
                },
                "due_set": {
                    "purpose": "input",
                    "description": "the work due",
                    "params": ["run"],
                    "jq": {"content": "[(.ledger // [])[] | {id, run: $params.run}]"},
                },
            }
        )
    )
    assert set(template.template_jq) == {"anything_due", "due_set"}
    assert template.template_jq["due_set"].purpose == "input"
    assert template.template_jq["due_set"].params == ["run"]
    assert template.template_jq["due_set"].description == "the work due"
    assert validate_template(template.to_document()).template_jq == template.template_jq


def test_input_declared_params_ride_the_single_params_object() -> None:
    # Decision 3: a declared param arrives as ``$params.<key>``, never a bare ``$<name>``.
    # A body reading the bare ``$run`` references an undeclared variable and is refused.
    with pytest.raises(TemplateValidationError, match="not a valid jq"):
        validate_template(
            _planner_doc(template_jq={"v": {"purpose": "input", "params": ["run"], "jq": {"content": "{run: $run}"}}})
        )


def test_input_program_may_call_a_sibling_by_name_in_dependency_order() -> None:
    # ``due_set`` calls the sibling ``tjq_anything_due({})``; the compile prelude emits the
    # sibling def first, so the reference resolves.
    template = validate_template(
        _planner_doc(
            template_jq={
                "anything_due": {"purpose": "input", "jq": {"content": "(.ledger // []) | length > 0"}},
                "due_set": {
                    "purpose": "input",
                    "jq": {"content": "if tjq_anything_due({}) then (.ledger // []) else [] end"},
                },
            }
        )
    )
    assert set(template.template_jq) == {"anything_due", "due_set"}


def test_input_program_reference_cycle_is_a_loud_refusal() -> None:
    with pytest.raises(TemplateValidationError, match="not a valid jq"):
        validate_template(
            _planner_doc(
                template_jq={
                    "a": {"purpose": "input", "jq": {"content": "tjq_b({})"}},
                    "b": {"purpose": "input", "jq": {"content": "tjq_a({})"}},
                }
            )
        )


def test_non_compiling_program_jq_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq 'bad' jq is not a valid jq"):
        validate_template(_planner_doc(template_jq={"bad": {"purpose": "input", "jq": {"content": "this is (not jq"}}}))


def test_program_using_parameters_and_declarations_variables_compiles() -> None:
    template = validate_template(
        _planner_doc(
            declarations={"schema": {"type": "object"}},
            template_jq={"scoped": {"purpose": "input", "jq": {"content": "$parameters + $declarations | .ledger"}}},
        )
    )
    assert "scoped" in template.template_jq


def test_program_bad_name_or_param_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq name"):
        validate_template(_planner_doc(template_jq={"Bad-Name": {"purpose": "input", "jq": {"content": "."}}}))
    with pytest.raises(TemplateValidationError, match="params"):
        validate_template(
            _planner_doc(template_jq={"v": {"purpose": "input", "jq": {"content": "."}, "params": ["Bad Param"]}})
        )


def test_bad_or_missing_purpose_refused() -> None:
    with pytest.raises(TemplateValidationError, match="purpose"):
        validate_template(_planner_doc(template_jq={"v": {"jq": {"content": "."}}}))
    with pytest.raises(TemplateValidationError, match="purpose"):
        validate_template(_planner_doc(template_jq={"v": {"purpose": "verdict", "jq": {"content": "."}}}))


def test_purpose_specific_keys_refused() -> None:
    # An ``input`` program may not declare ``reads``/``writes``; ``params`` is admitted on
    # BOTH purposes (an update's ``params`` names its ``$input`` keys). A key outside a
    # purpose's set is refused.
    with pytest.raises(TemplateValidationError, match="declares no reads or writes"):
        validate_template(
            _planner_doc(template_jq={"v": {"purpose": "input", "writes": [["ledger"]], "jq": {"content": "."}}})
        )
    with pytest.raises(TemplateValidationError, match="reads_x\n  Extra inputs are not permitted"):
        validate_template(
            _planner_doc(template_jq={"u": {"purpose": "update", "reads_x": [["a"]], "jq": {"content": "[]"}}})
        )


def test_update_may_declare_params_naming_its_input_keys() -> None:
    template = validate_template(
        _planner_doc(
            template_jq={
                "outcome": {
                    "purpose": "update",
                    "params": ["verdict"],
                    "writes": [["ledger"]],
                    "jq": {"content": '[{op: "set", path: ["ledger"], value: $input.verdict}]'},
                }
            }
        )
    )
    assert template.template_jq["outcome"].params == ["verdict"]
    # to_document round-trips update params.
    assert validate_template(template.to_document()).template_jq == template.template_jq


# --------------------------------------------------------------------------- #
# template_jq — update programs                                                 #
# --------------------------------------------------------------------------- #
def test_update_programs_parse_reads_and_writes() -> None:
    template = validate_template(
        _planner_doc(
            template_jq={
                "outcome": {
                    "purpose": "update",
                    "description": "settle",
                    "reads": [["ledger"]],
                    "writes": [["ledger"]],
                    "jq": {"content": '[{op: "set", path: ["ledger"], value: $input}]'},
                },
            }
        )
    )
    assert template.template_jq["outcome"].purpose == "update"
    assert template.template_jq["outcome"].writes == [["ledger"]]
    assert validate_template(template.to_document()).template_jq == template.template_jq


def test_update_writing_outside_the_fragment_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq 'bad'"):
        validate_template(
            _planner_doc(template_jq={"bad": {"purpose": "update", "writes": [["nonesuch"]], "jq": {"content": "[]"}}})
        )


def test_update_may_write_any_regime_including_single() -> None:
    # An update program may write a ``single``-regime path (``phases``); the runtime apply
    # enforces the single-writer rule, not validation.
    template = validate_template(
        _planner_doc(
            template_jq={
                "seed": {
                    "purpose": "update",
                    "writes": [["phases"]],
                    "jq": {"content": '[{op: "set", path: ["phases"], value: []}]'},
                }
            }
        )
    )
    assert template.template_jq["seed"].writes == [["phases"]]


def test_non_compiling_update_jq_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq 'bad' jq is not a valid jq"):
        validate_template(
            _planner_doc(
                template_jq={"bad": {"purpose": "update", "writes": [["ledger"]], "jq": {"content": "this is (not jq"}}}
            )
        )


def test_update_may_call_an_input_program_by_name() -> None:
    # An update program's jq may call an input program by its relative name; the compile
    # prepends the sibling input-program prelude exactly as the evaluator does, so a
    # reference over the record subtree (its ``.``) resolves.
    template = validate_template(
        _planner_doc(
            template_jq={
                "anything_due": {"purpose": "input", "jq": {"content": "(.ledger // []) | length > 0"}},
                "act": {
                    "purpose": "update",
                    "writes": [["ledger"]],
                    "jq": {
                        "content": (
                            'if (. | tjq_anything_due({})) then [{op: "set", path: ["ledger"], value: []}] else [] end'
                        )
                    },
                },
            }
        )
    )
    assert template.template_jq["act"].purpose == "update"
    assert template.template_jq["anything_due"].purpose == "input"


# --------------------------------------------------------------------------- #
# reconcile                                                                     #
# --------------------------------------------------------------------------- #
def test_reconcile_parses_three_jq_programs() -> None:
    template = validate_template(
        _planner_doc(
            reconcile={
                "orphans": {"content": "[.ledger[]? | {id, label: .id}]"},
                "resolutions": {"content": "[.resolutions[]?]"},
                "close": {"content": '[{op: "remove", path: ["ledger"], keys: [$id]}]'},
            }
        )
    )
    assert template.reconcile is not None
    assert template.reconcile.orphans.content is not None
    assert template.reconcile.orphans.content.startswith("[.ledger")
    assert validate_template(template.to_document()).reconcile == template.reconcile


def test_reconcile_missing_a_program_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match=r"reconcile\.close\n  Field required"):
        validate_template(_planner_doc(reconcile={"orphans": {"content": "."}, "resolutions": {"content": "."}}))


def test_reconcile_non_compiling_jq_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match="reconcile orphans is not a valid jq"):
        validate_template(
            _planner_doc(
                reconcile={
                    "orphans": {"content": "this is (not jq"},
                    "close": {"content": "[]"},
                    "resolutions": {"content": "[]"},
                }
            )
        )


def test_empty_or_badly_named_members_are_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq 'v' jq must be a non-empty"):
        validate_template(_planner_doc(template_jq={"v": {"purpose": "input", "jq": {"content": "  "}}}))
    with pytest.raises(TemplateValidationError, match="template_jq name"):
        validate_template(_planner_doc(template_jq={"Bad-Rule": {"purpose": "update", "jq": {"content": "[]"}}}))
    with pytest.raises(TemplateValidationError, match="template_jq 'r' jq must be a non-empty"):
        validate_template(_planner_doc(template_jq={"r": {"purpose": "update", "jq": {"content": ""}}}))
    with pytest.raises(TemplateValidationError, match="reconcile close must be a non-empty"):
        validate_template(
            _planner_doc(
                reconcile={"orphans": {"content": "."}, "close": {"content": "  "}, "resolutions": {"content": "."}}
            )
        )


# --------------------------------------------------------------------------- #
# by-id program bodies (a stored resource holds the jq)                         #
# --------------------------------------------------------------------------- #
def test_template_jq_by_id_is_accepted_and_round_trips() -> None:
    # A by-id program body is not compiled by the pure validator (it cannot fetch the resource);
    # the save door renders and compiles it. Validate accepts the shape and ``to_document`` emits
    # the nested templated text verbatim, so the by-id reference round-trips.
    doc = _planner_doc(
        template_jq={
            "any_due": {"purpose": "input", "jq": {"id": "stored-any-due", "kwargs": {"limit": 3}}},
        }
    )
    template = validate_template(doc)
    assert template.template_jq["any_due"].jq == TemplatedText(id="stored-any-due", kwargs={"limit": 3})
    assert template.to_document()["template_jq"]["any_due"]["jq"] == {"id": "stored-any-due", "kwargs": {"limit": 3}}
    assert validate_template(template.to_document()).template_jq == template.template_jq


def test_reconcile_by_id_is_accepted_and_round_trips() -> None:
    doc = _planner_doc(
        reconcile={
            "orphans": {"id": "stored-orphans"},
            "resolutions": {"content": "[.resolutions[]?]"},
            "close": {"id": "stored-close"},
        }
    )
    template = validate_template(doc)
    assert template.reconcile is not None
    assert template.reconcile.orphans == TemplatedText(id="stored-orphans")
    assert template.reconcile.close == TemplatedText(id="stored-close")
    assert template.to_document()["reconcile"]["orphans"] == {"id": "stored-orphans"}
    assert validate_template(template.to_document()).reconcile == template.reconcile


def test_program_body_with_a_stray_key_is_refused() -> None:
    # A stray key inside the nested templated-text value is refused by the value type.
    with pytest.raises(TemplateValidationError, match=r"template_jq\.v\.jq\.extra\n  Extra inputs are not permitted"):
        validate_template(_planner_doc(template_jq={"v": {"purpose": "input", "jq": {"content": ".", "extra": 1}}}))
    with pytest.raises(TemplateValidationError, match=r"reconcile\.orphans\.extra\n  Extra inputs are not permitted"):
        validate_template(
            _planner_doc(
                reconcile={
                    "orphans": {"content": ".", "extra": 1},
                    "close": {"content": "."},
                    "resolutions": {"content": "."},
                }
            )
        )
