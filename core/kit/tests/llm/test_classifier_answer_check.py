"""Proofs that the kit checks every classifier response against the sets its request declares.

A request declares its question names and each question's kind; a ``choice`` question
declares its labels (the keys of its ``criteria``) and a ``score`` question its levels
(``0`` to ``len(criteria) - 1``). The factory's classifier checks the provider's response
against all of them on every call. A response outside any of them raises
:class:`ClassifierAnswerError` (kind ``upstream_error``) instead of reaching the caller, and
the monitoring span records the failure. These tests run only where the ``typesafe`` extra
is installed and drive the real factory against an injected mock transport that gives each
test's scripted answers.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

pytest.importorskip("langchain_typesafe")

import httpx2
from pydantic import JsonValue
from tai42_contract.app import tai42_app
from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.monitoring import MonitoringLevel, TokenUsage

from tai42_kit.llm import classifier
from tai42_kit.llm.classifier import (
    ChoiceAnswer,
    ChoiceQuestion,
    ClassifierAnswerError,
    ClassifyRequest,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
    get_classifier,
)

pytestmark = pytest.mark.filterwarnings("ignore::langchain_core._api.beta_decorator.LangChainBetaWarning")

_LABELS: dict[str, JsonValue] = {"events": "an event", "status": "a status"}
_LEVELS: list[JsonValue] = ["low", "medium", "high"]

# Sentinel for an answer the provider leaves out.
_OMIT: Any = object()


def _request() -> ClassifyRequest:
    return ClassifyRequest(
        state={"events": ["a", "b"]},
        questions={
            "flag": NoulQuestion(instructions="Is this an alert?"),
            "lane": ChoiceQuestion(instructions="Which lane?", criteria=_LABELS),
            "level": ScoreQuestion(instructions="How severe?", criteria=_LEVELS),
        },
    )


def _choice(choice: str, probabilities: dict[str, float]) -> dict[str, Any]:
    return {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": 0.9}


def _score(
    score: float,
    legend: dict[str, JsonValue] | None = None,
    probabilities: dict[str, float] | None = None,
) -> dict[str, Any]:
    return {
        "type": "score",
        "score": score,
        "legend": legend if legend is not None else {"0": "low", "1": "medium", "2": "high"},
        "probabilities": probabilities if probabilities is not None else {"0": 0.2, "1": 0.3, "2": 0.5},
        "confidence": 0.5,
    }


def _answers(**overrides: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {
        "flag": {"type": "noul", "noul": 0.5},
        "lane": _choice("status", {"events": 0.2, "status": 0.8}),
        "level": _score(1.3),
    }
    answers.update(overrides)
    return {name: answer for name, answer in answers.items() if answer is not _OMIT}


def _built(answers: dict[str, Any]) -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        body = {"model": payload["model"], "answers": answers, "usage": {"input_tokens": 7, "output_tokens": 11}}
        return httpx2.Response(200, json=body, headers={"x-typesafe-request-id": "req-vendor"})

    transport = httpx2.MockTransport(handler)
    classifier._cached_classifier.cache_clear()
    return get_classifier(
        "typesafe",
        api_key="sk-test",
        client=httpx2.Client(transport=transport),
        async_client=httpx2.AsyncClient(transport=transport),
    )


def _invoke(built: Any) -> Any:
    return built.invoke(_request())


def _ainvoke(built: Any) -> Any:
    return asyncio.run(built.ainvoke(_request()))


_CALLS: list[Callable[[Any], Any]] = [_invoke, _ainvoke]

# Each response breaks one declared set, with the words its error must name.
_OUT_OF_SET: dict[str, tuple[dict[str, Any], list[str]]] = {
    "choice-label": (_answers(lane=_choice("alerts", {"events": 0.2, "status": 0.8})), ["'lane'", "'alerts'"]),
    "choice-probability-extra-label": (
        _answers(lane=_choice("events", {"events": 0.2, "status": 0.6, "alerts": 0.2})),
        ["'lane'", "probabilities"],
    ),
    "choice-probability-missing-label": (
        _answers(lane=_choice("events", {"events": 1.0})),
        ["'lane'", "probabilities"],
    ),
    "score-above-levels": (_answers(level=_score(2.5)), ["'level'", "2.5"]),
    "score-below-levels": (_answers(level=_score(-0.1)), ["'level'", "-0.1"]),
    "score-probability-extra-level": (
        _answers(level=_score(1.0, probabilities={"0": 0.2, "1": 0.3, "2": 0.4, "3": 0.1})),
        ["'level'", "probabilities"],
    ),
    "score-legend-missing-level": (
        _answers(level=_score(1.0, legend={"0": "low", "1": "medium"})),
        ["'level'", "legend"],
    ),
    "missing-answer": (_answers(level=_OMIT), ["'level'"]),
    "extra-answer": (_answers(other={"type": "noul", "noul": 0.1}), ["'other'"]),
    "wrong-kind": (_answers(flag=_choice("events", {"events": 0.5, "status": 0.5})), ["'flag'", "noul", "choice"]),
}


@pytest.mark.parametrize("call", _CALLS, ids=["invoke", "ainvoke"])
def test_a_response_inside_every_declared_set_is_returned(noop_monitoring: None, call: Callable[[Any], Any]) -> None:
    response = call(_built(_answers()))
    lane = response.answers["lane"]
    assert isinstance(lane, ChoiceAnswer)
    assert lane.choice == "status"
    level = response.answers["level"]
    assert isinstance(level, ScoreAnswer)
    assert level.score == 1.3


@pytest.mark.parametrize("call", _CALLS, ids=["invoke", "ainvoke"])
def test_a_score_a_rounding_step_above_the_top_level_is_returned(
    noop_monitoring: None, call: Callable[[Any], Any]
) -> None:
    # An expected value over a distribution that sums to 1 only within float rounding can
    # land one float step above the top level.
    top = math.nextafter(2.0, 3.0)
    response = call(_built(_answers(level=_score(top))))
    assert response.answers["level"].score == top


@pytest.mark.parametrize("call", _CALLS, ids=["invoke", "ainvoke"])
@pytest.mark.parametrize("case", list(_OUT_OF_SET))
def test_a_response_outside_a_declared_set_raises(noop_monitoring: None, call: Callable[[Any], Any], case: str) -> None:
    answers, named = _OUT_OF_SET[case]
    with pytest.raises(ClassifierAnswerError) as raised:
        call(_built(answers))
    message = str(raised.value)
    for word in named:
        assert word in message, message


def test_the_error_is_an_upstream_error() -> None:
    assert error_kind(ClassifierAnswerError("x")) is ErrorKind.UPSTREAM_ERROR


class _RecordingSpan:
    def __init__(self) -> None:
        self.level: MonitoringLevel | None = None
        self.status_message: str | None = None
        self.usage: TokenUsage | None = None
        self.metadata: dict[str, Any] | None = None

    def update(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class _RecordingWriter:
    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    @contextmanager
    def start_span(self, **_: Any) -> Iterator[_RecordingSpan]:
        span = _RecordingSpan()
        self.spans.append(span)
        yield span


class _FakeApp:
    def __init__(self, writer: _RecordingWriter) -> None:
        backend = type("_Monitoring", (), {"writer": writer})()
        self.monitoring = type("_Facet", (), {"active": backend})()


def test_a_response_outside_a_declared_set_records_the_span_as_an_error_with_the_vendor_call() -> None:
    writer = _RecordingWriter()
    built = _built(_answers(lane=_choice("alerts", {"events": 0.2, "status": 0.8})))
    with tai42_app.bound(_FakeApp(writer)), pytest.raises(ClassifierAnswerError):
        built.invoke(_request())
    assert len(writer.spans) == 1
    assert writer.spans[0].level is MonitoringLevel.ERROR
    assert writer.spans[0].status_message is not None
    assert "'alerts'" in writer.spans[0].status_message
    # The vendor answered and billed the call, so its usage and request id stay on the span.
    assert writer.spans[0].usage == TokenUsage(input_tokens=7, output_tokens=11)
    assert writer.spans[0].metadata == {"request_id": "req-vendor"}
