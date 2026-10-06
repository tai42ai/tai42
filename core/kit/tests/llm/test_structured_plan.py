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
