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
# nullable type-array — the shape the splitter builds once a card/line/booking is shown. A native
# grammar binder cannot carry a type-less enum without inflating it into an anyOf union, so this
# schema is "not natively representable" and is carried by the tool tier with the authored shape.
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
            },
        }
    },
    "required": ["talk"],
}


def test_tool_tier_carries_a_mixed_type_less_enum_schema_unchanged() -> None:
    # Native is declared, bindable, forced choice allowed; but the mixed type-less enum is not
    # natively representable (no value-preserving minimal adaptation gives it a type), so the plan
    # is the tool tier and the schema it validates against is the authored one, unchanged.
    plan = plan_structured_output(_llm({"structured_output": True}), "anthropic", _MIXED_ENUM_SCHEMA)
    assert plan.mode == "tool"
    assert plan.validation_schema is _MIXED_ENUM_SCHEMA


def test_loud_error_when_mixed_enum_and_forced_choice_refused() -> None:
    # Not natively carried and the model refuses forced tool choice: refused loudly before any
    # request, naming the provider and the construct neither tier can carry.
    with pytest.raises(StructuredOutputUnsupportedError) as excinfo:
        plan_structured_output(_llm({"structured_output": True, "tool_choice": False}), "anthropic", _MIXED_ENUM_SCHEMA)
    message = str(excinfo.value)
    assert "anthropic" in message
    assert "enum" in message.lower()
    assert excinfo.value.provider == "anthropic"


def test_native_minimally_adapts_without_wrapping_an_enum() -> None:
    # A representable schema (nullable type-array + single-type type-less enum) stays native: the
    # type-array becomes an anyOf, the single-type enum gets its type, and NO enum is wrapped in an
    # anyOf — the enum value sets are preserved exactly.
    schema = {
        "title": "R",
        "type": "object",
        "properties": {
            "status": {"enum": ["open", "closed"]},
            "n": {"type": ["integer", "null"]},
        },
        "required": ["status"],
    }
    plan = plan_structured_output(_llm({"structured_output": True}), "anthropic", schema)
    assert plan.mode == "native"
    assert plan.provider_schema is not None
    status = plan.provider_schema["properties"]["status"]
    assert status.get("type") == "string"
    assert status["enum"] == ["open", "closed"]
    assert "anyOf" not in status
    assert "anyOf" in plan.provider_schema["properties"]["n"]


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
    openai_kwargs = native_structured_output_kwargs("openai", "Answer", _SCHEMA)
    assert openai_kwargs == {
        "response_format": {"type": "json_schema", "json_schema": {"name": "Answer", "schema": _SCHEMA}}
    }
    assert native_structured_output_kwargs("anthropic", "Answer", _SCHEMA) == openai_kwargs
    assert native_structured_output_kwargs("xai", "Answer", _SCHEMA) == openai_kwargs

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
