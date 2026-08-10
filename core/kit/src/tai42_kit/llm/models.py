import asyncio
from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from tai42_kit.llm._secret_kwargs import KwargsCacheKey, unwrap_secret_kwargs


async def get_llm_async(provider: str, **kwargs) -> BaseChatModel:
    return await asyncio.to_thread(get_llm, provider=provider, **kwargs)


def get_llm(provider: str, **kwargs) -> BaseChatModel:
    return _cached_llm(provider, KwargsCacheKey(kwargs))


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
