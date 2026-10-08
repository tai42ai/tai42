"""The handler's remaining run kinds and failure paths, driven through its callback methods directly."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.documents import Document
from langchain_core.outputs import Generation, LLMResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_contract.monitoring import MonitoringLevel, SpanKind, TraceContext

from tai42_kit.llm.monitoring_callbacks import MonitoringCallbackHandler, _run_name, _same, _shallow_members
from tai42_kit.monitoring.otel import OtelWriter
from tai42_kit.monitoring.testing import span_to_observation

_TRACE = "0af7651916cd43dd8448eb211c80319c"


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter) -> Iterator[OtelWriter]:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    w = OtelWriter(exporter=exporter)
    yield w
    w.shutdown()


@pytest.fixture
def handler(writer: OtelWriter) -> MonitoringCallbackHandler:
    return MonitoringCallbackHandler(writer, TraceContext(trace_id=_TRACE))


def _records(writer: OtelWriter, exporter: InMemorySpanExporter) -> dict[str, Any]:
    writer.flush()
    return {s.name: span_to_observation(s) for s in exporter.get_finished_spans()}


def test_completion_model_run_records_prompts_params_tools_and_llm_output_usage(
    handler: MonitoringCallbackHandler, writer: OtelWriter, exporter: InMemorySpanExporter
):
    run_id = uuid.uuid4()
    handler.on_llm_start(
        {"id": ["x", "Completion"]},
        ["say hi"],
        run_id=run_id,
        invocation_params={"model_name": "m1", "temperature": 0.1, "tools": [{"name": "t"}], "_type": "x"},
    )
    handler.on_llm_end(
        LLMResult(
            generations=[[Generation(text="hi", generation_info={"finish_reason": "stop"})]],
            llm_output={
                "token_usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
                "model_name": "m1",
            },
        ),
        run_id=run_id,
    )
    record = _records(writer, exporter)["Completion"]
    assert record.input == [{"role": "user", "parts": [{"type": "text", "content": "say hi"}]}]
    assert record.output == [
        {"role": "assistant", "parts": [{"type": "text", "content": "hi"}], "finish_reason": "stop"}
    ]
    assert (record.input_tokens, record.output_tokens, record.total_tokens) == (3, 4, 7)
    assert record.metadata == {"tools": [{"name": "t"}]}
    assert record.model == "m1"


@pytest.mark.parametrize("callback", ["on_llm_error", "on_tool_error", "on_retriever_error"])
def test_failed_runs_are_recorded_at_error(
    handler: MonitoringCallbackHandler, writer: OtelWriter, exporter: InMemorySpanExporter, callback: str
):
    run_id = uuid.uuid4()
    if callback == "on_llm_error":
        handler.on_chat_model_start({"name": "run"}, [[]], run_id=run_id)
    elif callback == "on_tool_error":
        handler.on_tool_start({"name": "run"}, "raw", run_id=run_id)
    else:
        handler.on_retriever_start({"name": "run"}, "q", run_id=run_id)
    getattr(handler, callback)(RuntimeError("down"), run_id=run_id)
    record = _records(writer, exporter)["run"]
    assert (record.level, record.status_message) == (MonitoringLevel.ERROR.value, "down")


def test_retriever_run_records_query_and_documents(
    handler: MonitoringCallbackHandler, writer: OtelWriter, exporter: InMemorySpanExporter
):
    run_id = uuid.uuid4()
    handler.on_retriever_start({}, "query", run_id=run_id, name="search")
    handler.on_retriever_end([Document(page_content="doc")], run_id=run_id)
    record = _records(writer, exporter)["search"]
    assert record.kind is SpanKind.TOOL
    assert record.input == "query"
    assert record.output[0]["page_content"] == "doc"


def test_tool_run_with_a_string_input_records_it(
    handler: MonitoringCallbackHandler, writer: OtelWriter, exporter: InMemorySpanExporter
):
    run_id = uuid.uuid4()
    handler.on_tool_start({"name": "echo"}, "plain", run_id=run_id)
    handler.on_tool_end("out", run_id=run_id)
    record = _records(writer, exporter)["echo"]
    assert (record.input, record.output) == ("plain", "out")


def test_a_hidden_chain_run_is_recorded_at_debug(
    handler: MonitoringCallbackHandler, writer: OtelWriter, exporter: InMemorySpanExporter
):
    run_id = uuid.uuid4()
    handler.on_chain_start({"id": ["a", "Hidden"]}, {"x": 1}, run_id=run_id, tags=["langsmith:hidden"])
    handler.on_chain_end({"y": 2}, run_id=run_id)
    record = _records(writer, exporter)["Hidden"]
    assert record.level == MonitoringLevel.DEBUG.value
    assert record.input == {"x": 1}


def test_a_failing_callback_is_logged_and_never_raised(
    handler: MonitoringCallbackHandler, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("handler broke")

    monkeypatch.setattr(handler, "_open", broken)
    with caplog.at_level(logging.ERROR):
        handler.on_chain_start({}, {}, run_id=uuid.uuid4(), name="n")
    assert "monitoring callback on_chain_start failed" in caplog.text


def test_end_callbacks_for_an_unknown_run_record_nothing(handler: MonitoringCallbackHandler):
    run_id = uuid.uuid4()
    handler.on_llm_end(LLMResult(generations=[[]]), run_id=run_id)
    handler.on_chain_end({}, run_id=run_id)
    handler.on_retriever_end([], run_id=run_id)


def test_run_names_fall_back_through_the_serialized_id():
    assert _run_name({"id": ["a", "B"]}, {}, "chain") == "B"
    assert _run_name({"id": []}, {}, "chain") == "chain"
    assert _run_name(None, {}, "chain") == "chain"


def test_member_comparison_treats_a_raising_equality_as_changed():
    class Unequal:
        def __eq__(self, other: object) -> bool:
            raise ValueError("no comparison")

        __hash__ = object.__hash__

    assert _same(Unequal(), Unequal()) is False


def test_a_changed_list_is_written_member_wise(handler: MonitoringCallbackHandler):
    origin = ["x" * 300, 1]
    handler._register(origin, "a" * 16, "input", "/l")
    assert _shallow_members(origin) == ("x" * 300, 1)
    origin.append(2)
    form = handler._manifest(origin, "b" * 16, "output", "", 0)
    assert form[0].ref.pointer == "/l/0"
    assert form[1:] == [1, 2]
