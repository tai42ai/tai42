"""Embedding-model factory: build a cached langchain ``Embeddings`` client per provider.

The chat builders' rule (``llm/models.py``) holds here too: each kwarg handed to
:func:`get_embedding` reaches the provider — configured on its client or sent in its request — or
the build refuses it with ``ValueError`` naming it. No setting is dropped. What a provider carries is
read from its own embedding class: its declared fields that it applies, plus, for OpenAI (whose class
forwards ``model_kwargs`` into the SDK call), that call's parameters; a flat setting a provider takes
under another name or in its client options is mapped there, and the same setting given twice is
refused naming both.
"""

import asyncio
from functools import lru_cache
from typing import Any

from langchain_core.embeddings import Embeddings

from tai42_kit.llm._carry import carried, parameters, refuse_twice, set_client_option
from tai42_kit.llm._secret_kwargs import KwargsCacheKey, unwrap_secret_kwargs
from tai42_kit.llm.models import _ollama_client_options

_MODEL = "embedding model"


async def get_embedding_async(provider: str, **kwargs) -> Embeddings:
    """Build (or return the cached) ``Embeddings`` client for ``provider``, off the event loop."""
    return await asyncio.to_thread(get_embedding, provider=provider, **kwargs)


def get_embedding(provider: str, **kwargs) -> Embeddings:
    """Build (or return the cached) ``Embeddings`` client for ``provider`` with the given kwargs."""
    return _cached_embedding(provider, KwargsCacheKey(kwargs))


@lru_cache(maxsize=64)
def _cached_embedding(provider: str, kwargs_key: KwargsCacheKey) -> Embeddings:
    # The caller's original kwargs flow to the constructor untouched (no JSON
    # round trip); secrets are unwrapped only at this seam.
    return _build_embedding(provider, **unwrap_secret_kwargs(kwargs_key.kwargs))


def _build_embedding(provider: str, **kwargs) -> Embeddings:
    kwargs = dict(kwargs)
    match provider:
        case "openai":
            return _build_openai_embedding(kwargs)

        case "mistral":
            from langchain_mistralai import MistralAIEmbeddings  # pyright: ignore[reportMissingImports]

            # The class takes its base URL as ``endpoint``; ``max_concurrent_requests`` is declared but
            # never read by the class.
            if "base_url" in kwargs:
                if "endpoint" in kwargs:
                    refuse_twice("mistral", "base_url", "endpoint")
                kwargs["endpoint"] = kwargs.pop("base_url")
            return MistralAIEmbeddings(
                **carried(
                    "mistral",
                    kwargs,
                    MistralAIEmbeddings,
                    frozenset(),
                    unsent=frozenset({"max_concurrent_requests"}),
                    model=_MODEL,
                )
            )

        case "google":
            from langchain_google_genai import GoogleGenerativeAIEmbeddings  # pyright: ignore[reportMissingImports]

            # ``user_agent`` rides the client's additional headers; ``request_options`` is declared
            # but never applied by the class.
            _user_agent_header("google", kwargs, ("additional_headers", "user-agent"))
            return GoogleGenerativeAIEmbeddings(
                **carried(
                    "google",
                    kwargs,
                    GoogleGenerativeAIEmbeddings,
                    frozenset(),
                    unsent=frozenset({"request_options"}),
                    model=_MODEL,
                )
            )

        case "huggingface":
            return _build_huggingface_embedding(kwargs)

        case "ollama":
            from langchain_ollama import OllamaEmbeddings  # pyright: ignore[reportMissingImports]

            # ``timeout`` and ``api_key`` (a bearer ``authorization`` header) configure its HTTP client
            # through ``client_kwargs``; the client always sets its own user agent, so a flat
            # ``user_agent`` is refused with the other undeclared names.
            _ollama_client_options("ollama", kwargs)
            return OllamaEmbeddings(**carried("ollama", kwargs, OllamaEmbeddings, frozenset(), model=_MODEL))

    raise ValueError(f"Unsupported embedding model provider: '{provider}'")


def _build_openai_embedding(kwargs: dict[str, Any]) -> Embeddings:
    """An ``OpenAIEmbeddings`` built from flat settings.

    ``user_agent`` rides the client's default headers under the SDK's own key, ``User-Agent``, which
    the SDK merges over its default with a plain (letter-case-sensitive) dict, so the request carries
    one agent line; a caller's per-call ``extra_headers`` agent (flat or in ``model_kwargs``) would set
    it a second time and is refused. ``openai_api_version`` (alias ``api_version``), ``headers``,
    ``retry_min_seconds`` and ``retry_max_seconds`` are declared but never read by the class; it fills
    ``input`` and ``model`` in each call itself.
    """
    from langchain_openai import OpenAIEmbeddings
    from openai.resources import Embeddings as EmbeddingsCall

    if "user_agent" in kwargs:
        _refuse_extra_headers_agent(kwargs)
    _user_agent_header("openai", kwargs, ("default_headers", "User-Agent"))
    forwardable = parameters(EmbeddingsCall.create) - {"input", "model"}
    unsent = frozenset({"openai_api_version", "api_version", "headers", "retry_min_seconds", "retry_max_seconds"})
    return OpenAIEmbeddings(**carried("openai", kwargs, OpenAIEmbeddings, forwardable, unsent=unsent, model=_MODEL))


def _build_huggingface_embedding(kwargs: dict[str, Any]) -> Embeddings:
    """A ``HuggingFaceEmbeddings`` built from flat settings.

    The class loads a local ``SentenceTransformer`` with its ``model_kwargs``, so a name it does not
    declare is carried when the loader takes it; ``api_key`` is the loader's hub ``token`` (for a
    private or gated model).
    """
    from langchain_huggingface import HuggingFaceEmbeddings  # pyright: ignore[reportMissingImports]

    if "api_key" in kwargs:
        if "token" in kwargs:
            refuse_twice("huggingface", "api_key", "token")
        set_client_option("huggingface", kwargs, ("model_kwargs", "token"), kwargs.pop("api_key"), flat="api_key")
    return HuggingFaceEmbeddings(
        **carried("huggingface", kwargs, HuggingFaceEmbeddings, _sentence_transformer_parameters, model=_MODEL)
    )


def _sentence_transformer_parameters(*_: Any) -> frozenset[str]:
    """What ``sentence_transformers.SentenceTransformer`` takes beside the model name and cache folder."""
    from sentence_transformers import SentenceTransformer  # pyright: ignore[reportMissingImports]

    return parameters(SentenceTransformer.__init__) - {"model_name_or_path", "cache_folder"}


def _refuse_extra_headers_agent(kwargs: dict[str, Any]) -> None:
    """Refuse a flat ``user_agent`` beside an agent in the OpenAI call's ``extra_headers``."""
    places = {
        "extra_headers": kwargs.get("extra_headers"),
        "model_kwargs['extra_headers']": (kwargs.get("model_kwargs") or {}).get("extra_headers"),
    }
    for place, headers in places.items():
        for name in headers or {}:
            if name.lower() == "user-agent":
                refuse_twice("openai", "user_agent", f"{place}[{name!r}]")


def _user_agent_header(provider: str, kwargs: dict[str, Any], path: tuple[str, ...]) -> None:
    """Move a flat ``user_agent`` to the header option at ``path`` of the provider's client."""
    if "user_agent" in kwargs:
        set_client_option(provider, kwargs, path, kwargs.pop("user_agent"), flat="user_agent", header=True)
