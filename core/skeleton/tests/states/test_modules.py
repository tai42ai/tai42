"""The platform state-module document: validation of the platform keys, the refusal of
any key outside them, and the pure compose/regime transforms."""

from __future__ import annotations

import pytest
from tai42_contract.states.errors import ModuleValidationError, MountConflictError

from tai42_skeleton.states.modules import (
    StateModule,
    compose_effective_schema,
    regime_for,
    substitute_parameters,
    validate_module,
)


def _doc(**over):
    doc = {
        "kind": "state-module",
        "name": "demo",
        "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
    }
    doc.update(over)
    return doc


def test_validate_minimal_module() -> None:
    module = validate_module(_doc())
    assert isinstance(module, StateModule)
    assert module.name == "demo"
    assert module.trace.enabled is False
    assert module.declarations is None
    # round-trips through to_document
    assert validate_module(module.to_document()).name == "demo"


def test_wrong_kind_refused() -> None:
    with pytest.raises(ModuleValidationError, match="module kind"):
        validate_module(_doc(kind="flow"))


def test_bad_name_refused() -> None:
    with pytest.raises(ModuleValidationError, match="module name"):
        validate_module(_doc(name="Not A Name"))


@pytest.mark.parametrize("key", ["views", "loop", "bogus", "widgets"])
def test_unknown_key_refused_plainly(key: str) -> None:
    # A consumer keeps its own keys in its own sibling document; any key outside the
    # platform set is refused, naming what a state-module document holds.
    with pytest.raises(ModuleValidationError, match="unknown key"):
        validate_module(_doc(**{key: {}}))


def test_parameters_and_defaults() -> None:
    # The marker sits at a sub-schema position, so its default is a JSON-Schema fragment.
    doc = _doc(
        schema={"type": "object", "properties": {"cap": {"$parameter": "cap"}}},
        parameters={"cap": {"schema": {"type": "object"}, "default": {"type": "integer"}}},
    )
    module = validate_module(doc)
    assert module.defaults() == {"cap": {"type": "integer"}}


def test_no_default_parameter_must_appear_as_marker() -> None:
    doc = _doc(
        schema={"type": "object", "properties": {"x": {"type": "string"}}},
        parameters={"cap": {"schema": {"type": "integer"}}},
    )
    with pytest.raises(ModuleValidationError, match=r"must appear as a \$parameter marker"):
        validate_module(doc)


def test_schema_references_undeclared_parameter() -> None:
    doc = _doc(schema={"type": "object", "properties": {"c": {"$parameter": "cap"}}})
    with pytest.raises(ModuleValidationError, match="undeclared parameter"):
        validate_module(doc)


def test_regime_valid_and_invalid_path() -> None:
    ok = _doc(
        schema={"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}},
        regimes=[{"path": ["items"], "regime": "composing"}],
    )
    module = validate_module(ok)
    assert module.regimes[0].regime == "composing"
    bad = _doc(regimes=[{"path": ["nope"], "regime": "single"}])
    with pytest.raises(ModuleValidationError, match="not a property of the fragment"):
        validate_module(bad)


def test_regime_wildcard_needs_array() -> None:
    bad = _doc(
        schema={"type": "object", "properties": {"x": {"type": "string"}}},
        regimes=[{"path": ["x", "*"], "regime": "composing"}],
    )
    with pytest.raises(ModuleValidationError):
        validate_module(bad)


def test_declarations_check_compiles() -> None:
    ok = _doc(declarations={"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}, "check": ".n > 0"})
    module = validate_module(ok)
    assert module.declarations is not None
    assert module.declarations.check == ".n > 0"
    bad = _doc(declarations={"schema": {"type": "object"}, "check": "this is (not jq"})
    with pytest.raises(ModuleValidationError, match="not a valid jq"):
        validate_module(bad)


def test_trace_enabled() -> None:
    module = validate_module(_doc(trace={"enabled": True}))
    assert module.trace.enabled is True
    assert module.to_document()["trace"] == {"enabled": True}


def test_fragment_must_be_object_schema() -> None:
    with pytest.raises(ModuleValidationError, match="valid object schema"):
        validate_module(_doc(schema={"type": "string"}))


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
    module = validate_module(_doc(name="m", schema={"type": "object", "properties": {"y": {"type": "integer"}}}))
    effective = compose_effective_schema(base, [(module, ["sub"], {})])
    assert effective["properties"]["top"] == {"type": "string"}
    assert effective["properties"]["sub"]["properties"]["y"] == {"type": "integer"}


def test_compose_effective_schema_collision_refused() -> None:
    base = {"type": "object", "properties": {"sub": {"type": "string"}}}
    module = validate_module(_doc(name="m"))
    with pytest.raises(MountConflictError, match="collides"):
        compose_effective_schema(base, [(module, ["sub"], {})])


def test_compose_effective_schema_overlap_refused() -> None:
    module = validate_module(_doc(name="m"))
    base = {"type": "object", "properties": {}}
    with pytest.raises(MountConflictError, match="overlaps"):
        compose_effective_schema(base, [(module, ["a"], {}), (module, ["a", "b"], {})])


def test_compose_injects_trace_under_tracing_mount() -> None:
    base = {"type": "object", "properties": {}}
    module = validate_module(
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
    module = validate_module(
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
