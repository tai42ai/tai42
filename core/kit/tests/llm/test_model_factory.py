"""Provider-selection + factory logic for _build_llm / _build_embedding.

Each non-openai provider's backend is a heavy optional extra, so the per-provider
match branch is exercised by injecting a fake backend module into sys.modules —
this proves the selection wiring (right class, kwargs forwarded) without the real
package or any API key. The openai branch (core dep) is built for real.
"""

import asyncio
import hashlib
import sys
import types

import pytest
from pydantic import SecretStr

pytest.importorskip("langgraph")

from tai42_kit.llm import embedding, models
from tai42_kit.llm._secret_kwargs import KwargsCacheKey, unwrap_secret_kwargs

# (provider, fake module name, class attribute the branch imports)
_LLM_PROVIDERS = [
    ("anthropic", "langchain_anthropic", "ChatAnthropic"),
    ("mistral", "langchain_mistralai", "ChatMistralAI"),
    ("google", "langchain_google_genai", "ChatGoogleGenerativeAI"),
    ("xai", "langchain_xai", "ChatXAI"),
    ("ollama", "langchain_ollama", "ChatOllama"),
    ("huggingface", "langchain_huggingface", "ChatHuggingFace"),
]

_EMBEDDING_PROVIDERS = [
    ("mistral", "langchain_mistralai", "MistralAIEmbeddings"),
    ("google", "langchain_google_genai", "GoogleGenerativeAIEmbeddings"),
    ("huggingface", "langchain_huggingface", "HuggingFaceEmbeddings"),
    ("ollama", "langchain_ollama", "OllamaEmbeddings"),
]


def _install_fake_backend(monkeypatch, module_name: str, class_name: str):
    """Inject (or extend) a fake provider module exposing a kwarg-capturing class."""
    captured = {}

    class _FakeBackend:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._is_fake = True

    mod = sys.modules.get(module_name)
    if mod is None:
        mod = types.ModuleType(module_name)
        monkeypatch.setitem(sys.modules, module_name, mod)
    monkeypatch.setattr(mod, class_name, _FakeBackend, raising=False)
    return captured


@pytest.mark.parametrize(("provider", "module_name", "class_name"), _LLM_PROVIDERS)
def test_build_llm_selects_provider_backend(monkeypatch, provider, module_name, class_name):
    captured = _install_fake_backend(monkeypatch, module_name, class_name)
    built = models._build_llm(provider, model="m", temperature=0.5)
    assert getattr(built, "_is_fake", False) is True
    assert captured == {"model": "m", "temperature": 0.5}


def test_build_llm_openai_branch_builds_real_chatopenai():
    from langchain_openai import ChatOpenAI

    built = models._build_llm("openai", model="gpt-4o", api_key="sk-test")
    assert isinstance(built, ChatOpenAI)


def test_build_llm_unsupported_provider_raises():
    with pytest.raises(ValueError, match="Unsupported chat model provider"):
        models._build_llm("nope")


def test_get_llm_async_offloads_to_thread(monkeypatch):
    models._cached_llm.cache_clear()
    sentinel = object()
    monkeypatch.setattr(models, "_build_llm", lambda provider, **kw: sentinel)
    out = asyncio.run(models.get_llm_async("openai", model="m"))
    assert out is sentinel


def test_unwrap_secret_kwargs_reveals_only_secrets():
    out = unwrap_secret_kwargs({"api_key": SecretStr("sk-x"), "model": "m", "n": 3})
    assert out == {"api_key": "sk-x", "model": "m", "n": 3}
    assert not isinstance(out["api_key"], SecretStr)


def test_cache_key_masks_secret_with_sha256_digest():
    key = KwargsCacheKey({"api_key": SecretStr("sk-secret"), "model": "m"})
    # No plaintext in the key string; the secret appears only as its digest.
    assert "sk-secret" not in key._key
    assert hashlib.sha256(b"sk-secret").hexdigest() in key._key
    # The original kwargs are carried untouched (secret still wrapped) so the
    # constructor seam — not the key — reveals it.
    assert isinstance(key.kwargs["api_key"], SecretStr)


def test_cache_key_equality_follows_secret_plaintext():
    a = KwargsCacheKey({"api_key": SecretStr("sk-1"), "model": "m"})
    b = KwargsCacheKey({"model": "m", "api_key": SecretStr("sk-1")})  # reordered
    c = KwargsCacheKey({"api_key": SecretStr("sk-2"), "model": "m"})
    assert a == b
    assert hash(a) == hash(b)
    assert a != c


def test_get_llm_unwraps_secretstr_api_key(monkeypatch):
    # A SecretStr api_key (as it arrives from settings.model_dump) must reach
    # the provider as plaintext, not a SecretStr.
    models._cached_llm.cache_clear()
    captured: dict = {}

    def _capture(provider, **kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(models, "_build_llm", _capture)
    models.get_llm("openai", model="m", api_key=SecretStr("sk-secret"))
    assert captured["api_key"] == "sk-secret"
    assert not isinstance(captured["api_key"], SecretStr)


def test_get_embedding_unwraps_secretstr_api_key(monkeypatch):
    embedding._cached_embedding.cache_clear()
    captured: dict = {}

    def _capture(provider, **kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(embedding, "_build_embedding", _capture)
    embedding.get_embedding("openai", model="e", api_key=SecretStr("sk-secret"))
    assert captured["api_key"] == "sk-secret"
    assert not isinstance(captured["api_key"], SecretStr)


@pytest.mark.parametrize(("provider", "module_name", "class_name"), _EMBEDDING_PROVIDERS)
def test_build_embedding_selects_provider_backend(monkeypatch, provider, module_name, class_name):
    captured = _install_fake_backend(monkeypatch, module_name, class_name)
    built = embedding._build_embedding(provider, model="e")
    assert getattr(built, "_is_fake", False) is True
    assert captured == {"model": "e"}


def test_build_embedding_openai_branch_builds_real():
    from langchain_openai import OpenAIEmbeddings

    built = embedding._build_embedding("openai", model="text-embedding-3-small", api_key="sk-test")
    assert isinstance(built, OpenAIEmbeddings)


def test_build_embedding_unsupported_provider_raises():
    with pytest.raises(ValueError, match="Unsupported embedding model provider"):
        embedding._build_embedding("nope")


def test_get_embedding_async_offloads_to_thread(monkeypatch):
    embedding._cached_embedding.cache_clear()
    sentinel = object()
    monkeypatch.setattr(embedding, "_build_embedding", lambda provider, **kw: sentinel)
    out = asyncio.run(embedding.get_embedding_async("openai", model="e"))
    assert out is sentinel
