"""Capability-negotiated structured-output planning.

One decision, made once per compiled run at the seam where the model object is
known, from three generic inputs: the model's declared capabilities (``llm.profile``,
provider-populated), the provider binding the kit can make
(:func:`~tai42_kit.llm.models.supports_native_structured_output`), and whether the
authored schema is representable by the native grammars (after the portable rewrite).

* ``native`` — the model declares native structured output, the kit has a binding
  for the provider, and the schema is representable. Grammar-enforced; the one path a
  model that refuses forced tool calls offers.
* ``tool`` — the second tier: today's bounded-``TypedDict`` tool-calling mechanics,
  taken when the model declares no native structured output (or the schema is not
  representable, or the kit has no native binding) and the model allows forced tool
  choice.
* neither — refused loudly at compile with :class:`StructuredOutputUnsupportedError`,
  naming both missing capabilities, before any model call.

This module owns the plan, the capability logic and the schema preparation; it builds
no LangChain strategy object (that is the agents plugin's binding seam) so it stays
free of the agent framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from tai42_kit.llm.models import supports_native_structured_output
from tai42_kit.utils.data.json_schema_util import (
    NonPortableSchemaError,
    check_native_representable,
    inject_int64_bounds,
    json_schema_to_typed_dict,
    to_portable_schema,
)


class StructuredOutputUnsupportedError(Exception):
    """A model offers neither a native structured-output grammar nor forced tool choice.

    Raised at compile, before any model call, naming the model, the provider and both
    missing capabilities — never a silent free-text answer when the caller asked for a
    structured object.
    """

    def __init__(self, *, model: str, provider: str, reasons: list[str]) -> None:
        """Carry the model label, provider and the capability reasons the model is refused for."""
        self.model = model
        self.provider = provider
        self.reasons = reasons
        super().__init__(
            f"model {model!r} (provider {provider!r}) cannot produce the requested structured output: "
            + "; ".join(reasons)
        )


@dataclass(frozen=True)
class StructuredOutputPlan:
    """The resolved structured-output plan for one compiled run.

    ``mode`` selects native vs tool. ``name`` is the schema title / class name (the
    structured-output name). ``provider`` is the resolved LLM provider (for the native
    bind kwargs). ``provider_schema`` is the portable form the provider receives
    (native mode only). ``validation_schema`` is the ORIGINAL authored dict or pydantic
    class — what every door keeps validating produced output against, unchanged.
    ``bound_typed_dict`` is the int64-tightened ``TypedDict`` shape the tool tier binds
    (tool mode only).
    """

    mode: Literal["native", "tool"]
    name: str
    provider: str
    validation_schema: Any
    provider_schema: dict[str, Any] | None = None
    bound_typed_dict: Any | None = None


def _schema_dict_and_name(response_format: Any) -> tuple[dict[str, Any], str, Any]:
    """The authored ``(json_schema_dict, name, validation_schema)`` for a dict or pydantic class.

    A JSON-Schema dict validates against itself; a pydantic ``BaseModel`` class
    validates against the class (``validate_structured_output`` re-inflates the parsed
    dict) while its JSON schema drives the portable rewrite and the bounded TypedDict.
    """
    if isinstance(response_format, dict):
        name = response_format.get("title") or "Response"
        return response_format, name, response_format
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return response_format.model_json_schema(), response_format.__name__, response_format
    raise TypeError(
        f"response_format must be a JSON-Schema dict or a pydantic BaseModel class, got {type(response_format)!r}"
    )


def _bounded_typed_dict(schema_dict: dict[str, Any], name: str, validation_schema: Any) -> Any:
    """The int64-tightened ``TypedDict`` tool shape, or the pydantic class for a recursive model.

    A recursive model has no ``TypedDict``-tree form (the converter raises); the class
    itself is bound then, and its oversized-int door is closed by the rail's / the
    downstream ``validate_structured_output`` int64 walk.
    """
    try:
        return json_schema_to_typed_dict(inject_int64_bounds(schema_dict), name=name)
    except ValueError:
        if isinstance(validation_schema, type) and issubclass(validation_schema, BaseModel):
            return validation_schema
        raise


def plan_structured_output(llm: BaseChatModel, provider: str, response_format: Any) -> StructuredOutputPlan:
    """Resolve the structured-output plan for ``llm``/``provider`` and the authored ``response_format``.

    Native-first: when the model declares native structured output, the kit has a
    binding for the provider and the schema is representable, the native grammar is
    used. Otherwise the tool tier is taken when the model allows forced tool choice.
    A model with neither capability raises :class:`StructuredOutputUnsupportedError`.
    """
    schema_dict, name, validation_schema = _schema_dict_and_name(response_format)

    profile = getattr(llm, "profile", None) or {}
    native_declared = profile.get("structured_output") is True
    # Probe the provider binding only when the model declares native output: an unknown
    # provider raises loudly there, which should not fire for a model that would take the
    # tool tier regardless of its provider.
    native_bindable = native_declared and supports_native_structured_output(provider)
    forced_allowed = profile.get("tool_choice") is not False  # absent == unknown == allowed

    portable_schema: dict[str, Any] | None = None
    representable = False
    if native_declared and native_bindable:
        try:
            portable_schema = to_portable_schema(schema_dict)
        except NonPortableSchemaError:
            representable = False
        else:
            representable = check_native_representable(portable_schema)

    if native_declared and native_bindable and representable:
        return StructuredOutputPlan(
            mode="native",
            name=name,
            provider=provider,
            validation_schema=validation_schema,
            provider_schema=portable_schema,
        )

    if forced_allowed:
        return StructuredOutputPlan(
            mode="tool",
            name=name,
            provider=provider,
            validation_schema=validation_schema,
            bound_typed_dict=_bounded_typed_dict(schema_dict, name, validation_schema),
        )

    if not native_declared:
        native_reason = "it declares no native structured output"
    elif not native_bindable:
        native_reason = "the platform has no native binding for its provider"
    else:
        native_reason = "its schema is not natively representable"
    reasons = [native_reason, "and it refuses forced tool choice"]
    model_label = (
        getattr(llm, "model_name", None) or getattr(llm, "model", None) or getattr(llm, "model_id", "") or "model"
    )
    raise StructuredOutputUnsupportedError(model=model_label, provider=provider, reasons=reasons)
