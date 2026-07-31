import asyncio
from functools import lru_cache

from langchain_core.embeddings import Embeddings

from tai42_kit.llm._secret_kwargs import KwargsCacheKey, unwrap_secret_kwargs


async def get_embedding_async(provider: str, **kwargs) -> Embeddings:
    return await asyncio.to_thread(get_embedding, provider=provider, **kwargs)


def get_embedding(provider: str, **kwargs) -> Embeddings:
    return _cached_embedding(provider, KwargsCacheKey(kwargs))


@lru_cache(maxsize=64)
def _cached_embedding(provider: str, kwargs_key: KwargsCacheKey) -> Embeddings:
    # The caller's original kwargs flow to the constructor untouched (no JSON
    # round trip); secrets are unwrapped only at this seam.
    return _build_embedding(provider, **unwrap_secret_kwargs(kwargs_key.kwargs))


def _build_embedding(provider: str, **kwargs) -> Embeddings:
    match provider:
        case "openai":
            from langchain_openai import OpenAIEmbeddings

            return OpenAIEmbeddings(**kwargs)

        case "mistral":
            from langchain_mistralai import MistralAIEmbeddings  # pyright: ignore[reportMissingImports]

            return MistralAIEmbeddings(**kwargs)

        case "google":
            from langchain_google_genai import GoogleGenerativeAIEmbeddings  # pyright: ignore[reportMissingImports]

            return GoogleGenerativeAIEmbeddings(**kwargs)

        case "huggingface":
            from langchain_huggingface import HuggingFaceEmbeddings  # pyright: ignore[reportMissingImports]

            return HuggingFaceEmbeddings(**kwargs)

        case "ollama":
            from langchain_ollama import OllamaEmbeddings  # pyright: ignore[reportMissingImports]

            return OllamaEmbeddings(**kwargs)

    raise ValueError(f"Unsupported embedding model provider: '{provider}'")
