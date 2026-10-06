"""Provider-keyed construction of LangChain chat models."""

import asyncio
import copy
from functools import lru_cache
from typing import Any

from langchain_core.language_models import BaseChatModel

from tai42_kit.llm._secret_kwargs import KwargsCacheKey, unwrap_secret_kwargs
from tai42_kit.utils.data.json_schema_util import (
    adapt_native_schema,
    count_optional_properties,
    count_union_or_type_array_nodes,
    find_ref_cycle,
    has_array_constraint,
    has_complex_enum_member,
    has_external_ref,
    has_numeric_constraint,
    has_open_additional_properties,
    has_ref_with_allof,
    has_string_length_or_pattern,
    iter_schema_nodes,
    native_representability_reason,
)


async def get_llm_async(provider: str, **kwargs) -> BaseChatModel:
    """Build the chat model for ``provider`` in a worker thread, off the event loop."""
    return await asyncio.to_thread(get_llm, provider=provider, **kwargs)


def get_llm(provider: str, **kwargs) -> BaseChatModel:
    """Build (or return the cached) chat model for ``provider`` with the given kwargs.

    A set ``reasoning_effort`` is validated against the built model's declared
    capability after construction: the generic effort lever reaches the provider
    constructor as a kwarg, and a level the model does not declare is refused loudly
    here (see :func:`_validate_reasoning_effort`) rather than failing opaquely on the
    wire.
    """
    llm = _cached_llm(provider, KwargsCacheKey(kwargs))
    _validate_reasoning_effort(llm, kwargs.get("reasoning_effort"))
    return llm


@lru_cache(maxsize=64)
def _cached_llm(provider: str, kwargs_key: KwargsCacheKey) -> BaseChatModel:
    # The caller's original kwargs flow to the constructor untouched (no JSON
    # round trip); secrets are unwrapped only at this seam.
    return _build_llm(provider, **unwrap_secret_kwargs(kwargs_key.kwargs))


def _build_llm(provider: str, **kwargs) -> BaseChatModel:
    match provider:
        case "anthropic":
            from langchain_anthropic import ChatAnthropic  # pyright: ignore[reportMissingImports]

            return ChatAnthropic(**kwargs)

        case "mistral":
            from langchain_mistralai import ChatMistralAI  # pyright: ignore[reportMissingImports]

            return ChatMistralAI(**kwargs)

        case "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(**kwargs)

        case "google":
            from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

            return ChatGoogleGenerativeAI(**kwargs)

        case "xai":
            from langchain_xai import ChatXAI  # pyright: ignore[reportMissingImports]

            return ChatXAI(**kwargs)

        case "ollama":
            from langchain_ollama import ChatOllama  # pyright: ignore[reportMissingImports]

            return ChatOllama(**kwargs)

        case "huggingface":
            from langchain_huggingface import ChatHuggingFace  # pyright: ignore[reportMissingImports]

            return ChatHuggingFace(**kwargs)

    raise ValueError(f"Unsupported chat model provider: '{provider}'")


def _model_label(llm: BaseChatModel) -> str:
    """The model id/name a provider exposes, for a loud error naming the model."""
    return getattr(llm, "model_name", None) or getattr(llm, "model", None) or getattr(llm, "model_id", "") or "model"


def _validate_reasoning_effort(llm: BaseChatModel, effort: Any) -> None:
    """Refuse a ``reasoning_effort`` the built model does not declare, naming the levels it does.

    When the model's generic profile declares a non-empty ``reasoning_effort_levels``
    and ``effort`` is not among them, raise ``ValueError``. When the profile declares
    no levels the value passes through and the provider answers loudly on the wire
    (nothing the platform can check). An unset ``effort`` is a no-op.
    """
    if effort is None:
        return
    profile = getattr(llm, "profile", None) or {}
    levels = profile.get("reasoning_effort_levels")
    if isinstance(levels, list) and levels and effort not in levels:
        raise ValueError(
            f"reasoning_effort {effort!r} is not a declared level for model {_model_label(llm)!r}; "
            f"declared levels: {levels}"
        )


def supports_native_structured_output(provider: str) -> bool:
    """Whether the kit can bind ``provider``'s native structured-output grammar.

    The ``match`` mirrors :func:`_build_llm`'s so a provider name never leaves this
    module and an unknown provider is refused loudly. ``anthropic`` binds through its
    ``output_config``; ``openai``/``xai`` take the OpenAI-shaped ``response_format`` kwarg
    (consumed by ``langchain-openai`` and its ``ChatXAI`` subclass); ``google`` takes its
    own ``response_json_schema`` kwarg. ``mistral``/``ollama``/``huggingface`` have no
    verified native binding here, so they return ``False`` and take the tool tier —
    explicit, never a silent inert bind.
    """
    match provider:
        case "anthropic" | "openai" | "xai" | "google":
            return True
        case "mistral" | "ollama" | "huggingface":
            return False
    raise ValueError(f"Unsupported chat model provider: '{provider}'")


def shape_native_schema(provider: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The schema to send to ``provider``'s native grammar: the shape its binder carries.

    ``anthropic`` binds the authored schema DIRECTLY and VERBATIM, so it is returned as a
    by-value copy with its content UNCHANGED (its native binder carries the full authored
    shape, judged against its own documented contract — a distinct object, never a rewrite).
    ``openai``/``xai``/``google`` take the minimal, value-preserving
    adaptations (:func:`~tai42_kit.utils.data.json_schema_util.adapt_native_schema`): a
    nullable/multi ``"type"`` array becomes an ``anyOf`` of single-type members, a
    single-type type-less ``enum``/``const`` gets its type, and a mixed type-less ``enum``
    is left bare (reported non-representable, never inflated). The ``match`` mirrors
    :func:`_build_llm`'s so a provider name never leaves this module. A provider with no
    native binding (and an unknown one) raises loudly — the caller gates on
    :func:`supports_native_structured_output` first.
    """
    match provider:
        case "anthropic":
            return copy.deepcopy(schema)
        case "openai" | "xai" | "google":
            return adapt_native_schema(schema)
        case "mistral" | "ollama" | "huggingface":
            raise ValueError(f"provider '{provider}' has no native structured-output binding")
    raise ValueError(f"Unsupported chat model provider: '{provider}'")


def native_structured_output_kwargs(provider: str, name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The constructor-bind kwargs that force ``provider``'s native structured output for ``schema``.

    ``schema`` is the shape :func:`shape_native_schema` produced for the provider (what the
    provider receives); ``name`` is the structured-output name. ``anthropic`` binds the
    schema directly and verbatim through its ``output_config``; ``openai``/``xai`` take the
    OpenAI-shaped ``response_format`` with the named ``json_schema``; ``google`` takes its own
    ``response_json_schema``. The ``match`` mirrors :func:`_build_llm`'s. A provider with no
    native binding (and an unknown one) raises loudly — the caller gates on
    :func:`supports_native_structured_output` first, so reaching a non-native provider here is
    a bug, never a silent no-op bind.
    """
    match provider:
        case "anthropic":
            return {"output_config": {"format": {"type": "json_schema", "schema": schema}}}
        case "openai" | "xai":
            return {"response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}}
        case "google":
            return {"response_mime_type": "application/json", "response_json_schema": schema}
        case "mistral" | "ollama" | "huggingface":
            raise ValueError(f"provider '{provider}' has no native structured-output binding")
    raise ValueError(f"Unsupported chat model provider: '{provider}'")


#: The per-node constructs Anthropic's documented structured-output contract does not
#: support, paired with the vendor-neutral reason that names each (a JSON path is appended).
_ANTHROPIC_UNSUPPORTED_NODE_CONSTRUCTS = (
    (has_external_ref, "an external $ref"),
    (has_ref_with_allof, "allOf combined with $ref"),
    (has_numeric_constraint, "a numeric constraint"),
    (has_string_length_or_pattern, "a string constraint"),
    (has_array_constraint, "an array constraint"),
    (has_complex_enum_member, "a complex type as an enum member"),
    (has_open_additional_properties, "an object with additionalProperties not false"),
)
#: Documented counted limits a schema must not exceed on its own (the internal grammar-size
#: limit is not pre-checkable and is left to the vendor's call-time error).
_ANTHROPIC_MAX_UNION_OR_TYPE_ARRAY_PARAMS = 16
_ANTHROPIC_MAX_OPTIONAL_PARAMS = 24


def _anthropic_representability_reason(schema: dict[str, Any]) -> str | None:
    """Why ``schema`` is outside Anthropic's documented structured-output contract, or ``None``.

    Returns a vendor-neutral description with a JSON path of the FIRST failing construct or
    counted limit. Checks, in order: a recursive schema (a ``$ref`` cycle); then, per node in
    document order, an external ``$ref``, ``allOf`` combined with ``$ref``, a numeric
    constraint, a string length/``pattern`` constraint, an array constraint beyond a
    ``minItems`` of 0 or 1, a complex type as an ``enum`` member, and an object opening its
    ``additionalProperties``; then the counted limits — more than 16 parameters using
    ``anyOf`` or type arrays, and more than 24 optional parameters. Everything the contract
    allows is carried: plain enums (including null members), type arrays, ``anyOf``, local
    non-cyclic ``$ref``/``$defs``, ``const``, ``required``, ``additionalProperties: false``,
    documented string ``format`` and a ``minItems`` of 0 or 1.
    """
    cycle_path = find_ref_cycle(schema)
    if cycle_path is not None:
        return f"a recursive schema at {cycle_path}"
    for node, path in iter_schema_nodes(schema):
        for check, label in _ANTHROPIC_UNSUPPORTED_NODE_CONSTRUCTS:
            if check(node):
                return f"{label} at {path}"
    if count_union_or_type_array_nodes(schema) > _ANTHROPIC_MAX_UNION_OR_TYPE_ARRAY_PARAMS:
        return f"more than {_ANTHROPIC_MAX_UNION_OR_TYPE_ARRAY_PARAMS} parameters using anyOf or type arrays"
    if count_optional_properties(schema) > _ANTHROPIC_MAX_OPTIONAL_PARAMS:
        return f"more than {_ANTHROPIC_MAX_OPTIONAL_PARAMS} optional parameters"
    return None


def native_representability_reason_for_provider(provider: str, schema: dict[str, Any]) -> str | None:
    """Why ``schema`` is not representable by ``provider``'s native grammar, or ``None`` if it is.

    ``anthropic`` is judged against its own documented structured-output contract
    (:func:`_anthropic_representability_reason`), since its binder carries the authored schema
    verbatim; ``openai``/``xai``/``google`` are judged by the generic native-grammar check
    (:func:`~tai42_kit.utils.data.json_schema_util.native_representability_reason`) over the
    shaped schema. The ``match`` mirrors :func:`_build_llm`'s so a provider name never leaves
    this module. A provider with no native binding (and an unknown one) raises loudly — the
    caller gates on :func:`supports_native_structured_output` first.
    """
    match provider:
        case "anthropic":
            return _anthropic_representability_reason(schema)
        case "openai" | "xai" | "google":
            return native_representability_reason(schema)
        case "mistral" | "ollama" | "huggingface":
            raise ValueError(f"provider '{provider}' has no native structured-output binding")
    raise ValueError(f"Unsupported chat model provider: '{provider}'")


def system_prompt_cache_mark(provider: str) -> dict[str, Any] | None:
    """The content-block kwargs that mark ``provider``'s system prompt for caching, or ``None``.

    A provider that takes an explicit system-prompt cache breakpoint returns the
    ``system_content_kwargs`` that carry it (the block :func:`build_system_message` merges onto
    the system message); a provider that caches its prefixes with no mark, or does not cache,
    returns ``None``. The ``match`` mirrors :func:`_build_llm`'s so a provider name never leaves
    this module and an unknown provider is refused loudly rather than silently un-marked.
    """
    match provider:
        case "anthropic":
            return {"cache_control": {"type": "ephemeral"}}

        case "mistral" | "openai" | "google" | "xai" | "ollama" | "huggingface":
            return None

    raise ValueError(f"Unsupported chat model provider: '{provider}'")
