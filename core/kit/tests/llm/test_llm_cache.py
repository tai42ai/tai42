"""Tests for the get_llm / get_embedding process caches.

Verify the caching contract: equal (provider, kwargs) returns the same built
instance (regardless of kwarg order), distinct kwargs build distinct instances,
and the builder is invoked exactly once per distinct key. ``_build_llm`` /
``_build_embedding`` are monkeypatched so no provider package or network is hit.
"""

import pytest

pytest.importorskip("langgraph")

from pydantic import SecretStr

from tai42_kit.llm import embedding, models


def test_get_llm_same_kwargs_builds_once(monkeypatch):
    models._cached_llm.cache_clear()
    calls = []
    monkeypatch.setattr(models, "_build_llm", lambda provider, **kw: calls.append((provider, kw)) or object())

    a = models.get_llm("openai", model="m", temperature=0.0)
    b = models.get_llm("openai", temperature=0.0, model="m")  # kwargs reordered

    assert a is b
    assert len(calls) == 1  # cache hit on the reordered call


def test_get_llm_distinct_kwargs_build_distinct(monkeypatch):
    models._cached_llm.cache_clear()
    monkeypatch.setattr(models, "_build_llm", lambda provider, **kw: object())

    a = models.get_llm("openai", model="m", temperature=0.0)
    b = models.get_llm("openai", model="m", temperature=0.7)

    assert a is not b


def test_get_llm_distinct_provider_builds_distinct(monkeypatch):
    models._cached_llm.cache_clear()
    monkeypatch.setattr(models, "_build_llm", lambda provider, **kw: object())

    assert models.get_llm("openai", model="m") is not models.get_llm("mistral", model="m")


def test_get_llm_same_secret_hits_cache(monkeypatch):
    models._cached_llm.cache_clear()
    calls = []
    monkeypatch.setattr(models, "_build_llm", lambda provider, **kw: calls.append(1) or object())

    a = models.get_llm("openai", model="m", api_key=SecretStr("sk-x"))
    b = models.get_llm("openai", model="m", api_key=SecretStr("sk-x"))
    c = models.get_llm("openai", model="m", api_key=SecretStr("sk-other"))

    assert a is b  # equal secrets key the same cache entry
    assert c is not a  # a different secret splits the key
    assert len(calls) == 2


def test_get_llm_kwargs_reach_builder_without_json_roundtrip(monkeypatch):
    models._cached_llm.cache_clear()
    captured: dict = {}

    def _capture(provider, **kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(models, "_build_llm", _capture)
    models.get_llm("openai", model="m", stop=("a", "b"))
    # The builder receives the caller's original kwargs: the tuple stays a
    # tuple instead of coming back as a list from a JSON round trip.
    assert captured["stop"] == ("a", "b")
    assert isinstance(captured["stop"], tuple)


def test_get_embedding_same_kwargs_builds_once(monkeypatch):
    embedding._cached_embedding.cache_clear()
    calls = []
    monkeypatch.setattr(embedding, "_build_embedding", lambda provider, **kw: calls.append(1) or object())

    a = embedding.get_embedding("openai", model="e")
    b = embedding.get_embedding("openai", model="e")

    assert a is b
    assert len(calls) == 1
