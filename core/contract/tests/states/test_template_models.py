"""The typed state-template sections, the rendered-template models and ``path_overlaps``.

The section models mirror the grammar the platform's template validator accepts: a parameter is
``{schema[, default]}``, a regime rule is ``{path, regime}`` with non-empty string segments, the
trace switch is ``{enabled: bool}``; every other key or type is refused.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tai42_contract.states import (
    RenderedAttachment,
    RenderedStateTemplate,
    RenderedTemplateDeclarations,
    RenderedTemplateJq,
    ResolvedTemplateJq,
    StateDeclaration,
    StateRegimeRule,
    StateTemplateDocument,
    StateTemplateParameter,
    StateTemplateTrace,
    StateUnitClosedError,
    path_overlaps,
)


@pytest.mark.parametrize(
    "raw",
    [
        {"schema": {"type": "string"}},
        {"schema": {"type": "string"}, "default": "x"},
        {"schema": {}, "default": None},
        {"schema": {"type": "integer"}, "default": 3},
    ],
)
def test_parameter_accepts(raw: dict[str, Any]) -> None:
    StateTemplateParameter.model_validate(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"default": 1},
        {"schema": "string"},
        {"schema": None},
        {"schema": [1]},
        {"schema": {}, "extra": 1},
        "x",
        None,
    ],
)
def test_parameter_refuses(raw: Any) -> None:
    with pytest.raises(ValidationError):
        StateTemplateParameter.model_validate(raw)


def test_parameter_has_default_for_an_explicit_null() -> None:
    param = StateTemplateParameter.model_validate({"schema": {}, "default": None})
    assert param.has_default is True
    assert param.model_dump() == {"schema": {}, "default": None}


def test_parameter_without_default_serializes_schema_only() -> None:
    param = StateTemplateParameter.model_validate({"schema": {"type": "string"}})
    assert param.has_default is False
    assert param.model_dump() == {"schema": {"type": "string"}}
    assert param.model_dump(mode="json") == {"schema": {"type": "string"}}


@pytest.mark.parametrize(
    "raw",
    [
        {"path": [], "regime": "single"},
        {"path": ["a", "*", "b"], "regime": "composing"},
        {"path": ["x"], "regime": "free"},
    ],
)
def test_regime_rule_accepts(raw: dict[str, Any]) -> None:
    rule = StateRegimeRule.model_validate(raw)
    assert rule.model_dump() == raw


@pytest.mark.parametrize(
    "raw",
    [
        {"path": ["a"]},
        {"regime": "single"},
        {"path": ["a"], "regime": "other"},
        {"path": [""], "regime": "single"},
        {"path": [1], "regime": "single"},
        {"path": "a", "regime": "single"},
        {"path": ["a"], "regime": "single", "extra": True},
    ],
)
def test_regime_rule_refuses(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StateRegimeRule.model_validate(raw)


def test_trace_defaults_off_and_accepts_a_bool() -> None:
    assert StateTemplateTrace().enabled is False
    assert StateTemplateTrace.model_validate({"enabled": True}).enabled is True
    assert StateTemplateTrace.model_validate({}).model_dump() == {"enabled": False}


@pytest.mark.parametrize("raw", [{"enabled": "true"}, {"enabled": 1}, {"enabled": None}, {"other": True}])
def test_trace_refuses_a_non_bool_or_an_unknown_key(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StateTemplateTrace.model_validate(raw)


def test_document_types_its_sections() -> None:
    doc = StateTemplateDocument.model_validate(
        {
            "kind": "state-template",
            "name": "probe",
            "parameters": {"p": {"schema": {"type": "string"}, "default": "d"}},
            "schema": {"type": "object"},
            "regimes": [{"path": ["a"], "regime": "single"}],
            "trace": {"enabled": True},
        }
    )
    assert isinstance(doc.parameters["p"], StateTemplateParameter)
    assert doc.regimes == [StateRegimeRule(path=["a"], regime="single")]
    assert doc.trace == StateTemplateTrace(enabled=True)
    dumped = doc.model_dump(by_alias=True, exclude_none=True)
    assert dumped["parameters"] == {"p": {"schema": {"type": "string"}, "default": "d"}}
    assert dumped["regimes"] == [{"path": ["a"], "regime": "single"}]
    assert dumped["trace"] == {"enabled": True}


def test_document_section_defaults() -> None:
    doc = StateTemplateDocument.model_validate({"name": "probe"})
    assert doc.parameters == {}
    assert doc.regimes == []
    assert doc.trace == StateTemplateTrace()


@pytest.mark.parametrize(
    "section",
    [
        {"parameters": {"p": {"schema": {}, "bogus": 1}}},
        {"regimes": [{"path": ["a"], "regime": "nope"}]},
        {"trace": {"enabled": "yes"}},
        {"parameters": None},
        {"regimes": None},
        {"trace": None},
    ],
)
def test_document_refuses_a_malformed_section(section: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StateTemplateDocument.model_validate({"name": "probe", **section})


def test_declaration_regimes_are_typed() -> None:
    decl = StateDeclaration.model_validate(
        {
            "name": "s",
            "subject_kinds": ["thread"],
            "default_subject_kind": "thread",
            "regimes": [{"path": ["a", "b"], "regime": "composing"}],
        }
    )
    assert decl.regimes == [StateRegimeRule(path=["a", "b"], regime="composing")]


def _rendered_template() -> RenderedStateTemplate:
    return RenderedStateTemplate(
        name="probe",
        description="",
        version="3:1.0",
        parameters={"p": StateTemplateParameter(schema={})},
        schema={"type": "object"},
        regimes=[],
        declarations=RenderedTemplateDeclarations(schema={"type": "object"}, check=". == {}"),
        trace=StateTemplateTrace(),
        template_jq={"a": RenderedTemplateJq(purpose="input", jq=".", params=[])},
        input_order=["a"],
    )


def test_rendered_models_round_trip_by_alias() -> None:
    template = _rendered_template()
    dumped = template.model_dump()
    assert dumped["schema"] == {"type": "object"}
    assert dumped["declarations"] == {"schema": {"type": "object"}, "check": ". == {}"}
    assert RenderedStateTemplate.model_validate(dumped) == template
    attachment = RenderedAttachment(
        state="s", template=template, path=["x"], parameters={}, declarations={}, version="2:3:1.0"
    )
    assert RenderedAttachment.model_validate(attachment.model_dump()) == attachment


def test_rendered_models_are_frozen_and_refuse_extra_keys() -> None:
    template = _rendered_template()
    with pytest.raises(ValidationError):
        template.name = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        RenderedTemplateJq.model_validate({"purpose": "input", "jq": ".", "params": [], "extra": 1})


def test_resolved_template_jq_round_trips_and_is_frozen() -> None:
    resolved = ResolvedTemplateJq(template="alpha", program="count", purpose="input", params=["v"], path=["alpha"])
    assert ResolvedTemplateJq.model_validate(resolved.model_dump()) == resolved
    with pytest.raises(ValidationError):
        resolved.program = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "raw",
    [
        {"template": "alpha", "program": "count", "purpose": "read", "params": [], "path": []},
        {"template": "alpha", "program": "count", "purpose": "input", "params": [], "path": [""]},
        {"template": "alpha", "program": "count", "purpose": "input", "params": [], "path": [], "extra": 1},
        {"template": "alpha", "purpose": "input", "params": [], "path": []},
    ],
)
def test_resolved_template_jq_refuses(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ResolvedTemplateJq.model_validate(raw)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ([], [], True),
        ([], ["a"], True),
        (["a"], ["a", "b"], True),
        (["a", "b"], ["a"], True),
        (["a", "b"], ["a", "c"], False),
        (["a", "*"], ["a", "x", "y"], True),
        (["*", "b"], ["a", "c"], False),
        (["a"], ["b"], False),
        (["a", 0], ["a", "*"], True),
    ],
)
def test_path_overlaps(a: list[Any], b: list[Any], expected: bool) -> None:
    assert path_overlaps(a, b) is expected
    assert path_overlaps(b, a) is expected


def test_unit_closed_error_is_a_runtime_error() -> None:
    assert issubclass(StateUnitClosedError, RuntimeError)
