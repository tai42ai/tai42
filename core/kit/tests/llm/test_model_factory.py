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
from typing import Annotated, Any, Literal

import pytest
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field, JsonValue, SecretStr
from typing_extensions import TypedDict

pytest.importorskip("langgraph")

from tai42_kit.llm import classifier, embedding, models
from tai42_kit.llm._secret_kwargs import KwargsCacheKey, unwrap_secret_kwargs
from tai42_kit.llm.classifier import (
    ChoiceAnswer,
    ChoiceQuestion,
    ClassifyRequest,
    ClassifyResponse,
    NoulQuestion,
    ScoreQuestion,
)

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


def test_system_prompt_cache_mark_marks_only_a_marking_provider():
    # A provider that takes an explicit system-prompt breakpoint returns the mark kwargs.
    assert models.system_prompt_cache_mark("anthropic") == {"cache_control": {"type": "ephemeral"}}
    # Every provider that caches without a mark (or does not) returns None.
    for provider in ("mistral", "openai", "google", "xai", "ollama", "huggingface"):
        assert models.system_prompt_cache_mark(provider) is None


def test_system_prompt_cache_mark_unsupported_provider_raises():
    with pytest.raises(ValueError, match="Unsupported chat model provider"):
        models.system_prompt_cache_mark("nope")


def test_system_prompt_cache_mark_is_the_exact_block_build_system_message_accepts():
    from langchain_core.messages import SystemMessage

    from tai42_kit.llm.runtime import build_system_message

    mark = models.system_prompt_cache_mark("anthropic")
    message = build_system_message("you are a helper", mark)
    assert isinstance(message, SystemMessage)
    assert message.content == [{"type": "text", "text": "you are a helper", "cache_control": {"type": "ephemeral"}}]


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


# --- classifier factory --------------------------------------------------------
#
# The typesafe branch composes RunnableLambda(to-vendor) | TypeSafeClassifier |
# RunnableLambda(to-kit), validating each kit question's JSON dump through the
# vendor's own ``Question`` union. The fake backend below exposes a real
# discriminated ``Question`` union (so the request adapter is genuinely
# exercised) and a Runnable ``TypeSafeClassifier`` whose response ``model_dump``
# yields a vendor-shaped body.

# (provider, fake module name, class attribute the branch imports)
_CLASSIFIER_PROVIDERS = [
    ("typesafe", "langchain_typesafe", "TypeSafeClassifier"),
]


# The vendor ``Question`` union constrains instructions to text or structured
# JSON, a ``choice`` to one-or-more labels, and a ``score`` to two-or-more levels;
# the fakes mirror those constraints so the request adapter is exercised faithfully.
_FakeInstructions = str | dict[str, JsonValue] | list[JsonValue]


class _FakeNoul(BaseModel):
    type: Literal["noul"] = "noul"
    instructions: _FakeInstructions
    criteria: dict[str, JsonValue] | None = None


class _FakeChoice(BaseModel):
    type: Literal["choice"] = "choice"
    instructions: _FakeInstructions
    criteria: dict[str, JsonValue] = Field(min_length=1)


class _FakeScore(BaseModel):
    type: Literal["score"] = "score"
    instructions: _FakeInstructions
    criteria: list[JsonValue] = Field(min_length=2)


_FakeQuestion = Annotated[_FakeNoul | _FakeChoice | _FakeScore, Field(discriminator="type")]


class _FakeClassifierRequest(TypedDict):
    """Stand-in for the vendor request ``TypedDict`` the branch constructs."""

    state: Any
    questions: dict[str, Any]


class _FakeClassifierResponse:
    """Stand-in for the vendor response type the branch annotates against."""


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def model_dump(self, **_):
        return self._body


def _fake_answer(question):
    if question.type == "noul":
        return {"type": "noul", "noul": 0.75}
    if question.type == "choice":
        labels = list(question.criteria)
        share = 1.0 / len(labels)
        return {
            "type": "choice",
            "choice": labels[0],
            "probabilities": dict.fromkeys(labels, share),
            "confidence": 0.9,
        }
    levels = question.criteria
    share = 1.0 / len(levels)
    return {
        "type": "score",
        "score": 1.0,
        "legend": dict(enumerate(levels)),
        "probabilities": dict.fromkeys(range(len(levels)), share),
        "confidence": 0.8,
    }


def _install_fake_typesafe(monkeypatch, module_name, class_name):
    """Install a fully synthetic ``langchain_typesafe`` package with everything the branch imports.

    The branch imports ``ClassifierRequest``, ``ClassifierResponse``, ``Question`` and
    ``TypeSafeClassifier`` from the top-level package and ``State`` from the ``types``
    submodule, so both modules are placed in ``sys.modules`` — the classifier tests then
    run with no vendor package installed.
    """
    state = {"ctor_kwargs": {}, "request": None}

    class _FakeTypeSafeClassifier(Runnable):
        def __init__(self, **kwargs):
            state["ctor_kwargs"] = kwargs

        def invoke(self, payload, config=None, **_):
            state["request"] = payload
            return _FakeResponse(
                {
                    "model": state["ctor_kwargs"].get("model", "jev-latest"),
                    "answers": {name: _fake_answer(q) for name, q in payload["questions"].items()},
                    "usage": {"input_tokens": 3, "output_tokens": 5},
                    "request_id": "req-fake",
                }
            )

        async def ainvoke(self, payload, config=None, **_):
            return self.invoke(payload, config)

    types_module = types.ModuleType(f"{module_name}.types")
    monkeypatch.setattr(types_module, "State", object, raising=False)

    package = types.ModuleType(module_name)
    monkeypatch.setattr(package, "ClassifierRequest", _FakeClassifierRequest, raising=False)
    monkeypatch.setattr(package, "ClassifierResponse", _FakeClassifierResponse, raising=False)
    monkeypatch.setattr(package, "Question", _FakeQuestion, raising=False)
    monkeypatch.setattr(package, "types", types_module, raising=False)
    monkeypatch.setattr(package, class_name, _FakeTypeSafeClassifier, raising=False)

    monkeypatch.setitem(sys.modules, module_name, package)
    monkeypatch.setitem(sys.modules, f"{module_name}.types", types_module)
    return state


def _sample_request():
    return ClassifyRequest(
        state={"events": ["a", "b"]},
        questions={
            "flag": NoulQuestion(instructions="Is this an alert?"),
            "lane": ChoiceQuestion(instructions="Which lane?", criteria={"events": "an event", "status": "a status"}),
            "severity": ScoreQuestion(instructions="How severe?", criteria=["low", "medium", "high"]),
        },
    )


@pytest.mark.parametrize(("provider", "module_name", "class_name"), _CLASSIFIER_PROVIDERS)
def test_build_classifier_composes_pipeline_and_validates_request(monkeypatch, provider, module_name, class_name):
    state = _install_fake_typesafe(monkeypatch, module_name, class_name)
    built = classifier._build_classifier(provider, model="jev-latest")

    response = built.invoke(_sample_request())

    # The vendor received the request with state passed through and questions
    # validated into vendor question objects (not raw dicts).
    assert state["request"]["state"] == {"events": ["a", "b"]}
    assert isinstance(state["request"]["questions"]["flag"], _FakeNoul)
    assert isinstance(state["request"]["questions"]["lane"], _FakeChoice)
    assert isinstance(state["request"]["questions"]["severity"], _FakeScore)

    # The vendor-shaped body is validated back into the kit contract.
    assert isinstance(response, ClassifyResponse)
    assert response.model == "jev-latest"
    assert response.answers["flag"].type == "noul"
    lane = response.answers["lane"]
    assert isinstance(lane, ChoiceAnswer)
    assert lane.choice == "events"
    assert response.answers["severity"].type == "score"
    assert response.usage.input_tokens == 3
    assert response.request_id == "req-fake"


@pytest.mark.parametrize(("provider", "module_name", "class_name"), _CLASSIFIER_PROVIDERS)
def test_build_classifier_pipeline_works_through_ainvoke(monkeypatch, provider, module_name, class_name):
    _install_fake_typesafe(monkeypatch, module_name, class_name)
    built = classifier._build_classifier(provider, model="jev-latest")

    response = asyncio.run(built.ainvoke(_sample_request()))
    assert isinstance(response, ClassifyResponse)
    assert response.answers["flag"].type == "noul"


@pytest.mark.parametrize(("provider", "module_name", "class_name"), _CLASSIFIER_PROVIDERS)
def test_build_classifier_malformed_question_raises_loudly(monkeypatch, provider, module_name, class_name):
    from pydantic import ValidationError

    _install_fake_typesafe(monkeypatch, module_name, class_name)
    built = classifier._build_classifier(provider, model="jev-latest")

    # A question whose dumped shape the vendor union cannot discriminate surfaces
    # loudly through the request adapter — never a silent drop.
    bad = ClassifyRequest.model_construct(
        state="s",
        questions={"q": NoulQuestion.model_construct(type="bogus", instructions="x", criteria=None)},
    )
    with pytest.raises(ValidationError):
        built.invoke(bad)


def test_get_classifier_unwraps_secretstr_api_key(monkeypatch):
    state = _install_fake_typesafe(monkeypatch, "langchain_typesafe", "TypeSafeClassifier")
    classifier._cached_classifier.cache_clear()

    classifier.get_classifier("typesafe", model="jev-latest", api_key=SecretStr("sk-secret"))
    assert state["ctor_kwargs"]["api_key"] == "sk-secret"
    assert not isinstance(state["ctor_kwargs"]["api_key"], SecretStr)


def test_build_classifier_unsupported_provider_raises():
    with pytest.raises(ValueError, match="Unsupported classifier provider"):
        classifier._build_classifier("nope")


def test_get_classifier_async_offloads_to_thread(monkeypatch):
    classifier._cached_classifier.cache_clear()
    sentinel = object()
    monkeypatch.setattr(classifier, "_build_classifier", lambda provider, **kw: sentinel)
    out = asyncio.run(classifier.get_classifier_async("typesafe", model="jev-latest"))
    assert out is sentinel
