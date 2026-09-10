"""The platform state-template document: validation of the platform keys, the refusal of
any key outside them, and the pure compose/regime transforms."""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import AttachConflictError, TemplateValidationError

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


def test_validate_minimal_module() -> None:
    module = validate_template(_doc())
    assert isinstance(module, StateTemplate)
    assert module.name == "demo"
    assert module.trace.enabled is False
    assert module.declarations is None
    # round-trips through to_document
    assert validate_template(module.to_document()).name == "demo"


def test_wrong_kind_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template kind"):
        validate_template(_doc(kind="flow"))


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
    # holds (template_jq/reconcile are IN the set; a flow concept like ``loop`` is not).
    with pytest.raises(TemplateValidationError, match="unknown key"):
        validate_template(_doc(**{key: {}}))


def test_parameters_and_defaults() -> None:
    # The marker sits at a sub-schema position, so its default is a JSON-Schema fragment.
    doc = _doc(
        schema={"type": "object", "properties": {"cap": {"$parameter": "cap"}}},
        parameters={"cap": {"schema": {"type": "object"}, "default": {"type": "integer"}}},
    )
    module = validate_template(doc)
    assert module.defaults() == {"cap": {"type": "integer"}}


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
    module = validate_template(ok)
    assert module.regimes[0].regime == "composing"
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
    ok = _doc(declarations={"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}, "check": ".n > 0"})
    module = validate_template(ok)
    assert module.declarations is not None
    assert module.declarations.check == ".n > 0"
    bad = _doc(declarations={"schema": {"type": "object"}, "check": "this is (not jq"})
    with pytest.raises(TemplateValidationError, match="not a valid jq"):
        validate_template(bad)


def test_trace_enabled() -> None:
    module = validate_template(_doc(trace={"enabled": True}))
    assert module.trace.enabled is True
    assert module.to_document()["trace"] == {"enabled": True}


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
    module = validate_template(_doc(name="m", schema={"type": "object", "properties": {"y": {"type": "integer"}}}))
    effective = compose_effective_schema(base, [(module, ["sub"], {})])
    assert effective["properties"]["top"] == {"type": "string"}
    assert effective["properties"]["sub"]["properties"]["y"] == {"type": "integer"}


def test_compose_effective_schema_collision_refused() -> None:
    base = {"type": "object", "properties": {"sub": {"type": "string"}}}
    module = validate_template(_doc(name="m"))
    with pytest.raises(AttachConflictError, match="collides"):
        compose_effective_schema(base, [(module, ["sub"], {})])


def test_compose_effective_schema_overlap_refused() -> None:
    module = validate_template(_doc(name="m"))
    base = {"type": "object", "properties": {}}
    with pytest.raises(AttachConflictError, match="overlaps"):
        compose_effective_schema(base, [(module, ["a"], {}), (module, ["a", "b"], {})])


def test_compose_injects_trace_under_tracing_mount() -> None:
    base = {"type": "object", "properties": {}}
    module = validate_template(
        _doc(name="m", schema={"type": "object", "properties": {"y": {"type": "integer"}}}, trace={"enabled": True})
    )
    effective = compose_effective_schema(base, [(module, ["sub"], {})])
    sub = effective["properties"]["sub"]
    assert "_trace" in sub["properties"]
    # ``meta`` is a nullable object: a writer with no provenance bag (a hook/schedule/api
    # door, a builtin ``state_*`` tool) stamps ``meta=None``, while ``at`` is always a string.
    assert sub["properties"]["_trace"]["properties"]["meta"] == {"type": ["object", "null"]}
    assert sub["properties"]["_trace"]["properties"]["at"] == {"type": "string"}


def test_regime_for_longest_match() -> None:
    module = validate_template(
        _doc(
            schema={
                "type": "object",
                "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}}}},
            },
            regimes=[{"path": ["a"], "regime": "composing"}, {"path": ["a", "b"], "regime": "single"}],
        )
    )
    assert regime_for(module, ["a", "b"]) == "single"
    assert regime_for(module, ["a"]) == "composing"
    assert regime_for(module, ["z"]) == "free"


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
    module = validate_template(
        _planner_doc(
            template_jq={
                "anything_due": {
                    "purpose": "input",
                    "description": "any work due",
                    "jq": "(.ledger // []) | length > 0",
                },
                "due_set": {
                    "purpose": "input",
                    "description": "the work due",
                    "params": ["run"],
                    "jq": "[(.ledger // [])[] | {id, run: $params.run}]",
                },
            }
        )
    )
    assert set(module.template_jq) == {"anything_due", "due_set"}
    assert module.template_jq["due_set"].purpose == "input"
    assert module.template_jq["due_set"].params == ["run"]
    assert module.template_jq["due_set"].description == "the work due"
    assert validate_template(module.to_document()).template_jq == module.template_jq


def test_input_declared_params_ride_the_single_params_object() -> None:
    # Decision 3: a declared param arrives as ``$params.<key>``, never a bare ``$<name>``.
    # A body reading the bare ``$run`` references an undeclared variable and is refused.
    with pytest.raises(TemplateValidationError, match="not a valid jq"):
        validate_template(_planner_doc(template_jq={"v": {"purpose": "input", "params": ["run"], "jq": "{run: $run}"}}))


def test_input_program_may_call_a_sibling_by_name_in_dependency_order() -> None:
    # ``due_set`` calls the sibling ``tjq_anything_due({})``; the compile prelude emits the
    # sibling def first, so the reference resolves.
    module = validate_template(
        _planner_doc(
            template_jq={
                "anything_due": {"purpose": "input", "jq": "(.ledger // []) | length > 0"},
                "due_set": {"purpose": "input", "jq": "if tjq_anything_due({}) then (.ledger // []) else [] end"},
            }
        )
    )
    assert set(module.template_jq) == {"anything_due", "due_set"}


def test_input_program_reference_cycle_is_a_loud_refusal() -> None:
    with pytest.raises(TemplateValidationError, match="not a valid jq"):
        validate_template(
            _planner_doc(
                template_jq={
                    "a": {"purpose": "input", "jq": "tjq_b({})"},
                    "b": {"purpose": "input", "jq": "tjq_a({})"},
                }
            )
        )


def test_non_compiling_program_jq_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq 'bad' jq is not a valid jq"):
        validate_template(_planner_doc(template_jq={"bad": {"purpose": "input", "jq": "this is (not jq"}}))


def test_program_using_parameters_and_declarations_variables_compiles() -> None:
    module = validate_template(
        _planner_doc(
            declarations={"schema": {"type": "object"}},
            template_jq={"scoped": {"purpose": "input", "jq": "$parameters + $declarations | .ledger"}},
        )
    )
    assert "scoped" in module.template_jq


def test_program_bad_name_or_param_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq name"):
        validate_template(_planner_doc(template_jq={"Bad-Name": {"purpose": "input", "jq": "."}}))
    with pytest.raises(TemplateValidationError, match="params"):
        validate_template(_planner_doc(template_jq={"v": {"purpose": "input", "jq": ".", "params": ["Bad Param"]}}))


def test_bad_or_missing_purpose_refused() -> None:
    with pytest.raises(TemplateValidationError, match="purpose"):
        validate_template(_planner_doc(template_jq={"v": {"jq": "."}}))
    with pytest.raises(TemplateValidationError, match="purpose"):
        validate_template(_planner_doc(template_jq={"v": {"purpose": "verdict", "jq": "."}}))


def test_purpose_specific_keys_refused() -> None:
    # An ``input`` program may not declare ``reads``/``writes``; ``params`` is admitted on
    # BOTH purposes (an update's ``params`` names its ``.input`` keys). A key outside a
    # purpose's set is refused.
    with pytest.raises(TemplateValidationError, match="unknown key"):
        validate_template(_planner_doc(template_jq={"v": {"purpose": "input", "writes": [["ledger"]], "jq": "."}}))
    with pytest.raises(TemplateValidationError, match="unknown key"):
        validate_template(_planner_doc(template_jq={"u": {"purpose": "update", "reads_x": [["a"]], "jq": "[]"}}))


def test_update_may_declare_params_naming_its_input_keys() -> None:
    module = validate_template(
        _planner_doc(
            template_jq={
                "outcome": {
                    "purpose": "update",
                    "params": ["verdict"],
                    "writes": [["ledger"]],
                    "jq": '[{op: "set", path: ["ledger"], value: .input.verdict}]',
                }
            }
        )
    )
    assert module.template_jq["outcome"].params == ["verdict"]
    # to_document round-trips update params.
    assert validate_template(module.to_document()).template_jq == module.template_jq


# --------------------------------------------------------------------------- #
# template_jq — update programs                                                 #
# --------------------------------------------------------------------------- #
def test_update_programs_parse_reads_and_writes() -> None:
    module = validate_template(
        _planner_doc(
            template_jq={
                "outcome": {
                    "purpose": "update",
                    "description": "settle",
                    "reads": [["ledger"]],
                    "writes": [["ledger"]],
                    "jq": '[{op: "set", path: ["ledger"], value: .input}]',
                },
            }
        )
    )
    assert module.template_jq["outcome"].purpose == "update"
    assert module.template_jq["outcome"].writes == [["ledger"]]
    assert validate_template(module.to_document()).template_jq == module.template_jq


def test_update_writing_outside_the_fragment_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq 'bad'"):
        validate_template(
            _planner_doc(template_jq={"bad": {"purpose": "update", "writes": [["nonesuch"]], "jq": "[]"}})
        )


def test_update_may_write_any_regime_including_single() -> None:
    # An update program may write a ``single``-regime path (``phases``); the runtime apply
    # enforces the single-writer rule, not validation.
    module = validate_template(
        _planner_doc(
            template_jq={
                "seed": {
                    "purpose": "update",
                    "writes": [["phases"]],
                    "jq": '[{op: "set", path: ["phases"], value: []}]',
                }
            }
        )
    )
    assert module.template_jq["seed"].writes == [["phases"]]


def test_non_compiling_update_jq_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq 'bad' jq is not a valid jq"):
        validate_template(
            _planner_doc(template_jq={"bad": {"purpose": "update", "writes": [["ledger"]], "jq": "this is (not jq"}})
        )


def test_update_may_call_an_input_program_by_name() -> None:
    # An update program's jq may call an input program by its relative name; the compile
    # prepends the sibling input-program prelude exactly as the evaluator does, so a
    # reference over ``.record`` resolves.
    module = validate_template(
        _planner_doc(
            template_jq={
                "anything_due": {"purpose": "input", "jq": "(.ledger // []) | length > 0"},
                "act": {
                    "purpose": "update",
                    "writes": [["ledger"]],
                    "jq": (
                        "if (.record | tjq_anything_due({})) then "
                        '[{op: "set", path: ["ledger"], value: []}] else [] end'
                    ),
                },
            }
        )
    )
    assert module.template_jq["act"].purpose == "update"
    assert module.template_jq["anything_due"].purpose == "input"


# --------------------------------------------------------------------------- #
# reconcile                                                                     #
# --------------------------------------------------------------------------- #
def test_reconcile_parses_three_jq_programs() -> None:
    module = validate_template(
        _planner_doc(
            reconcile={
                "view": "[.data.ledger[]? | {id, label: .id}]",
                "resolutions": "[.new.resolutions[]?]",
                "close": '[{op: "remove", path: ["ledger"], keys: [.id]}]',
            }
        )
    )
    assert module.reconcile is not None
    assert module.reconcile.view.startswith("[.data")
    assert validate_template(module.to_document()).reconcile == module.reconcile


def test_reconcile_missing_a_program_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match="reconcile close"):
        validate_template(_planner_doc(reconcile={"view": ".", "resolutions": "."}))


def test_reconcile_non_compiling_jq_is_refused() -> None:
    with pytest.raises(TemplateValidationError, match="reconcile view is not a valid jq"):
        validate_template(_planner_doc(reconcile={"view": "this is (not jq", "close": "[]", "resolutions": "[]"}))


def test_empty_or_badly_named_members_are_refused() -> None:
    with pytest.raises(TemplateValidationError, match="template_jq 'v' jq must be a non-empty"):
        validate_template(_planner_doc(template_jq={"v": {"purpose": "input", "jq": "  "}}))
    with pytest.raises(TemplateValidationError, match="template_jq name"):
        validate_template(_planner_doc(template_jq={"Bad-Rule": {"purpose": "update", "jq": "[]"}}))
    with pytest.raises(TemplateValidationError, match="template_jq 'r' jq must be a non-empty"):
        validate_template(_planner_doc(template_jq={"r": {"purpose": "update", "jq": ""}}))
    with pytest.raises(TemplateValidationError, match="reconcile close must be a non-empty"):
        validate_template(_planner_doc(reconcile={"view": ".", "close": "  ", "resolutions": "."}))
