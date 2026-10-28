"""Every jq-expression field the contract serves carries the ``x-tai42-expression`` annotation.

A schema-driven editor offers its jq editor only on a property that carries the vendor key, so a
jq field without it renders as a plain text box. These tests read the JSON schemas the contract
emits — per model and through the served document bundle — and assert the annotation sits on each
jq field with the facts of that field's own input document, variables and result.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import BaseModel

from tai42_contract.access_control import AccessCondition, AccessPolicy, RoleDefinition
from tai42_contract.conversations import ApiConversationRouteCreate, ChannelConversationRouteCreate
from tai42_contract.hooks import HOOK_DELIVERY_BLURB, HookRegister, HookSubject
from tai42_contract.interactions import (
    DoorCancelExpr,
    DoorExtrasExpr,
    DoorResumeExpr,
    DoorStartExpr,
    ParkableDoorMixin,
)
from tai42_contract.interactions.door_contract import PARKED_VARIABLE
from tai42_contract.presets import (
    PRESET_INPUT_MAP_ANNOTATION,
    PRESET_OUTPUT_MAP_ANNOTATION,
    PresetBody,
    PresetSeed,
)
from tai42_contract.schema_export.registry import build_document_schemas
from tai42_contract.states.models import (
    StateTemplateDeclarations,
    StateTemplateDocument,
    StateTemplateJq,
    StateTemplateReconcile,
)
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, TEMPLATED_TEXT_ANNOTATION_KEY

# Properties typed TemplatedText that hold something other than jq: a JSON Schema document.
_NON_JQ_TEMPLATED_PROPERTIES = frozenset({"schema", "input_schema", "output_schema"})


def _annotation(model: type[BaseModel], field: str) -> dict[str, Any]:
    return model.model_json_schema()["properties"][field][EXPRESSION_ANNOTATION_KEY]


def _variable_names(payload: dict[str, Any]) -> list[str]:
    return [variable["name"] for variable in payload.get("variables", [])]


def test_hook_subject_key_expr_reads_the_delivery_and_returns_the_subject_key() -> None:
    payload = _annotation(HookSubject, "key_expr")
    assert payload["language"] == "jq"
    assert payload["label"] == "subject key expression"
    assert payload["blurb"] == HOOK_DELIVERY_BLURB
    assert "non-empty string" in payload["returns"]
    assert "variables" not in payload


def test_hook_subject_annotation_reaches_the_registration_document() -> None:
    schema = HookRegister.model_json_schema()
    assert schema["$defs"]["HookSubject"]["properties"]["key_expr"][EXPRESSION_ANNOTATION_KEY] == _annotation(
        HookSubject, "key_expr"
    )


def test_declarations_check_reads_the_declarations_with_parameters() -> None:
    payload = _annotation(StateTemplateDeclarations, "check")
    assert payload["label"] == "declarations check"
    assert _variable_names(payload) == ["parameters"]
    assert "boolean true" in payload["returns"]


def test_template_jq_program_binds_both_purposes_variables() -> None:
    payload = _annotation(StateTemplateJq, "jq")
    assert payload["label"] == "template jq program"
    assert _variable_names(payload) == ["parameters", "declarations", "params", "input"]
    assert "op" in payload["returns"]


@pytest.mark.parametrize(
    ("field", "label", "variables"),
    [
        ("orphans", "orphans program", ["previous", "new"]),
        ("close", "close program", ["id", "resolution"]),
        ("resolutions", "resolutions program", []),
    ],
)
def test_reconcile_programs_carry_their_own_annotation(field: str, label: str, variables: list[str]) -> None:
    payload = _annotation(StateTemplateReconcile, field)
    assert payload["label"] == label
    assert _variable_names(payload) == variables


def test_state_template_document_carries_every_program_annotation() -> None:
    defs = StateTemplateDocument.model_json_schema()["$defs"]
    assert EXPRESSION_ANNOTATION_KEY in defs["StateTemplateDeclarations"]["properties"]["check"]
    assert EXPRESSION_ANNOTATION_KEY in defs["StateTemplateJq"]["properties"]["jq"]
    for field in ("orphans", "close", "resolutions"):
        assert EXPRESSION_ANNOTATION_KEY in defs["StateTemplateReconcile"]["properties"][field], field


@pytest.mark.parametrize("model", [PresetBody, PresetSeed])
def test_preset_maps_carry_the_preset_map_annotations(model: type[BaseModel]) -> None:
    assert _annotation(model, "input_map") == PRESET_INPUT_MAP_ANNOTATION
    assert _annotation(model, "output_map") == PRESET_OUTPUT_MAP_ANNOTATION


def test_preset_input_map_reads_fixed_kwargs_as_fixed() -> None:
    assert _variable_names(PRESET_INPUT_MAP_ANNOTATION) == ["fixed"]
    assert "variables" not in PRESET_OUTPUT_MAP_ANNOTATION


@pytest.mark.parametrize("model", [AccessPolicy, RoleDefinition])
def test_access_control_condition_is_the_access_condition_type(model: type[BaseModel]) -> None:
    class _Carrier(BaseModel):
        condition: AccessCondition = None

    assert _annotation(model, "condition") == _annotation(_Carrier, "condition")


@pytest.mark.parametrize(
    ("alias", "field"),
    [
        (DoorCancelExpr, "cancel_expr"),
        (DoorResumeExpr, "resume_expr"),
        (DoorStartExpr, "start_expr"),
        (DoorExtrasExpr, "extras_expr"),
    ],
)
def test_door_expression_types_carry_the_mixin_annotation(alias: Any, field: str) -> None:
    carrier = type("_Carrier", (BaseModel,), {"__annotations__": {field: alias}, field: None})
    payload = _annotation(carrier, field)
    assert payload == _annotation(ParkableDoorMixin, field)
    assert _variable_names(payload) == [PARKED_VARIABLE]


@pytest.mark.parametrize("model", [ApiConversationRouteCreate, ChannelConversationRouteCreate])
def test_conversation_route_expressions_carry_the_route_annotations(model: type[BaseModel]) -> None:
    assert _annotation(model, "start_expr")["label"] == "start expression"
    assert _annotation(model, "reply_expr")["label"] == "reply expression"
    for field in ("cancel_expr", "resume_expr", "extras_expr"):
        assert _annotation(model, field) == _annotation(ParkableDoorMixin, field)


Schema = dict[str, Any]


def _resolve(member: Schema, defs: Schema) -> Schema:
    ref = member.get("$ref")
    if isinstance(ref, str):
        return cast("Schema", defs.get(ref.rsplit("/", 1)[-1], {}))
    return member


def _members(schema: Schema, key: str) -> list[Schema]:
    return cast("list[Schema]", schema.get(key, []))


def _is_templated(prop: Schema, defs: Schema) -> bool:
    members = [prop, *_members(prop, "anyOf"), *_members(prop, "oneOf")]
    return any(TEMPLATED_TEXT_ANNOTATION_KEY in _resolve(member, defs) for member in members)


def _unannotated_jq_properties(schema: Schema, defs: Schema, path: str, seen: frozenset[str]) -> list[str]:
    """The path of every TemplatedText jq property under ``schema`` lacking the annotation, each ``$ref`` once."""
    found: list[str] = []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        if name not in seen:
            found += _unannotated_jq_properties(_resolve(schema, defs), defs, f"{path}<{name}>", seen | {name})
    properties = cast("dict[str, Schema]", schema.get("properties") or {})
    for key, prop in properties.items():
        if (
            key not in _NON_JQ_TEMPLATED_PROPERTIES
            and _is_templated(prop, defs)
            and EXPRESSION_ANNOTATION_KEY not in prop
        ):
            found.append(f"{path}.{key}")
        found += _unannotated_jq_properties(prop, defs, f"{path}.{key}", seen)
    for key in ("anyOf", "oneOf", "allOf"):
        for member in _members(schema, key):
            found += _unannotated_jq_properties(member, defs, path, seen)
    for key in ("items", "additionalProperties"):
        nested = schema.get(key)
        if isinstance(nested, dict):
            found += _unannotated_jq_properties(cast("Schema", nested), defs, f"{path}[]", seen)
    return found


def test_every_templated_jq_property_in_the_served_documents_is_annotated() -> None:
    bundle = build_document_schemas()
    defs = cast("Schema", bundle["$defs"])
    documents = cast("dict[str, Schema]", bundle["documents"])
    unannotated = [
        path
        for name, document in documents.items()
        for path in _unannotated_jq_properties(document, defs, name, frozenset())
    ]
    assert unannotated == []
