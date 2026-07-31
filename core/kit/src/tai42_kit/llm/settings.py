"""LLM + context-middleware settings, co-located with the llm impl.

``api_key`` is a ``SecretStr``, so it stays masked in repr/logs/tracebacks. It is
consumed as one ``model_dump -> **kwargs -> get_llm/get_embedding`` bundle; those
builders unwrap any ``SecretStr`` kwarg at their seam, so the plaintext is
revealed from the unwrap onward (the JSON cache key and provider construction) —
masked everywhere upstream of that seam. The LangGraph ``checkpoint_conn_string``
/ ``store_conn_string`` stay opaque connection URLs (addresses, not discrete
secrets) handed straight to the LangGraph savers.
"""

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import TaiBaseSettings, settings_cache

ContextOverflowMethod = Literal["trimming", "summarization", "context_editing"]


class TrimmingMiddlewareSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRIMMING_MIDDLEWARE_")

    strategy: Literal["first", "last"] = "last"
    max_tokens: int = 5000
    include_system: bool = True
    allow_partial: bool = False
    end_on: str | Sequence[str] | None = None
    start_on: str | Sequence[str] | None = None


class ContextOverflowSettings(TaiBaseSettings):
    """Selects how the conversation is reduced when it grows past the limit.

    ``methods`` may list one or more strategies (parsed as JSON from the env
    var, e.g. ``CONTEXT_OVERFLOW_METHODS=["context_editing","summarization"]``).
    Multiple strategies are applied in a fixed order (cheap structural edits
    before LLM summarization). ``trimming`` is destructive (drops whole
    messages) so it cannot be combined with strategies that depend on that
    history surviving.
    """

    model_config = SettingsConfigDict(env_prefix="CONTEXT_OVERFLOW_")

    methods: list[ContextOverflowMethod] = ["trimming"]  # noqa: RUF012

    @field_validator("methods")
    @classmethod
    def _normalize_methods(cls, value: list[str]) -> list[str]:
        # Silently normalize harmless input: drop duplicates (order-preserving).
        # An empty list is allowed and means "no context management".
        deduped: list[str] = []
        for method in value:
            if method not in deduped:
                deduped.append(method)
        # Genuine conflict, not normalizable: trimming deletes whole messages,
        # discarding the history the other strategies need -> surface it.
        if "trimming" in deduped and len(deduped) > 1:
            raise ValueError(
                "'trimming' cannot be composed with other methods: it drops whole "
                "messages, discarding the history the other strategies operate on"
            )
        return deduped


class SummarizationMiddlewareSettings(TaiBaseSettings):
    """Behavior of the summarization context-overflow strategy."""

    model_config = SettingsConfigDict(env_prefix="SUMMARIZATION_MIDDLEWARE_")

    trigger_tokens: int = 5000  # summarize once history crosses this many tokens
    keep_messages: int = 20  # most recent messages kept verbatim
    trim_tokens_to_summarize: int = 4000  # cap on what is fed into the summary call
    summary_prompt: str | None = None  # None -> built-in default prompt


class ContextEditingSettings(TaiBaseSettings):
    """Behavior of the context-editing strategy (clears stale tool outputs).

    Mirrors LangChain's ``ClearToolUsesEdit`` (Anthropic-style context editing):
    once the history crosses ``trigger_tokens`` the oldest tool results are
    replaced with a placeholder, preserving the ``keep_tool_results`` most
    recent ones."""

    model_config = SettingsConfigDict(env_prefix="CONTEXT_EDITING_")

    trigger_tokens: int = 100_000  # token count that triggers clearing
    keep_tool_results: int = 3  # most recent tool results preserved
    clear_at_least: int = 0  # minimum tokens to reclaim per run
    clear_tool_inputs: bool = False  # also clear the originating tool-call args
    exclude_tools: Sequence[str] = ()  # tool names never cleared


class EmbeddingSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMBEDDING_")

    model: str = "text-embedding-3-small"  # Required by all providers
    base_url: str | None = None  # Used by OpenAI (self-hosted), Mistral, Ollama
    api_key: SecretStr | None = None  # Used by OpenAI, Google, Mistral
    api_version: str | None = None  # Optional, used by OpenAI, Mistral
    timeout: int | None = None  # Optional for all
    user_agent: str | None = None  # Used by HuggingFace (optional header)
    task: str | None = None  # HuggingFace-specific ("feature-extraction", etc.)
    model_kwargs: dict[str, Any] | None = None  # HuggingFace, Ollama: optional params


class LLMSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_")
    model: str = "gpt-4o"  # Common to all
    temperature: float = 0.0  # Common to all
    max_tokens: int | None = None  # Common to most, optional in case default is used
    timeout: int | None = None  # Optional but supported by most
    base_url: str | None = None  # Ollama, OpenAI (self-hosted), Mistral
    api_key: SecretStr | None = None  # OpenAI, Anthropic, Groq, etc.
    api_version: str | None = None  # Optional, used in some providers (OpenAI, Mistral)
    top_p: float | None = None  # Optional sampling param (OpenAI, Anthropic)
    top_k: int | None = None  # Mistral (optional)
    n: int | None = None  # Optional: number of completions (OpenAI)


class LLMProviderSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_PROVIDER_")

    llm: str = "openai"
    embedding: str = "openai"
    checkpoint: str = "redis"
    checkpoint_conn_string: str = "redis://localhost:6379/0"
    checkpoint_ttl_minutes: int | None = None  # idle-TTL (minutes); None = keep forever
    store: str = "redis"
    store_conn_string: str = "redis://localhost:6379/0"

    @field_validator("checkpoint_ttl_minutes")
    @classmethod
    def _validate_checkpoint_ttl_minutes(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError(
                "checkpoint_ttl_minutes must be a positive number of minutes; "
                "leave it unset to keep checkpoints forever"
            )
        return value


@settings_cache
def trimming_middleware_settings() -> TrimmingMiddlewareSettings:
    return TrimmingMiddlewareSettings()


@settings_cache
def context_overflow_settings() -> ContextOverflowSettings:
    return ContextOverflowSettings()


@settings_cache
def summarization_middleware_settings() -> SummarizationMiddlewareSettings:
    return SummarizationMiddlewareSettings()


@settings_cache
def context_editing_settings() -> ContextEditingSettings:
    return ContextEditingSettings()


@settings_cache
def llm_provider_settings() -> LLMProviderSettings:
    return LLMProviderSettings()


@settings_cache
def llm_settings() -> LLMSettings:
    return LLMSettings()


@settings_cache
def embedding_settings() -> EmbeddingSettings:
    return EmbeddingSettings()
