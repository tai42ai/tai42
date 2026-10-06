"""``plan_structured_output`` + the provider-native binding seam.

The plan is decided from three generic inputs — the model's declared capabilities
(``profile``), the kit's provider binding, and whether the schema is representable —
with no model id or vendor name in the logic. A model declaring native output over a
bindable, representable schema takes the native tier; a model declaring none (or a
non-representable schema, or a provider with no binding) falls to the tool tier when
forced choice is allowed; a model with neither capability is refused loudly.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from tai42_kit.llm.models import native_structured_output_kwargs, supports_native_structured_output
from tai42_kit.llm.structured import (
    StructuredOutputPlan,
    StructuredOutputUnsupportedError,
    plan_structured_output,
)


class _Fake:
    """A duck-typed stand-in for a chat model carrying only ``profile`` and a label."""

    def __init__(self, profile: dict[str, Any] | None, model_name: str = "fake-model") -> None:
        self.profile = profile
        self.model_name = model_name


def _llm(profile: dict[str, Any] | None) -> BaseChatModel:
    return cast(BaseChatModel, _Fake(profile))


_SCHEMA = {"title": "Answer", "type": "object", "properties": {"value": {"type": ["integer", "null"]}}}
_MAP_SCHEMA = {"title": "Map", "type": "object", "additionalProperties": {"type": "integer"}}

# A schema holding a MIXED type-less enum (null plus string ids) inside an array's items, beside a
# nullable type-array — the shape the splitter builds once an item is shown; every object closes
# itself with additionalProperties:false. The openai/xai/google native binders cannot carry a
# type-less enum without inflating it into an anyOf union, so for them this schema is "not natively
# representable" and is carried by the tool tier with the authored shape; anthropic binds the
# authored schema verbatim and a plain enum (including null members) over closed objects is within
# its contract, so for anthropic the same schema is natively representable and carried unchanged.
_MIXED_ENUM_SCHEMA = {
    "title": "Split",
    "type": "object",
    "properties": {
        "talk": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"enum": [None, "id-a", "id-b", "id-c"]},
                    "want": {"type": ["string", "null"]},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["talk"],
    "additionalProperties": False,
}


def test_tool_tier_carries_a_mixed_type_less_enum_schema_unchanged() -> None:
    # openai declared, bindable, forced choice allowed; but the mixed type-less enum is not
    # representable by its generic native grammar (no value-preserving minimal adaptation gives it a
    # type), so the plan is the tool tier and the schema it validates against is the authored one.
    plan = plan_structured_output(_llm({"structured_output": True}), "openai", _MIXED_ENUM_SCHEMA)
    assert plan.mode == "tool"
    assert plan.validation_schema is _MIXED_ENUM_SCHEMA


def test_loud_error_when_mixed_enum_and_forced_choice_refused() -> None:
    # Not carried by the generic native grammar and the model refuses forced tool choice: refused
    # loudly before any request, naming the provider and the construct neither tier can carry.
    with pytest.raises(StructuredOutputUnsupportedError) as excinfo:
        plan_structured_output(_llm({"structured_output": True, "tool_choice": False}), "openai", _MIXED_ENUM_SCHEMA)
    message = str(excinfo.value)
    assert "openai" in message
    assert "enum" in message.lower()
    assert excinfo.value.provider == "openai"


def test_native_minimally_adapts_without_wrapping_an_enum() -> None:
    # A schema representable by the generic native grammar (nullable type-array + single-type
    # type-less enum) stays native for openai/xai/google: the type-array becomes an anyOf, the
    # single-type enum gets its type, and NO enum is wrapped in an anyOf — value sets are preserved.
    schema = {
        "title": "R",
        "type": "object",
        "properties": {
            "status": {"enum": ["open", "closed"]},
            "n": {"type": ["integer", "null"]},
        },
        "required": ["status"],
    }
    plan = plan_structured_output(_llm({"structured_output": True}), "openai", schema)
    assert plan.mode == "native"
    assert plan.provider_schema is not None
    status = plan.provider_schema["properties"]["status"]
    assert status.get("type") == "string"
    assert status["enum"] == ["open", "closed"]
    assert "anyOf" not in status
    assert "anyOf" in plan.provider_schema["properties"]["n"]


def test_anthropic_native_carries_the_authored_schema_verbatim() -> None:
    # The splitter shape (a type-less enum with a null member, beside a nullable type-array, over
    # closed objects) is within anthropic's documented contract and is bound directly: the plan is
    # native and provider_schema is a by-value copy of the AUTHORED schema, byte-identical and NOT
    # rewritten (a distinct object, so content equality — not identity).
    plan = plan_structured_output(_llm({"structured_output": True}), "anthropic", _MIXED_ENUM_SCHEMA)
    assert plan.mode == "native"
    assert plan.provider_schema == _MIXED_ENUM_SCHEMA
    assert plan.provider_schema is not _MIXED_ENUM_SCHEMA
    assert plan.validation_schema is _MIXED_ENUM_SCHEMA


# Each construct is outside anthropic's documented structured-output contract; the schema is otherwise
# representable (every object closes itself with additionalProperties:false, except where an open
# object IS the construct under test), so the named reason (with a JSON path) is exactly this construct.
_ANTHROPIC_UNSUPPORTED_CONSTRUCTS: dict[str, tuple[dict[str, Any], str]] = {
    "external ref": (
        {
            "title": "Ext",
            "type": "object",
            "properties": {"x": {"$ref": "https://example.test/s"}},
            "required": ["x"],
            "additionalProperties": False,
        },
        "an external $ref at $.properties.x",
    ),
    "allOf with ref": (
        {
            "title": "AllRef",
            "type": "object",
            "properties": {"x": {"allOf": [{"type": "string"}], "$ref": "#/$defs/A"}},
            "$defs": {"A": {"type": "object", "additionalProperties": False}},
            "required": ["x"],
            "additionalProperties": False,
        },
        "allOf combined with $ref at $.properties.x",
    ),
    "complex enum member": (
        {
            "title": "Cplx",
            "type": "object",
            "properties": {"x": {"enum": ["a", {"k": 1}]}},
            "required": ["x"],
            "additionalProperties": False,
        },
        "a complex type as an enum member at $.properties.x",
    ),
    "numeric constraint": (
        {
            "title": "Num",
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 0}},
            "required": ["n"],
            "additionalProperties": False,
        },
        "a numeric constraint at $.properties.n",
    ),
    "string constraint": (
        {
            "title": "Str",
            "type": "object",
            "properties": {"s": {"type": "string", "pattern": "^a"}},
            "required": ["s"],
            "additionalProperties": False,
        },
        "a string constraint at $.properties.s",
    ),
    "array constraint": (
        {
            "title": "Arr",
            "type": "object",
            "properties": {"a": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
            "required": ["a"],
            "additionalProperties": False,
        },
        "an array constraint at $.properties.a",
    ),
    "open additionalProperties (true)": (
        {
            "title": "Open",
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "additionalProperties": True,
            "required": ["x"],
        },
        "an object with additionalProperties not false at $",
    ),
    "open additionalProperties (omitted)": (
        {"title": "Omit", "type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        "an object with additionalProperties not false at $",
    ),
}

# The subset the tool tier can bind from a raw dict (the TypedDict converter carries these); an
# external $ref, an allOf+$ref and a complex enum member have no dict TypedDict form, so their
# tool-tier binding is exercised only through the loud-refusal path, which never builds one.
_ANTHROPIC_TOOL_BINDABLE = (
    "numeric constraint",
    "string constraint",
    "array constraint",
    "open additionalProperties (true)",
    "open additionalProperties (omitted)",
)


@pytest.mark.parametrize(
    ("schema", "reason"),
    list(_ANTHROPIC_UNSUPPORTED_CONSTRUCTS.values()),
    ids=list(_ANTHROPIC_UNSUPPORTED_CONSTRUCTS),
)
def test_anthropic_unsupported_construct_names_its_reason_when_refused(schema: dict[str, Any], reason: str) -> None:
    # Native declared but the construct is outside anthropic's contract, and forced tool choice is
    # refused: refused loudly, naming the provider and the exact construct with its JSON path.
    with pytest.raises(StructuredOutputUnsupportedError) as excinfo:
        plan_structured_output(_llm({"structured_output": True, "tool_choice": False}), "anthropic", schema)
    assert reason in str(excinfo.value)
    assert excinfo.value.provider == "anthropic"


@pytest.mark.parametrize("key", _ANTHROPIC_TOOL_BINDABLE)
def test_anthropic_unsupported_construct_routes_to_tool_when_capable(key: str) -> None:
    # The same construct, with forced tool choice allowed: not natively representable for anthropic,
    # so it falls to the tool tier with the authored schema.
    schema, _ = _ANTHROPIC_UNSUPPORTED_CONSTRUCTS[key]
    plan = plan_structured_output(_llm({"structured_output": True}), "anthropic", schema)
    assert plan.mode == "tool"
    assert plan.validation_schema is schema


class _RecNode(BaseModel):
    """A self-referential model: its JSON schema is a ``$ref`` cycle anthropic cannot carry."""

    value: int
    child: _RecNode | None = None


def test_anthropic_recursive_schema_named_reason_and_tool_tier() -> None:
    # A recursive schema ($ref cycle): refused loudly naming it when forced choice is refused, and
    # carried by the tool tier (the model class itself, since a TypedDict tree cannot express a cycle)
    # when forced choice is allowed.
    with pytest.raises(StructuredOutputUnsupportedError) as excinfo:
        plan_structured_output(_llm({"structured_output": True, "tool_choice": False}), "anthropic", _RecNode)
    assert "recursive schema" in str(excinfo.value)
    plan = plan_structured_output(_llm({"structured_output": True}), "anthropic", _RecNode)
    assert plan.mode == "tool"
    assert plan.bound_typed_dict is _RecNode


_SIXTEEN_UNION = {
    "title": "Big",
    "type": "object",
    "properties": {f"p{i}": {"type": ["string", "null"]} for i in range(16)},
    "additionalProperties": False,
}
_SEVENTEEN_UNION = {
    "title": "Big",
    "type": "object",
    "properties": {
        **{f"p{i}": {"type": ["string", "null"]} for i in range(16)},
        "nested": {
            "type": "object",
            "properties": {"inner": {"type": ["integer", "null"]}},
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}
_TWENTYFOUR_OPTIONAL = {
    "title": "Big",
    "type": "object",
    "properties": {f"p{i}": {"type": "string"} for i in range(24)},
    "additionalProperties": False,
}
_TWENTYFIVE_OPTIONAL = {
    "title": "Big",
    "type": "object",
    "properties": {f"p{i}": {"type": "string"} for i in range(25)},
    "additionalProperties": False,
}


def test_anthropic_union_count_at_the_limit_is_representable() -> None:
    # Exactly 16 parameters using a type array: within the documented counted limit, so native and
    # carried verbatim (a by-value copy — content equality).
    plan = plan_structured_output(_llm({"structured_output": True}), "anthropic", _SIXTEEN_UNION)
    assert plan.mode == "native"
    assert plan.provider_schema == _SIXTEEN_UNION
    assert plan.provider_schema is not _SIXTEEN_UNION


def test_anthropic_union_count_over_the_limit_names_its_reason() -> None:
    # 17 parameters using anyOf or type arrays, counting a nested one: over the documented limit.
    with pytest.raises(StructuredOutputUnsupportedError) as excinfo:
        plan_structured_output(_llm({"structured_output": True, "tool_choice": False}), "anthropic", _SEVENTEEN_UNION)
    assert "more than 16 parameters using anyOf or type arrays" in str(excinfo.value)


def test_anthropic_optional_count_at_the_limit_is_representable() -> None:
    # Exactly 24 optional parameters: within the documented counted limit, so native (a by-value
    # copy — content equality).
    plan = plan_structured_output(_llm({"structured_output": True}), "anthropic", _TWENTYFOUR_OPTIONAL)
    assert plan.mode == "native"
    assert plan.provider_schema == _TWENTYFOUR_OPTIONAL
    assert plan.provider_schema is not _TWENTYFOUR_OPTIONAL


def test_anthropic_optional_count_over_the_limit_names_its_reason() -> None:
    # 25 optional parameters: over the documented limit.
    with pytest.raises(StructuredOutputUnsupportedError) as excinfo:
        plan_structured_output(
            _llm({"structured_output": True, "tool_choice": False}), "anthropic", _TWENTYFIVE_OPTIONAL
        )
    assert "more than 24 optional parameters" in str(excinfo.value)


def test_native_when_declared_bindable_and_representable() -> None:
    plan = plan_structured_output(_llm({"structured_output": True}), "openai", _SCHEMA)
    assert plan.mode == "native"
    assert plan.name == "Answer"
    assert plan.provider == "openai"
    assert plan.provider_schema is not None
    # The portable form drives the native bind; the original dict stays the validation schema.
    assert plan.validation_schema is _SCHEMA
    assert not isinstance(plan.provider_schema["properties"]["value"].get("type"), list)


def test_tool_when_native_not_declared_and_forced_choice_allowed() -> None:
    plan = plan_structured_output(_llm({}), "openai", _SCHEMA)
    assert plan.mode == "tool"
    assert plan.bound_typed_dict is not None


def test_tool_when_provider_has_no_native_binding() -> None:
    # The model declares native output, but the kit has no binding for the provider.
    plan = plan_structured_output(_llm({"structured_output": True}), "mistral", _SCHEMA)
    assert plan.mode == "tool"


def test_tool_when_schema_not_natively_representable() -> None:
    # Native declared and bindable, but a map object is not representable natively.
    plan = plan_structured_output(_llm({"structured_output": True}), "openai", _MAP_SCHEMA)
    assert plan.mode == "tool"


def test_refused_when_neither_capability() -> None:
    with pytest.raises(StructuredOutputUnsupportedError) as excinfo:
        plan_structured_output(_llm({"structured_output": False, "tool_choice": False}), "openai", _SCHEMA)
    message = str(excinfo.value)
    assert "fake-model" in message
    assert "openai" in message
    assert "native structured output" in message
    assert "forced tool choice" in message
    assert excinfo.value.provider == "openai"
    assert len(excinfo.value.reasons) == 2


def test_plan_is_frozen() -> None:
    plan = plan_structured_output(_llm({"structured_output": True}), "openai", _SCHEMA)
    assert isinstance(plan, StructuredOutputPlan)
    with pytest.raises(Exception):  # noqa: B017,PT011 frozen dataclass refuses reassignment
        plan.mode = "tool"  # type: ignore[misc]


def test_native_kwargs_per_provider() -> None:
    # anthropic binds the schema DIRECTLY and VERBATIM through output_config (no response_format,
    # no name wrapper); the schema object is carried unchanged.
    anthropic_kwargs = native_structured_output_kwargs("anthropic", "Answer", _SCHEMA)
    assert anthropic_kwargs == {"output_config": {"format": {"type": "json_schema", "schema": _SCHEMA}}}
    assert anthropic_kwargs["output_config"]["format"]["schema"] is _SCHEMA

    # openai/xai keep the OpenAI-shaped response_format with the named json_schema.
    openai_kwargs = native_structured_output_kwargs("openai", "Answer", _SCHEMA)
    assert openai_kwargs == {
        "response_format": {"type": "json_schema", "json_schema": {"name": "Answer", "schema": _SCHEMA}}
    }
    assert native_structured_output_kwargs("xai", "Answer", _SCHEMA) == openai_kwargs

    # google keeps its own response_json_schema.
    google_kwargs = native_structured_output_kwargs("google", "Answer", _SCHEMA)
    assert google_kwargs == {"response_mime_type": "application/json", "response_json_schema": _SCHEMA}


def test_supports_native_per_provider() -> None:
    assert supports_native_structured_output("openai") is True
    assert supports_native_structured_output("anthropic") is True
    assert supports_native_structured_output("xai") is True
    assert supports_native_structured_output("google") is True
    assert supports_native_structured_output("mistral") is False
    assert supports_native_structured_output("ollama") is False
    assert supports_native_structured_output("huggingface") is False


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported chat model provider"):
        supports_native_structured_output("nope")
    with pytest.raises(ValueError, match="no native structured-output binding"):
        native_structured_output_kwargs("mistral", "Answer", _SCHEMA)
