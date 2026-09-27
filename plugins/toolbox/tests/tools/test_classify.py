"""Tests for the ``classify`` tool: provider selection, settings fallbacks, the
typed request/response round trip against a fake classifier, the discriminated
question schema, registration, and the missing-extra install hint."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from typing import Any

import pytest
from fastmcp.utilities.types import get_cached_typeadapter
from pydantic import JsonValue, ValidationError
from tai42_kit.llm.classifier import ClassifyQuestion, ClassifyResponse, NoulAnswer, NoulQuestion
from tai42_kit.llm.settings import ClassifierSettings

from tai42_toolbox.tools.classify import classify

_STATE: JsonValue = {"event": "status_changed", "level": "info"}
_QUESTIONS: dict[str, ClassifyQuestion] = {"is_alert": NoulQuestion(instructions="Is this an alert?")}
_RESPONSE = ClassifyResponse(model="jev-latest", answers={"is_alert": NoulAnswer(type="noul", noul=0.75)})


class _FakeClassifier:
    """A fake classifier runnable that records the request it is invoked with and
    returns a fixed :class:`ClassifyResponse`."""

    def __init__(self) -> None:
        self.received: Any = None

    async def ainvoke(self, request: Any) -> ClassifyResponse:
        self.received = request
        return _RESPONSE


def test_classify_uses_the_configured_provider_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_get_classifier_async(provider: str, **kwargs: Any) -> _FakeClassifier:
        captured["provider"] = provider
        return _FakeClassifier()

    monkeypatch.setattr("tai42_toolbox.tools.classify.get_classifier_async", fake_get_classifier_async)

    asyncio.run(classify(_STATE, _QUESTIONS))
    # The default provider comes from llm_provider_settings().classifier.
    assert captured["provider"] == "typesafe"


def test_classify_honours_an_explicit_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_get_classifier_async(provider: str, **kwargs: Any) -> _FakeClassifier:
        captured["provider"] = provider
        return _FakeClassifier()

    monkeypatch.setattr("tai42_toolbox.tools.classify.get_classifier_async", fake_get_classifier_async)

    asyncio.run(classify(_STATE, _QUESTIONS, classifier_provider="other"))
    assert captured["provider"] == "other"


def test_classifier_settings_flow_into_the_factory_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_get_classifier_async(provider: str, **kwargs: Any) -> _FakeClassifier:
        captured["kwargs"] = kwargs
        return _FakeClassifier()

    # A CLASSIFIER_* env value binds onto the configured settings and must reach
    # the provider factory as a kwarg. Read settings fresh so the env is honoured
    # rather than a process-cached instance.
    monkeypatch.setenv("CLASSIFIER_BASE_URL", "http://classifier.example")
    monkeypatch.setattr("tai42_toolbox.tools.classify.classifier_settings", lambda: ClassifierSettings())
    monkeypatch.setattr("tai42_toolbox.tools.classify.get_classifier_async", fake_get_classifier_async)

    asyncio.run(classify(_STATE, _QUESTIONS))
    assert captured["kwargs"]["base_url"] == "http://classifier.example"
    # The configured model default also flows through.
    assert captured["kwargs"]["model"] == "jev-latest"


def test_classifier_kwargs_win_over_the_configured_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_get_classifier_async(provider: str, **kwargs: Any) -> _FakeClassifier:
        captured["kwargs"] = kwargs
        return _FakeClassifier()

    monkeypatch.setenv("CLASSIFIER_MODEL", "from-settings")
    monkeypatch.setattr("tai42_toolbox.tools.classify.classifier_settings", lambda: ClassifierSettings())
    monkeypatch.setattr("tai42_toolbox.tools.classify.get_classifier_async", fake_get_classifier_async)

    asyncio.run(classify(_STATE, _QUESTIONS, classifier_kwargs={"model": "from-caller"}))
    assert captured["kwargs"]["model"] == "from-caller"


def test_classify_returns_the_typed_response_from_the_runnable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClassifier()

    async def fake_get_classifier_async(provider: str, **kwargs: Any) -> _FakeClassifier:
        return fake

    monkeypatch.setattr("tai42_toolbox.tools.classify.get_classifier_async", fake_get_classifier_async)

    result = asyncio.run(classify(_STATE, _QUESTIONS))
    # The tool returns exactly the ClassifyResponse the runnable produced.
    assert result is _RESPONSE
    # The runnable was invoked with a ClassifyRequest carrying the passed state and questions.
    assert fake.received.state == _STATE
    assert fake.received.questions == _QUESTIONS


def test_empty_questions_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_classifier_async(provider: str, **kwargs: Any) -> _FakeClassifier:
        return _FakeClassifier()

    monkeypatch.setattr("tai42_toolbox.tools.classify.get_classifier_async", fake_get_classifier_async)

    # ClassifyRequest.questions is min_length=1, so an empty mapping is a loud
    # validation error rather than an empty classification.
    with pytest.raises(ValidationError):
        asyncio.run(classify(_STATE, {}))


def test_questions_schema_carries_the_discriminated_union() -> None:
    # The tool adapter derives the presented input schema via a pydantic TypeAdapter
    # over the function signature, so ``questions`` carries the three-variant union
    # discriminated on ``type``.
    schema = get_cached_typeadapter(classify).json_schema()
    question_schema = schema["properties"]["questions"]["additionalProperties"]
    assert question_schema["discriminator"]["propertyName"] == "type"
    assert len(question_schema["oneOf"]) == 3


def test_registration(load_registrations: Callable[[str], Any], null_app: Any) -> None:
    app = load_registrations("tai42_toolbox.tools.classify")
    assert set(app.tools.registered) == {"classify"}
    # The tool is tagged for the classifier group.
    assert null_app.tools.tags["classify"] == {"classifier"}


def test_missing_extra_raises_install_hint(
    force_missing_import: Callable[[str, list[str]], None],
) -> None:
    force_missing_import("langchain_core", ["tai42_toolbox.tools.classify", "tai42_kit.llm.classifier"])
    with pytest.raises(ImportError, match=r"tai42-toolbox\[classifier\]"):
        importlib.import_module("tai42_toolbox.tools.classify")
