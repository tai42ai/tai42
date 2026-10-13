"""Provider-keyed construction of LangChain chat models.

One rule holds for every provider, here and in the embedding builders (``llm/embedding.py``): each
kwarg handed to :func:`get_llm` reaches the provider — a connection setting (model, key, base URL,
timeout) is configured on the provider's client, a generation setting is sent in its request — or
the build refuses it with ``ValueError`` naming it. No setting is dropped. What a provider can carry
is read from its own client: the chat model's declared fields (by name or alias) that it applies,
plus, where the chat model forwards its ``model_kwargs`` into the SDK call, the parameters of the
call that chat model makes (any name for Mistral, whose chat model writes them into its JSON request
body). A name the kit carries that the chat model does not declare is passed to it as
``model_kwargs``; a flat setting a provider takes under another name or in its client options is
mapped there, and the same setting given twice is refused naming both. One case the build cannot see:
OpenAI and xAI chat models switch to the responses call when a built-in tool is bound later (a
``gpt-6`` model when any tool is bound), and a setting only the chat-completions call takes then fails
that first call with ``TypeError``, before any request is sent.
"""

import asyncio
import copy
from functools import lru_cache
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
from tai42_contract.agent import TOOL_NAME_MAX_LEN

from tai42_kit.llm._carry import carried, declared_names, parameters, refuse, refuse_twice, set_client_option
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


class ToolNameTooLongError(ValueError):
    """A tool name is longer than a bound tool name may be."""


def check_tool_name(name: str) -> None:
    """Refuse a tool name longer than :data:`~tai42_contract.agent.TOOL_NAME_MAX_LEN`.

    The bound is the strictest among the providers a bound tool is sent to; a longer name is
    refused, never truncated (a truncated name would name a different tool).
    """
    if len(name) > TOOL_NAME_MAX_LEN:
        raise ToolNameTooLongError(
            f"tool name {name!r} is {len(name)} characters; a bound tool name may be at most {TOOL_NAME_MAX_LEN}"
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

            return ChatAnthropic(**carried("anthropic", kwargs, ChatAnthropic, _anthropic_call_parameters))

        case "mistral":
            from langchain_mistralai import ChatMistralAI  # pyright: ignore[reportMissingImports]

            # The chat model declares ``max_concurrent_requests`` but never reads it.
            return ChatMistralAI(
                **carried("mistral", kwargs, ChatMistralAI, None, unsent=frozenset({"max_concurrent_requests"}))
            )

        case "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(**carried("openai", kwargs, ChatOpenAI, _openai_call_parameters))

        case "google":
            from langchain_google_genai import ChatGoogleGenerativeAI  # pyright: ignore[reportMissingImports]

            # The chat model declares ``model_kwargs`` but never sends it.
            return ChatGoogleGenerativeAI(
                **carried("google", kwargs, ChatGoogleGenerativeAI, frozenset(), unsent=frozenset({"model_kwargs"}))
            )

        case "xai":
            from langchain_xai import ChatXAI  # pyright: ignore[reportMissingImports]

            return ChatXAI(**carried("xai", kwargs, ChatXAI, _openai_call_parameters))

        case "ollama":
            return _build_ollama_chat(**kwargs)

        case "huggingface":
            return _build_huggingface_chat(**kwargs)

    raise ValueError(f"Unsupported chat model provider: '{provider}'")


def _openai_call_parameters(
    model_cls: type[BaseModel], built: dict[str, Any], forwarded: dict[str, Any]
) -> frozenset[str]:
    """The parameters of the OpenAI SDK call the configured chat model makes.

    The chat model picks the responses call or the chat-completions call from its own settings and
    the request it builds (``BaseChatOpenAI._use_responses_api``); the kit asks it, on a model built
    with the same settings, which one a request without tools takes.
    """
    from openai.resources.chat import Completions
    from openai.resources.responses import Responses

    probe: Any = model_cls(**built, **({"model_kwargs": forwarded} if forwarded else {}))
    call = Responses.create if probe._use_responses_api(probe._default_params) else Completions.create
    # The chat model fills the conversation, the model and streaming itself.
    return parameters(call) - {"messages", "input", "model", "stream"}


def _anthropic_call_parameters(
    model_cls: type[BaseModel], built: dict[str, Any], forwarded: dict[str, Any]
) -> frozenset[str]:
    """The parameters of the Anthropic SDK call the configured chat model makes.

    The chat model calls the beta messages endpoint when its request carries ``betas`` and the plain
    one otherwise (``ChatAnthropic._create``); the kit builds that request, without tools, on a model
    built with the same settings.
    """
    from anthropic.resources import Messages  # pyright: ignore[reportMissingImports]
    from anthropic.resources.beta import Messages as BetaMessages  # pyright: ignore[reportMissingImports]

    probe: Any = model_cls(**built, **({"model_kwargs": forwarded} if forwarded else {}))
    payload = probe._get_request_payload([HumanMessage("")])
    # The chat model fills the conversation, the model and streaming itself.
    return parameters(BetaMessages.create if "betas" in payload else Messages.create) - {"messages", "model", "stream"}


def _renamed(provider: str, kwargs: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """``kwargs`` with each flat name in ``names`` under the provider's name, refusing both given."""
    for flat, own in names.items():
        if flat in kwargs and own in kwargs:
            refuse_twice(provider, flat, own)
    return {names.get(key, key): value for key, value in kwargs.items()}


# The flat chat-model settings under the names Ollama's chat model takes.
_OLLAMA_NAMES = {"max_tokens": "num_predict", "reasoning_effort": "reasoning"}


def _build_ollama_chat(**kwargs: Any) -> BaseChatModel:
    """A ``ChatOllama`` built from flat settings.

    ``max_tokens`` is sent as ``num_predict`` and ``reasoning_effort`` as its reasoning level
    (``reasoning``, sent as ``think``); ``timeout`` and ``api_key`` (a bearer ``authorization``
    header) configure its HTTP client through ``client_kwargs``. ``ChatOllama`` forwards no
    constructor name outside its declared fields, so every other name must be one of them.
    """
    from langchain_ollama import ChatOllama  # pyright: ignore[reportMissingImports]

    renamed = _renamed("ollama", kwargs, _OLLAMA_NAMES)
    _ollama_client_options("ollama", renamed)
    return ChatOllama(**carried("ollama", renamed, ChatOllama, frozenset()))


# The containers whose entries reach Ollama's HTTP clients: shared, then per client.
_OLLAMA_CLIENT_OVERRIDES = ("sync_client_kwargs", "async_client_kwargs")


def _ollama_client_options(provider: str, kwargs: dict[str, Any]) -> None:
    """Move ``timeout`` and ``api_key`` into Ollama's ``client_kwargs`` (its HTTP clients' options).

    ``api_key`` becomes a bearer ``authorization`` header. The same option set another way — in
    ``client_kwargs`` or a per-client ``sync_client_kwargs`` / ``async_client_kwargs`` (header names
    compared without letter case), or credentials in the ``base_url`` (with or without a scheme), which
    Ollama's chat and embedding models turn into an ``Authorization`` header with the library's own
    ``parse_url_with_auth`` — is refused as given twice, so the sync and the async client never send
    different values.
    """
    if "timeout" in kwargs:
        set_client_option(
            provider,
            kwargs,
            ("client_kwargs", "timeout"),
            kwargs.pop("timeout"),
            flat="timeout",
            also=_OLLAMA_CLIENT_OVERRIDES,
        )
    if "api_key" in kwargs:
        from langchain_ollama._utils import parse_url_with_auth  # pyright: ignore[reportMissingImports]

        _, url_auth = parse_url_with_auth(kwargs.get("base_url"))
        if url_auth:
            refuse_twice(provider, "api_key", "base_url")
        bearer = f"Bearer {kwargs.pop('api_key')}"
        set_client_option(
            provider,
            kwargs,
            ("client_kwargs", "headers", "authorization"),
            bearer,
            flat="api_key",
            header=True,
            also=_OLLAMA_CLIENT_OVERRIDES,
        )


# What ``ChatHuggingFace`` fills itself in each chat-completion call.
_HUGGINGFACE_MANAGED = frozenset(
    {"messages", "model", "stream", "stream_options", "tools", "tool_choice", "response_format"}
)
# The chat model's own fields its chat path applies, beyond the generic chat-model ones.
_HUGGINGFACE_CHAT_FIELDS = frozenset({"streaming", "stream_usage"})


def _build_huggingface_chat(
    *,
    model: str,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """A ``ChatHuggingFace`` over a hosted Hugging Face Inference endpoint, built from flat settings.

    ``ChatHuggingFace`` wraps a required ``llm``; the flat settings carry a hosted model's shape, so
    the wrapped model is a ``HuggingFaceEndpoint`` configured with the connection settings: the hub
    model ``model``, or, when ``base_url`` is set, the server at that URL, which serves its own model
    (the library then takes the URL as the chat model's id); ``timeout``. ``api_key`` is the hub
    token for a hub model; for a ``base_url`` it is sent as a bearer ``authorization`` header on
    every request (the endpoint hands its token only to huggingface.co and hf.space hosts).
    The chat path sends ``max_tokens`` and its ``model_kwargs`` in each chat-completion call, so
    every generation setting goes there, and it must be a parameter
    ``huggingface_hub.InferenceClient.chat_completion`` accepts. The endpoint's own sampling and
    length defaults are not sent: only configured settings are.
    """
    from huggingface_hub import InferenceClient  # pyright: ignore[reportMissingImports]
    from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint  # pyright: ignore[reportMissingImports]

    request = parameters(InferenceClient.chat_completion) - _HUGGINGFACE_MANAGED
    chat_fields = declared_names(BaseChatModel) | _HUGGINGFACE_CHAT_FIELDS
    forwarded = {key: value for key, value in kwargs.items() if key in request}
    chat_kwargs = {key: value for key, value in kwargs.items() if key in chat_fields and key not in request}
    refuse("huggingface", [key for key in kwargs if key not in forwarded and key not in chat_kwargs])

    endpoint_kwargs: dict[str, Any] = {"endpoint_url": base_url} if base_url else {"repo_id": model}
    if api_key is not None and base_url:
        endpoint_kwargs["server_kwargs"] = {"headers": {"authorization": f"Bearer {api_key}"}}
    elif api_key is not None:
        endpoint_kwargs["huggingfacehub_api_token"] = api_key
    if timeout is not None:
        endpoint_kwargs["timeout"] = timeout
    endpoint = HuggingFaceEndpoint(**endpoint_kwargs, temperature=None, top_p=None, typical_p=None)
    chat = ChatHuggingFace(llm=endpoint, model_kwargs=forwarded, **chat_kwargs)
    # The chat model copies the endpoint's length default (``max_new_tokens``) when it has none of
    # its own; only a configured ``max_tokens`` is sent.
    chat.max_tokens = max_tokens
    return chat


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


def requires_leading_user_turn(provider: str) -> bool:
    """Whether ``provider`` needs an outgoing history whose first non-system message is a user turn.

    No installed provider package inserts a user turn or refuses an assistant-first history
    before sending, so a hosted provider's answer is its server's: each hosted entry is the
    value the real-LLM leg ``e2e/tests/agents/test_leading_user_turn_real_leg.py`` confirms
    (a bare assistant-first call accepted or refused). ``ollama`` and ``huggingface`` serve
    the model's own chat template, which decides per model, so they answer ``True``. The
    ``match`` mirrors :func:`_build_llm`'s and an unknown provider is refused loudly.
    """
    match provider:
        case "anthropic":
            return False
        case "openai" | "google" | "xai" | "mistral":
            return True
        case "ollama" | "huggingface":
            return True
    raise ValueError(f"Unsupported chat model provider: '{provider}'")


def cache_mark_key(provider: str) -> str | None:
    """The content-block key that marks a prompt-caching breakpoint for ``provider``, or ``None``.

    ``anthropic`` hoists a block's ``cache_control``; ``openai`` takes a block's
    ``prompt_cache_breakpoint``. Every other provider takes no per-block mark. The ``match``
    mirrors :func:`_build_llm`'s and an unknown provider is refused loudly.
    """
    match provider:
        case "anthropic":
            return "cache_control"
        case "openai":
            return "prompt_cache_breakpoint"
        case "mistral" | "google" | "xai" | "ollama" | "huggingface":
            return None
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
            key = cache_mark_key(provider)
            if key is None:
                raise AssertionError
            return {key: {"type": "ephemeral"}}

        case "mistral" | "openai" | "google" | "xai" | "ollama" | "huggingface":
            return None

    raise ValueError(f"Unsupported chat model provider: '{provider}'")
