"""Proofs that every classifier call is recorded to monitoring as a MODEL call.

A classifier is its own model kind, not a chat model, so its call never fires the
chat-model callbacks the backend records a generation from. The factory wraps the provider
pipeline in a recording seam that opens one generation span per call and records, from the
kit's own ``ClassifyResponse``, the model id, the token usage, and the provider request id;
the span's duration is the latency. These tests run only where the ``typesafe`` extra is
installed and drive the real factory against an injected mock transport, under a recording
writer bound to ``tai42_app``.

They prove: an in-run call (``invoke`` and ``ainvoke``) records one ``LLM`` span under the
run's trace id and parent anchor; a standalone call records one ``LLM`` span under a fresh
root; and a provider failure records the span as an error and re-raises the same error.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

pytest.importorskip("langchain_typesafe")

import httpx2
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import MonitoringLevel, SpanKind, TokenUsage, TraceContext, ambient_trace_context

from tai42_kit.llm import classifier
from tai42_kit.llm.classifier import (
    ChoiceQuestion,
    ClassifyRequest,
    ClassifyResponse,
    NoulQuestion,
    ScoreQuestion,
    get_classifier,
)

# ``TypeSafeClassifier`` is marked ``@beta`` by the vendor and warns on construction;
# these tests build it on purpose, so that warning is not an error here.
pytestmark = pytest.mark.filterwarnings("ignore::langchain_core._api.beta_decorator.LangChainBetaWarning")


# --- the classifier, driven against an injected mock transport -------------------------


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
        return {"type": "choice", "choice": labels[0], "probabilities": dict.fromkeys(labels, share), "confidence": 0.9}
    levels = question["criteria"]
    share = 1.0 / len(levels)
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
    return {"client": httpx2.Client(transport=transport), "async_client": httpx2.AsyncClient(transport=transport)}


def _built() -> Any:
    classifier._cached_classifier.cache_clear()
    return get_classifier("typesafe", api_key="sk-test", **_mock_clients())


# --- a recording monitoring backend bound to tai42_app ---------------------------------


class _RecordingSpan:
    def __init__(self) -> None:
        self.model: str | None = None
        self.usage: TokenUsage | None = None
        self.metadata: dict[str, Any] | None = None
        self.level: MonitoringLevel | None = None
        self.status_message: str | None = None

    @property
    def id(self) -> str:
        return "span-id"

    def update(
        self,
        *,
        output: Any = None,
        model: str | None = None,
        usage: TokenUsage | None = None,
        metadata: dict[str, Any] | None = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
    ) -> None:
        if model is not None:
            self.model = model
        if usage is not None:
            self.usage = usage
        if metadata is not None:
            self.metadata = metadata
        if level is not None:
            self.level = level
        if status_message is not None:
            self.status_message = status_message

    def set_trace_metadata(self, *, name: str | None = None, tags: list[str] | None = None) -> None:
        pass


class _OpenSpan:
    def __init__(self, name: str, kind: SpanKind, trace_context: TraceContext | None, span: _RecordingSpan) -> None:
        self.name = name
        self.kind = kind
        self.trace_context = trace_context
        self.span = span


class _RecordingWriter:
    def __init__(self) -> None:
        self.opened: list[_OpenSpan] = []

    @contextmanager
    def start_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        trace_context: TraceContext | None = None,
        input_: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[_RecordingSpan]:
        span = _RecordingSpan()
        self.opened.append(_OpenSpan(name, kind, trace_context, span))
        yield span


class _Monitoring:
    def __init__(self, writer: _RecordingWriter) -> None:
        self.writer = writer


class _MonitoringFacet:
    def __init__(self, writer: _RecordingWriter) -> None:
        self._backend = _Monitoring(writer)

    @property
    def active(self) -> _Monitoring:
        return self._backend


class _FakeApp:
    def __init__(self, writer: _RecordingWriter) -> None:
        self.monitoring = _MonitoringFacet(writer)


@pytest.fixture
def writer() -> Any:
    recording = _RecordingWriter()
    with tai42_app.bound(_FakeApp(recording)):
        yield recording


# --- the proofs -------------------------------------------------------------------------


def _assert_model_record(opened: _OpenSpan) -> None:
    assert opened.name == "classifier"
    assert opened.kind is SpanKind.LLM
    assert opened.span.model == "jev-latest"
    assert opened.span.usage == TokenUsage(input_tokens=7, output_tokens=11)
    assert opened.span.metadata == {"request_id": "req-vendor"}
    assert opened.span.level is None


def test_in_run_invoke_records_one_llm_span_under_the_run_trace(writer: _RecordingWriter) -> None:
    built = _built()
    with ambient_trace_context(TraceContext(trace_id="run-trace", parent_span_id="run-root")):
        response = built.invoke(_sample_request())
    assert isinstance(response, ClassifyResponse)
    assert len(writer.opened) == 1
    opened = writer.opened[0]
    assert opened.trace_context is not None
    assert opened.trace_context.trace_id == "run-trace"
    assert opened.trace_context.parent_span_id == "run-root"
    _assert_model_record(opened)


def test_in_run_ainvoke_records_one_llm_span_under_the_run_trace(writer: _RecordingWriter) -> None:
    built = _built()

    async def _run() -> ClassifyResponse:
        with ambient_trace_context(TraceContext(trace_id="run-trace", parent_span_id="run-root")):
            return await built.ainvoke(_sample_request())

    response = asyncio.run(_run())
    assert isinstance(response, ClassifyResponse)
    assert len(writer.opened) == 1
    opened = writer.opened[0]
    assert opened.trace_context is not None
    assert opened.trace_context.trace_id == "run-trace"
    assert opened.trace_context.parent_span_id == "run-root"
    _assert_model_record(opened)


def test_standalone_call_records_one_llm_span_under_a_fresh_root(writer: _RecordingWriter) -> None:
    built = _built()
    built.invoke(_sample_request())
    assert len(writer.opened) == 1
    ctx = writer.opened[0].trace_context
    assert ctx is not None
    assert ctx.trace_id is not None
    assert len(ctx.trace_id) == 32
    assert "-" not in ctx.trace_id
    assert ctx.parent_span_id is None
    _assert_model_record(writer.opened[0])


def test_provider_failure_records_the_span_error_and_reraises(writer: _RecordingWriter) -> None:
    built = _built()
    bad = ClassifyRequest(state=42, questions={"flag": NoulQuestion(instructions="Is this an alert?")})
    with pytest.raises(TypeError):
        built.invoke(bad)
    assert len(writer.opened) == 1
    span = writer.opened[0].span
    assert span.level is MonitoringLevel.ERROR
    assert span.status_message
    # A failed call carries no model/usage record.
    assert span.model is None
    assert span.usage is None
