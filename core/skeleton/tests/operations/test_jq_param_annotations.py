"""Every jq-expression parameter an operation exposes carries the ``x-tai42-expression`` annotation.

An operation reaches clients through two schemas: the MCP tool form, which fastmcp derives from
the operation's FLAT signature, and the OpenAPI document, built from its request and response
models. A model-level annotation never reaches the flat signature, so each door is read here as
the platform builds it — the tool form via ``_make_tool`` + ``Tool.from_function``, the models via
their JSON schema — and the annotation is asserted on every jq field of both.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp.tools import Tool
from pydantic import BaseModel
from tai42_contract.access_control import AccessPolicy
from tai42_contract.conversations import ApiConversationRouteCreate
from tai42_contract.hooks import HookSubject
from tai42_contract.presets import PRESET_INPUT_MAP_ANNOTATION, PRESET_OUTPUT_MAP_ANNOTATION
from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, TEMPLATED_TEXT_ANNOTATION_KEY

from tai42_skeleton.app.route_registry import load_api_routes
from tai42_skeleton.operations.api_keys.conditions import validate_condition
from tai42_skeleton.operations.api_keys.keys import create_api_key
from tai42_skeleton.operations.api_keys.models import ApiKeyCreate, ApiKeyEdit, ConditionValidation
from tai42_skeleton.operations.conversations.routes import create_conversation_route
from tai42_skeleton.operations.decorator import operation_metadata_of
from tai42_skeleton.operations.hooks import register_hook
from tai42_skeleton.operations.presets.create import create_preset
from tai42_skeleton.operations.presets.models import PresetCreate, PresetValidate, PresetVersionSave
from tai42_skeleton.operations.presets.validate import validate_preset
from tai42_skeleton.operations.presets.versions import save_version
from tai42_skeleton.operations.projection import _make_tool
from tai42_skeleton.operations.registry import operation_registry
from tai42_skeleton.operations.response_models.api_keys import TokenPayloadRow
from tai42_skeleton.operations.response_models.presets import PresetRecordView

# Operation parameters typed TemplatedText that hold something other than jq: a JSON Schema
# document (a preset's input/output schema) or a Jinja template (render_template's text).
_NON_JQ_TEMPLATED_PARAMS = frozenset({"input_schema", "output_schema", "text"})

_ACCESS_CONDITION = AccessPolicy.model_json_schema()["properties"]["condition"][EXPRESSION_ANNOTATION_KEY]


def _tool_parameters(fn: Any) -> dict[str, Any]:
    return Tool.from_function(_make_tool(operation_metadata_of(fn))).parameters


def _tool_annotation(fn: Any, param: str) -> dict[str, Any]:
    return _tool_parameters(fn)["properties"][param][EXPRESSION_ANNOTATION_KEY]


def _model_annotation(model: type[BaseModel], field: str) -> dict[str, Any]:
    return model.model_json_schema()["properties"][field][EXPRESSION_ANNOTATION_KEY]


@pytest.mark.parametrize("fn", [create_api_key, validate_condition])
def test_api_key_condition_params_carry_the_access_condition_annotation(fn: Any) -> None:
    assert _tool_annotation(fn, "condition") == _ACCESS_CONDITION


@pytest.mark.parametrize("fn", [create_preset, save_version])
def test_preset_map_params_carry_the_preset_map_annotations(fn: Any) -> None:
    assert _tool_annotation(fn, "input_map") == PRESET_INPUT_MAP_ANNOTATION
    assert _tool_annotation(fn, "output_map") == PRESET_OUTPUT_MAP_ANNOTATION


def test_validate_preset_map_values_share_the_preset_map_schemas() -> None:
    validate_props = _tool_parameters(validate_preset)["properties"]
    create_props = _tool_parameters(create_preset)["properties"]
    assert validate_props["input_map_value"] == create_props["input_map"]
    assert validate_props["output_map_value"] == create_props["output_map"]
    assert validate_props["input_map_value"][EXPRESSION_ANNOTATION_KEY] == PRESET_INPUT_MAP_ANNOTATION
    assert validate_props["output_map_value"][EXPRESSION_ANNOTATION_KEY] == PRESET_OUTPUT_MAP_ANNOTATION


def test_create_conversation_route_params_carry_the_route_annotations() -> None:
    for param in ("start_expr", "cancel_expr", "resume_expr", "extras_expr", "reply_expr"):
        assert _tool_annotation(create_conversation_route, param) == _model_annotation(
            ApiConversationRouteCreate, param
        ), param


def test_register_hook_subject_key_expr_carries_the_subject_annotation() -> None:
    parameters = _tool_parameters(register_hook)
    subject = parameters["$defs"]["HookSubject"]["properties"]["key_expr"]
    assert subject[EXPRESSION_ANNOTATION_KEY] == _model_annotation(HookSubject, "key_expr")


@pytest.mark.parametrize("model", [ApiKeyCreate, ApiKeyEdit, ConditionValidation, TokenPayloadRow])
def test_api_key_models_carry_the_access_condition_annotation(model: type[BaseModel]) -> None:
    assert _model_annotation(model, "condition") == _ACCESS_CONDITION


@pytest.mark.parametrize("model", [PresetCreate, PresetValidate, PresetVersionSave, PresetRecordView])
def test_preset_models_carry_the_preset_map_annotations(model: type[BaseModel]) -> None:
    assert _model_annotation(model, "input_map") == PRESET_INPUT_MAP_ANNOTATION
    assert _model_annotation(model, "output_map") == PRESET_OUTPUT_MAP_ANNOTATION


def test_preset_record_view_serialization_schema_carries_the_annotations() -> None:
    properties = PresetRecordView.model_json_schema(mode="serialization")["properties"]
    assert properties["input_map"][EXPRESSION_ANNOTATION_KEY] == PRESET_INPUT_MAP_ANNOTATION
    assert properties["output_map"][EXPRESSION_ANNOTATION_KEY] == PRESET_OUTPUT_MAP_ANNOTATION


def _is_templated(prop: dict[str, Any], defs: dict[str, Any]) -> bool:
    for member in (prop, *prop.get("anyOf", []), *prop.get("oneOf", [])):
        ref = member.get("$ref")
        target = defs.get(ref.rsplit("/", 1)[-1], {}) if isinstance(ref, str) else member
        if TEMPLATED_TEXT_ANNOTATION_KEY in target:
            return True
    return False


def test_every_templated_jq_param_of_every_operation_is_annotated() -> None:
    list(load_api_routes())
    unannotated: list[str] = []
    for meta in operation_registry.all():
        parameters = Tool.from_function(_make_tool(meta)).parameters
        defs = parameters.get("$defs", {})
        for name, prop in parameters.get("properties", {}).items():
            if name in _NON_JQ_TEMPLATED_PARAMS or not _is_templated(prop, defs):
                continue
            if EXPRESSION_ANNOTATION_KEY not in prop:
                unannotated.append(f"{meta.name}.{name}")
    assert unannotated == []
