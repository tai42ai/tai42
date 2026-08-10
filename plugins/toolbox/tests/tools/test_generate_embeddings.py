"""Tests for the ``generate_embeddings`` tool: generation against a mocked
provider, registration, and the missing-extra install hint."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from typing import Any

import pytest

from tai42_toolbox.tools.generate_embeddings import generate_embeddings


class _FakeEmbeddingModel:
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]


class _SyncOnlyEmbeddingModel:
    """A model exposing only the sync embed call, to prove the tool never falls
    back to it when the async call is absent."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]


def test_generate_embeddings_wraps_single_string(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_get_embedding_async(provider: str, **kwargs: Any) -> _FakeEmbeddingModel:
        captured["provider"] = provider
        captured["kwargs"] = kwargs
        return _FakeEmbeddingModel()

    monkeypatch.setattr("tai42_toolbox.tools.generate_embeddings.get_embedding_async", fake_get_embedding_async)

    result = asyncio.run(generate_embeddings("hello", embedding_provider="openai"))
    assert result == [[5.0, 1.0]]
    assert captured["provider"] == "openai"
    # The configured embedding settings flow through as provider kwargs.
    assert captured["kwargs"]["model"]


def test_generate_embeddings_handles_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_embedding_async(provider: str, **kwargs: Any) -> _FakeEmbeddingModel:
        return _FakeEmbeddingModel()

    monkeypatch.setattr("tai42_toolbox.tools.generate_embeddings.get_embedding_async", fake_get_embedding_async)

    result = asyncio.run(generate_embeddings(["a", "bb", "ccc"], embedding_provider="openai"))
    assert result == [[1.0, 1.0], [2.0, 1.0], [3.0, 1.0]]


def test_generate_embeddings_requires_async_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_embedding_async(provider: str, **kwargs: Any) -> _SyncOnlyEmbeddingModel:
        return _SyncOnlyEmbeddingModel()

    monkeypatch.setattr("tai42_toolbox.tools.generate_embeddings.get_embedding_async", fake_get_embedding_async)

    with pytest.raises(AttributeError, match="aembed_documents"):
        asyncio.run(generate_embeddings("hello", embedding_provider="openai"))


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_toolbox.tools.generate_embeddings")
    assert set(app.tools.registered) == {"generate_embeddings"}


def test_missing_extra_raises_install_hint(
    force_missing_import: Callable[[str, list[str]], None],
) -> None:
    force_missing_import("langchain_core", ["tai42_toolbox.tools.generate_embeddings", "tai42_kit.llm.embedding"])
    with pytest.raises(ImportError, match=r"tai42-toolbox\[embeddings\]"):
        importlib.import_module("tai42_toolbox.tools.generate_embeddings")
