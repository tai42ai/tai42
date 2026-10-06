"""Proofs of the classifier factory against the real ``langchain_typesafe`` package.

These tests run only where the ``typesafe`` extra is installed. They prove that the
vendor ``Question`` union accepts the kit questions' JSON dump and rejects a malformed
one, that the vendor constructor rejects an unknown keyword, that the factory's
composition round-trips through both ``invoke`` and ``ainvoke`` against an injected
mock transport and re-coerces the score legend to integer keys, and that an
unsupported root state raises the vendor's ``TypeError`` at call time.
"""

import asyncio
import json

import pytest
from pydantic import TypeAdapter, ValidationError

pytest.importorskip("langchain_typesafe")

import httpx2
from langchain_typesafe import Question

from tai42_kit.llm import classifier
from tai42_kit.llm.classifier import (
    ChoiceAnswer,
    ChoiceQuestion,
    ClassifyRequest,
    ClassifyResponse,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
    get_classifier,
)

# ``TypeSafeClassifier`` is marked ``@beta`` by the vendor and warns on construction;
# these tests build it on purpose, so that warning is not an error here.
pytestmark = pytest.mark.filterwarnings("ignore::langchain_core._api.beta_decorator.LangChainBetaWarning")


def _sample_request() -> ClassifyRequest:
    return ClassifyRequest(
        state={"events": ["a", "b"]},
        questions={
            "flag": NoulQuestion(instructions="Is this an alert?"),
            "lane": ChoiceQuestion(instructions="Which lane?", criteria={"events": "an event", "status": "a status"}),
            "severity": ScoreQuestion(instructions="How severe?", criteria=["low", "medium", "high"]),
        },
    )


def _vendor_answer(question: dict) -> dict:
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.75}
    if question["type"] == "choice":
        labels = list(question["criteria"])
        share = 1.0 / len(labels)
        return {
            "type": "choice",
            "choice": labels[0],
            "probabilities": dict.fromkeys(labels, share),
            "confidence": 0.9,
        }
    levels = question["criteria"]
    share = 1.0 / len(levels)
    # JSON object keys are strings on the wire; the vendor and the kit both coerce
    # the score legend/probabilities back to integer levels on validation.
    return {
        "type": "score",
        "score": 1.0,
        "legend": {str(index): level for index, level in enumerate(levels)},
        "probabilities": {str(index): share for index in range(len(levels))},
        "confidence": 0.8,
    }


def _mock_handler(request: httpx2.Request) -> httpx2.Response:
    payload = json.loads(request.content)
    body = {
        "model": payload["model"],
        "answers": {name: _vendor_answer(question) for name, question in payload["questions"].items()},
        "usage": {"input_tokens": 7, "output_tokens": 11},
    }
    return httpx2.Response(200, json=body, headers={"x-typesafe-request-id": "req-vendor"})


def _mock_clients() -> dict:
    transport = httpx2.MockTransport(_mock_handler)
    return {
        "client": httpx2.Client(transport=transport),
        "async_client": httpx2.AsyncClient(transport=transport),
    }


def test_vendor_question_union_accepts_kit_dumps_and_rejects_malformed():
    adapter: TypeAdapter[dict[str, Question]] = TypeAdapter(dict[str, Question])
    dumped = {
        name: question.model_dump(mode="json", exclude_none=True)
        for name, question in _sample_request().questions.items()
    }

    validated = adapter.validate_python(dumped)
    assert {name: type(question).__name__ for name, question in validated.items()} == {
        "flag": "Noul",
        "lane": "Choice",
        "severity": "Score",
    }

    # A score with a single level violates the vendor's ``min_length=2`` rubric and
    # surfaces loudly rather than being silently accepted.
    with pytest.raises(ValidationError):
        adapter.validate_python({"bad": {"type": "score", "instructions": "x", "criteria": ["only-one"]}})


def test_vendor_constructor_rejects_unknown_keyword():
    # The vendor ctor is ``extra="forbid"``: a key outside its declared fields,
    # forwarded through the factory, is rejected loudly at construction rather than
    # silently dropped.
    classifier._cached_classifier.cache_clear()
    with pytest.raises(ValidationError):
        get_classifier("typesafe", api_key="sk-test", not_a_field="x")


def test_composition_round_trips_through_invoke_and_ainvoke(noop_monitoring):
    classifier._cached_classifier.cache_clear()
    built = get_classifier("typesafe", api_key="sk-test", **_mock_clients())

    for response in (built.invoke(_sample_request()), asyncio.run(built.ainvoke(_sample_request()))):
        assert isinstance(response, ClassifyResponse)
        assert response.model == "jev-latest"
        assert response.answers["flag"].type == "noul"
        lane = response.answers["lane"]
        assert isinstance(lane, ChoiceAnswer)
        assert lane.choice == "events"
        severity = response.answers["severity"]
        assert isinstance(severity, ScoreAnswer)
        assert all(isinstance(level, int) for level in severity.legend)
        assert severity.legend == {0: "low", 1: "medium", 2: "high"}
        assert response.usage.input_tokens == 7
        assert response.request_id == "req-vendor"


def test_unsupported_root_state_raises_vendor_type_error(noop_monitoring):
    classifier._cached_classifier.cache_clear()
    built = get_classifier("typesafe", api_key="sk-test", **_mock_clients())

    # The kit contract's ``state`` is any JSON value; the vendor rejects a bare
    # scalar root, and that ``TypeError`` propagates unchanged through the factory.
    with pytest.raises(TypeError):
        built.invoke(ClassifyRequest(state=42, questions={"flag": NoulQuestion(instructions="Is this an alert?")}))
