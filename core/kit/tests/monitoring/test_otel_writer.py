"""``OtelWriter``: every attribute of the record schema, trace placement, lifecycle and fork counting."""

from __future__ import annotations

import contextvars
import logging
import os
import weakref
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as semconv
from opentelemetry.trace import StatusCode
from tai42_contract.monitoring import (
    RUN_VERSION_METADATA_KEY,
    STEP_ROLE_METADATA_KEY,
    TIMING_ABSENT,
    TIMING_METADATA_KEY,
    MonitoringExportHealth,
    MonitoringLevel,
    RecordId,
    SpanKind,
    StepRole,
    TokenUsage,
    TraceContext,
)
from tai42_contract.secrets import SecretValue

from tai42_kit.monitoring import payload_ref
from tai42_kit.monitoring.otel import OtelWriter
from tai42_kit.monitoring.otel import attributes as attr

from .conftest import BlockingExporter, ReusableExporter, attrs, decoded, finished, only

_TRACE = "0af7651916cd43dd8448eb211c80319c"
_PARENT = "b7ad6b7169203331"
_FORK_WARNING = "ignore:This process .* is multi-threaded:DeprecationWarning"


# --- the attribute schema, one test per row -----------------------------------


@pytest.mark.parametrize("kind", list(SpanKind))
def test_every_record_carries_its_kind(writer: OtelWriter, exporter: InMemorySpanExporter, kind: SpanKind):
    writer.open_span(name="r", kind=kind).end()
    assert attrs(only(writer, exporter))[attr.SPAN_KIND] == kind.value


def test_llm_record(writer: OtelWriter, exporter: InMemorySpanExporter):
    messages = [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]
    span = writer.open_span(name="model", kind=SpanKind.LLM, input_=messages)
    span.update(output=[{"role": "assistant", "parts": [{"type": "text", "content": "yo"}]}])
    span.end()
    record = only(writer, exporter)
    assert attrs(record)[attr.GEN_AI_OPERATION_NAME] == "chat"
    assert decoded(record, attr.GEN_AI_INPUT_MESSAGES) == messages
    assert decoded(record, attr.GEN_AI_OUTPUT_MESSAGES)[0]["parts"][0]["content"] == "yo"
    assert attr.INPUT_VALUE not in attrs(record)


def test_tool_record(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(name="echo", kind=SpanKind.TOOL, input_={"text": "a"})
    span.update(output="a")
    span.end()
    record = only(writer, exporter)
    assert attrs(record)[attr.GEN_AI_OPERATION_NAME] == "execute_tool"
    assert attrs(record)[attr.GEN_AI_TOOL_NAME] == "echo"
    assert decoded(record, attr.GEN_AI_TOOL_CALL_ARGUMENTS) == {"text": "a"}
    assert decoded(record, attr.GEN_AI_TOOL_CALL_RESULT) == "a"


@pytest.mark.parametrize("kind", [SpanKind.CHAIN, SpanKind.EVENT])
def test_chain_and_event_records(writer: OtelWriter, exporter: InMemorySpanExporter, kind: SpanKind):
    span = writer.open_span(name="node", kind=kind, input_={"a": 1})
    span.update(output={"b": 2})
    span.end()
    record = only(writer, exporter)
    assert decoded(record, attr.INPUT_VALUE) == {"a": 1}
    assert decoded(record, attr.OUTPUT_VALUE) == {"b": 2}
    assert attr.GEN_AI_OPERATION_NAME not in attrs(record)


def test_model_at_open_and_at_update(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(name="m", kind=SpanKind.LLM, model="requested", model_parameters={"temperature": 0.2})
    span.update(model="responded")
    span.end()
    record = only(writer, exporter)
    assert attrs(record)[attr.GEN_AI_REQUEST_MODEL] == "requested"
    assert attrs(record)[attr.GEN_AI_RESPONSE_MODEL] == "responded"
    assert decoded(record, attr.MODEL_PARAMETERS) == {"temperature": 0.2}


def test_usage(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(name="m", kind=SpanKind.LLM)
    span.update(usage=TokenUsage(input_tokens=3, output_tokens=5, total_tokens=8, cost_usd=0.25))
    span.end()
    a = attrs(only(writer, exporter))
    assert (a[attr.GEN_AI_USAGE_INPUT_TOKENS], a[attr.GEN_AI_USAGE_OUTPUT_TOKENS]) == (3, 5)
    assert (a[attr.USAGE_TOTAL_TOKENS], a[attr.USAGE_COST_USD]) == (8, 0.25)


def test_partial_usage_sets_only_the_reported_fields(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(name="m", kind=SpanKind.LLM)
    span.update(usage=TokenUsage(input_tokens=3))
    span.end()
    a = attrs(only(writer, exporter))
    assert a[attr.GEN_AI_USAGE_INPUT_TOKENS] == 3
    assert attr.GEN_AI_USAGE_OUTPUT_TOKENS not in a
    assert attr.USAGE_COST_USD not in a


def test_error_level(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(name="t", kind=SpanKind.TOOL)
    span.update(level=MonitoringLevel.ERROR, status_message="boom")
    span.end()
    record = only(writer, exporter)
    assert record.status.status_code is StatusCode.ERROR
    assert record.status.description == "boom"
    assert attrs(record)[attr.LEVEL] == "ERROR"
    assert attrs(record)[attr.STATUS_MESSAGE] == "boom"


@pytest.mark.parametrize("level", [MonitoringLevel.DEBUG, MonitoringLevel.WARNING])
def test_debug_and_warning_levels(writer: OtelWriter, exporter: InMemorySpanExporter, level: MonitoringLevel):
    span = writer.open_span(name="t", kind=SpanKind.CHAIN)
    span.update(level=level)
    span.end()
    record = only(writer, exporter)
    assert attrs(record)[attr.LEVEL] == level.value
    assert record.status.status_code is StatusCode.UNSET


def test_default_level_sets_nothing(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(name="t", kind=SpanKind.CHAIN)
    span.update(level=MonitoringLevel.DEFAULT)
    span.end()
    assert attr.LEVEL not in attrs(only(writer, exporter))


def test_non_error_status_message(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(name="t", kind=SpanKind.CHAIN)
    span.update(status_message="note")
    span.end()
    record = only(writer, exporter)
    assert attrs(record)[attr.STATUS_MESSAGE] == "note"
    assert record.status.status_code is StatusCode.UNSET


def test_promoted_metadata_keys_are_own_attributes(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(
        name="n",
        kind=SpanKind.CHAIN,
        metadata={STEP_ROLE_METADATA_KEY: StepRole.GROUPING, TIMING_METADATA_KEY: TIMING_ABSENT, "k": 1},
    )
    span.end()
    a = attrs(only(writer, exporter))
    assert a[STEP_ROLE_METADATA_KEY] == "grouping"
    assert a[TIMING_METADATA_KEY] == "absent"
    assert orjson.loads(a[attr.METADATA]) == {"k": 1}


def test_metadata_is_one_attribute_merged_key_wise(writer: OtelWriter, exporter: InMemorySpanExporter):
    with writer.start_span(name="n", kind=SpanKind.CHAIN, metadata={"a": 1, "b": 1}) as span:
        span.update(metadata={"b": 2})
        writer.update_current_span(metadata={"c": 3})
    a = attrs(only(writer, exporter))
    assert orjson.loads(a[attr.METADATA]) == {"a": 1, "b": 2, "c": 3}
    assert not [k for k in a if k.startswith("tai42.metadata.")]


def test_no_metadata_no_attribute(writer: OtelWriter, exporter: InMemorySpanExporter):
    writer.open_span(name="n", kind=SpanKind.CHAIN).end()
    assert attr.METADATA not in attrs(only(writer, exporter))


def test_attribution_scope(writer: OtelWriter, exporter: InMemorySpanExporter):
    with (
        writer.trace_attributes(
            name="outer", tags=["a", "b"], metadata={RUN_VERSION_METADATA_KEY: 3, "x": 1}, user_id="u1"
        ),
        writer.trace_attributes(
            name="inner", tags=["b", "c"], metadata={RUN_VERSION_METADATA_KEY: 9, "x": 2}, session_id="s1"
        ),
    ):
        writer.open_span(name="n", kind=SpanKind.CHAIN).end()
    a = attrs(only(writer, exporter))
    assert a[attr.TRACE_NAME] == "inner"
    assert orjson.loads(a[attr.TRACE_TAGS]) == ["a", "b", "c"]
    assert orjson.loads(a[attr.TRACE_METADATA]) == {"x": 2}
    assert (a[attr.USER_ID], a[attr.SESSION_ID]) == ("u1", "s1")
    assert a[attr.RUN_VERSION] == "3"


def test_attribution_stamps_the_current_span_at_entry(writer: OtelWriter, exporter: InMemorySpanExporter):
    with writer.start_span(name="root", kind=SpanKind.CHAIN), writer.trace_attributes(name="event: t", tags=["t"]):
        pass
    a = attrs(only(writer, exporter))
    assert a[attr.TRACE_NAME] == "event: t"
    assert orjson.loads(a[attr.TRACE_TAGS]) == ["t"]


def test_no_attribution_outside_a_scope(writer: OtelWriter, exporter: InMemorySpanExporter):
    writer.open_span(name="n", kind=SpanKind.CHAIN).end()
    assert not [k for k in attrs(only(writer, exporter)) if k.startswith("tai42.trace.")]


def test_set_trace_metadata(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(name="n", kind=SpanKind.CHAIN)
    span.set_trace_metadata(name="named", tags=["t1"])
    span.end()
    a = attrs(only(writer, exporter))
    assert a[attr.TRACE_NAME] == "named"
    assert orjson.loads(a[attr.TRACE_TAGS]) == ["t1"]


def test_resource(monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "svc")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment.name=from-env,extra=1")
    w = OtelWriter(exporter=exporter, resource_attributes={"deployment.environment.name": "explicit"})
    try:
        w.open_span(name="n", kind=SpanKind.CHAIN).end()
        resource = only(w, exporter).resource.attributes
    finally:
        w.shutdown()
    assert resource["service.name"] == "svc"
    assert resource["deployment.environment.name"] == "explicit"
    assert resource["extra"] == "1"


def test_gen_ai_names_are_the_semantic_convention_names():
    assert attr.GEN_AI_OPERATION_NAME == semconv.GEN_AI_OPERATION_NAME
    assert attr.GEN_AI_INPUT_MESSAGES == semconv.GEN_AI_INPUT_MESSAGES
    assert attr.GEN_AI_OUTPUT_MESSAGES == semconv.GEN_AI_OUTPUT_MESSAGES
    assert attr.GEN_AI_REQUEST_MODEL == semconv.GEN_AI_REQUEST_MODEL
    assert attr.GEN_AI_RESPONSE_MODEL == semconv.GEN_AI_RESPONSE_MODEL
    assert attr.GEN_AI_USAGE_INPUT_TOKENS == semconv.GEN_AI_USAGE_INPUT_TOKENS
    assert attr.GEN_AI_USAGE_OUTPUT_TOKENS == semconv.GEN_AI_USAGE_OUTPUT_TOKENS
    assert attr.GEN_AI_TOOL_NAME == semconv.GEN_AI_TOOL_NAME
    assert attr.GEN_AI_TOOL_CALL_ARGUMENTS == semconv.GEN_AI_TOOL_CALL_ARGUMENTS
    assert attr.GEN_AI_TOOL_CALL_RESULT == semconv.GEN_AI_TOOL_CALL_RESULT
    assert semconv.GenAiOperationNameValues.CHAT.value == attr.GEN_AI_OPERATION_CHAT
    assert semconv.GenAiOperationNameValues.EXECUTE_TOOL.value == attr.GEN_AI_OPERATION_EXECUTE_TOOL


# --- masking and encoding --------------------------------------------------------


def test_writer_masks_secrets_in_input_output_and_metadata(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(
        name="t", kind=SpanKind.TOOL, input_={"k": SecretValue("s1")}, metadata={"m": SecretValue("s2")}
    )
    span.update(output=[SecretValue("s3")])
    span.end()
    record = only(writer, exporter)
    text = str(attrs(record))
    assert "s1" not in text
    assert "s2" not in text
    assert "s3" not in text
    assert decoded(record, attr.GEN_AI_TOOL_CALL_ARGUMENTS) == {"k": "[secret]"}
    assert decoded(record, attr.METADATA) == {"m": "[secret]"}


def test_references_render_in_records(writer: OtelWriter, exporter: InMemorySpanExporter):
    writer.open_span(name="n", kind=SpanKind.CHAIN, input_={"r": payload_ref(_PARENT, "output")}).end()
    expected = {"r": {"$tai42_ref": {"span_id": _PARENT, "field": "output", "pointer": ""}}}
    assert decoded(only(writer, exporter), attr.INPUT_VALUE) == expected


def test_unencodable_field_is_marked_counted_and_the_span_still_recorded(
    writer: OtelWriter, exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
):
    class Opaque:
        pass

    with caplog.at_level(logging.ERROR):
        span = writer.open_span(name="n", kind=SpanKind.CHAIN, input_={"o": Opaque()})
        span.update(output={"ok": True})
        span.end()
    record = only(writer, exporter)
    assert decoded(record, attr.INPUT_VALUE) == {"$tai42_unencodable": "dict"}
    assert decoded(record, attr.OUTPUT_VALUE) == {"ok": True}
    assert writer.export_health().records_failed == 1
    assert "Opaque" in (writer.export_health().last_error or "")
    assert "could not encode" in caplog.text


def test_hand_built_marker_in_user_data_is_refused_loudly(writer: OtelWriter, exporter: InMemorySpanExporter):
    writer.open_span(name="n", kind=SpanKind.CHAIN, input_={"$tai42_ref": "x"}).end()
    assert decoded(only(writer, exporter), attr.INPUT_VALUE) == {"$tai42_unencodable": "dict"}
    assert writer.export_health().records_failed == 1


def test_ten_megabyte_value_is_not_truncated(
    monkeypatch: pytest.MonkeyPatch, writer: OtelWriter, exporter: InMemorySpanExporter
):
    monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "10")
    monkeypatch.setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "10")
    big = "x" * (10 * 1024 * 1024)
    writer.open_span(name="n", kind=SpanKind.CHAIN, input_=big).end()
    assert decoded(only(writer, exporter), attr.INPUT_VALUE) == big


def test_large_metadata_drops_no_attribute(writer: OtelWriter, exporter: InMemorySpanExporter):
    writer.open_span(name="n", kind=SpanKind.CHAIN, metadata={f"k{i}": i for i in range(130)}).end()
    record = only(writer, exporter)
    assert record.dropped_attributes == 0
    assert len(decoded(record, attr.METADATA)) == 130
    assert writer.export_health().attributes_dropped == 0


# --- trace placement ---------------------------------------------------------------


def test_explicit_parent(writer: OtelWriter, exporter: InMemorySpanExporter):
    writer.open_span(
        name="n", kind=SpanKind.CHAIN, trace_context=TraceContext(trace_id=_TRACE, parent_span_id=_PARENT)
    ).end()
    record = only(writer, exporter)
    assert record.context is not None
    assert record.parent is not None
    assert format(record.context.trace_id, "032x") == _TRACE
    assert format(record.parent.span_id, "016x") == _PARENT


def test_chosen_trace_without_parent_is_a_root_of_that_trace(writer: OtelWriter, exporter: InMemorySpanExporter):
    writer.open_span(name="n", kind=SpanKind.CHAIN, trace_context=TraceContext(trace_id=_TRACE)).end()
    writer.open_span(name="m", kind=SpanKind.CHAIN).end()
    chosen, random_root = finished(writer, exporter)
    assert chosen.context is not None
    assert random_root.context is not None
    assert format(chosen.context.trace_id, "032x") == _TRACE
    assert chosen.parent is None
    assert format(random_root.context.trace_id, "032x") != _TRACE


def test_explicit_trace_ignores_the_ambient_span(writer: OtelWriter, exporter: InMemorySpanExporter):
    with writer.start_span(name="ambient", kind=SpanKind.CHAIN):
        writer.open_span(name="n", kind=SpanKind.CHAIN, trace_context=TraceContext(trace_id=_TRACE)).end()
    n = next(s for s in finished(writer, exporter) if s.name == "n")
    assert n.parent is None
    assert n.context is not None
    assert format(n.context.trace_id, "032x") == _TRACE


def test_ambient_parent_by_default(writer: OtelWriter, exporter: InMemorySpanExporter):
    with writer.start_span(name="outer", kind=SpanKind.CHAIN) as outer:
        writer.open_span(name="inner", kind=SpanKind.CHAIN).end()
    inner = next(s for s in finished(writer, exporter) if s.name == "inner")
    assert inner.parent is not None
    assert format(inner.parent.span_id, "016x") == outer.id


@pytest.mark.parametrize(
    "ctx",
    [
        TraceContext(trace_id="not-hex"),
        TraceContext(trace_id="0x" + "a" * 30),
        TraceContext(trace_id=_TRACE, parent_span_id="short"),
        TraceContext(trace_id="0" * 32),
    ],
)
def test_invalid_trace_context_records_nothing_and_counts(
    writer: OtelWriter, exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture, ctx: TraceContext
):
    with caplog.at_level(logging.ERROR):
        span = writer.open_span(name="n", kind=SpanKind.CHAIN, trace_context=ctx)
        span.end()
    assert span.id == ""
    assert finished(writer, exporter) == []
    assert writer.export_health().records_failed == 1
    assert "invalid trace context" in caplog.text


def test_record_span_exact_times(writer: OtelWriter, exporter: InMemorySpanExporter):
    start = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    end = start + timedelta(seconds=7)
    writer.record_span(
        name="closed",
        kind=SpanKind.TOOL,
        start=start,
        end=end,
        trace_context=TraceContext(trace_id=_TRACE, parent_span_id=_PARENT),
        input_={"a": 1},
        output={"b": 2},
        level=MonitoringLevel.WARNING,
        usage=TokenUsage(input_tokens=1),
        metadata={"k": "v"},
    )
    record = only(writer, exporter)
    assert record.start_time == int(start.timestamp() * 1e9)
    assert record.end_time == int(end.timestamp() * 1e9)
    assert decoded(record, attr.GEN_AI_TOOL_CALL_RESULT) == {"b": 2}
    assert attrs(record)[attr.LEVEL] == "WARNING"
    assert decoded(record, attr.METADATA) == {"k": "v"}


def test_record_span_requires_a_trace_id(writer: OtelWriter):
    with pytest.raises(ValueError, match=r"requires trace_context\.trace_id"):
        writer.record_span(
            name="x", kind=SpanKind.TOOL, start=datetime.now(UTC), end=datetime.now(UTC), trace_context=TraceContext()
        )


def test_create_event_returns_its_record_id(writer: OtelWriter, exporter: InMemorySpanExporter):
    rid = writer.create_event(
        name="ev", level=MonitoringLevel.WARNING, input_={"i": 1}, output={"o": 2}, metadata={"k": 1}
    )
    record = only(writer, exporter)
    assert record.context is not None
    assert rid == RecordId(
        trace_id=format(record.context.trace_id, "032x"), span_id=format(record.context.span_id, "016x")
    )
    assert record.start_time == record.end_time
    assert attrs(record)[attr.SPAN_KIND] == "EVENT"
    assert decoded(record, attr.OUTPUT_VALUE) == {"o": 2}
    assert attrs(record)[attr.LEVEL] == "WARNING"


# --- current span, disable ----------------------------------------------------------


def test_activated_span_is_current_until_end(writer: OtelWriter):
    assert writer.current_span_id() is None
    assert writer.current_trace_id() is None
    span = writer.open_span(name="n", kind=SpanKind.CHAIN, activate=True)
    assert writer.current_span_id() == span.id
    assert writer.current_trace_id() is not None
    span.end()
    assert writer.current_span_id() is None


def test_not_activated_span_is_not_current(writer: OtelWriter):
    span = writer.open_span(name="n", kind=SpanKind.CHAIN)
    assert writer.current_span_id() is None
    span.end()


def test_nested_activation_restores_the_previous_span(writer: OtelWriter):
    outer = writer.open_span(name="o", kind=SpanKind.CHAIN, activate=True)
    inner = writer.open_span(name="i", kind=SpanKind.CHAIN, activate=True)
    assert writer.current_span_id() == inner.id
    inner.end()
    assert writer.current_span_id() == outer.id
    outer.end()


def test_a_span_ended_in_another_context_is_never_current_again(
    writer: OtelWriter, exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
):
    outer = writer.open_span(name="outer", kind=SpanKind.CHAIN, activate=True)
    inner = writer.open_span(name="inner", kind=SpanKind.CHAIN, activate=True)
    with caplog.at_level(logging.ERROR):
        contextvars.copy_context().run(inner.end)
    assert caplog.text == ""
    assert writer.current_span_id() == outer.id
    writer.update_current_span(output="to the outer")
    writer.open_span(name="sibling", kind=SpanKind.CHAIN).end()
    outer.end()
    assert writer.current_span_id() is None
    assert writer.export_health().records_failed == 0
    by_name = {s.name: s for s in finished(writer, exporter)}
    assert by_name["sibling"].parent is not None
    assert format(by_name["sibling"].parent.span_id, "016x") == outer.id
    assert decoded(by_name["outer"], attr.OUTPUT_VALUE) == "to the outer"


def test_all_ended_activations_fall_back_to_what_was_current_before(writer: OtelWriter):
    only_span = writer.open_span(name="only", kind=SpanKind.CHAIN, activate=True)
    contextvars.copy_context().run(only_span.end)
    assert writer.current_span_id() is None
    assert writer.current_trace_id() is None


def test_update_current_span_sets_input_and_output(writer: OtelWriter, exporter: InMemorySpanExporter):
    with writer.start_span(name="n", kind=SpanKind.CHAIN):
        writer.update_current_span(input_={"in": 1}, output={"out": 2}, level=MonitoringLevel.ERROR, status_message="x")
    record = only(writer, exporter)
    assert decoded(record, attr.INPUT_VALUE) == {"in": 1}
    assert decoded(record, attr.OUTPUT_VALUE) == {"out": 2}
    assert record.status.status_code is StatusCode.ERROR


def test_update_current_span_outside_a_span_records_nothing(writer: OtelWriter, exporter: InMemorySpanExporter):
    writer.update_current_span(output={"out": 2})
    assert finished(writer, exporter) == []


def test_ending_twice_records_once(writer: OtelWriter, exporter: InMemorySpanExporter):
    span = writer.open_span(name="n", kind=SpanKind.CHAIN)
    span.end()
    span.end()
    assert len(finished(writer, exporter)) == 1


def test_disable_creates_nothing(writer: OtelWriter, exporter: InMemorySpanExporter):
    with writer.disable():
        with writer.start_span(name="s", kind=SpanKind.CHAIN) as span:
            assert span.id == ""
            span.update(output=1)
            span.set_trace_metadata(name="x")
            writer.update_current_span(output=2)
            assert writer.current_span_id() is None
        assert writer.create_event(name="e") is None
        writer.record_span(
            name="r",
            kind=SpanKind.TOOL,
            start=datetime.now(UTC),
            end=datetime.now(UTC),
            trace_context=TraceContext(trace_id=_TRACE),
        )
    assert finished(writer, exporter) == []


def test_is_recording(writer: OtelWriter):
    assert writer.is_recording() is True


def test_otel_global_provider_is_untouched(writer: OtelWriter):
    writer.open_span(name="n", kind=SpanKind.CHAIN).end()
    assert type(otel_trace.get_tracer_provider()).__name__ == "ProxyTracerProvider"


# --- boot refusals ---------------------------------------------------------------------


def test_refuses_without_processor_self_metrics(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED")
    with pytest.raises(RuntimeError) as info:
        OtelWriter(exporter=InMemorySpanExporter())
    assert str(info.value) == (
        "monitoring requires OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED=true: without it the span processor's "
        "queue-full drops are not counted"
    )


def test_refuses_without_an_endpoint():
    with pytest.raises(RuntimeError) as info:
        OtelWriter()
    assert str(info.value) == (
        "monitoring requires OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (or OTEL_EXPORTER_OTLP_ENDPOINT): the OTLP/HTTP "
        "endpoint of the collector"
    )


@pytest.mark.parametrize("name", ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"])
def test_builds_the_otlp_exporter_from_the_endpoint_env(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.setenv(name, "http://127.0.0.1:9/v1/traces")
    w = OtelWriter()
    try:
        processors = w._ensure_built().provider._active_span_processor._span_processors
        batch = next(p for p in processors if isinstance(p, BatchSpanProcessor))
        assert isinstance(batch.span_exporter._exporter, OTLPSpanExporter)  # pyright: ignore[reportAttributeAccessIssue]
    finally:
        w.shutdown()


# --- lifecycle and the health listener ------------------------------------------------


def test_shutdown_then_use_rebuilds_the_provider():
    exporter = ReusableExporter()
    w = OtelWriter(exporter=exporter)
    try:
        w.open_span(name="before", kind=SpanKind.CHAIN).end()
        first = w._pipeline
        w.shutdown()
        assert w._pipeline is None
        assert exporter.shutdowns == 1
        w.open_span(name="after", kind=SpanKind.CHAIN).end()
        assert w._pipeline is not None
        assert w._pipeline is not first
        w.flush()
        assert [s.name for s in exporter.get_finished_spans()] == ["before", "after"]
    finally:
        w.shutdown()


def test_flush_without_a_pipeline_still_calls_the_listener(writer: OtelWriter):
    seen: list[MonitoringExportHealth] = []
    writer.set_health_listener(seen.append)
    writer.flush()
    assert seen == [MonitoringExportHealth()]


def _queue_drop_writer(monkeypatch: pytest.MonkeyPatch) -> tuple[OtelWriter, BlockingExporter]:
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "2")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
    exporter = BlockingExporter()
    w = OtelWriter(exporter=exporter)
    w.open_span(name="first", kind=SpanKind.CHAIN).end()
    assert exporter.entered.wait(5)
    for i in range(10):
        w.open_span(name=f"s{i}", kind=SpanKind.CHAIN).end()
    return w, exporter


def test_flush_hands_the_listener_the_drops_collected_by_this_flush(monkeypatch: pytest.MonkeyPatch):
    w, exporter = _queue_drop_writer(monkeypatch)
    seen: list[MonitoringExportHealth] = []
    w.set_health_listener(seen.append)
    try:
        assert w.export_health().spans_dropped == 0
        exporter.release.set()
        w.flush()
        assert len(seen) == 1
        assert seen[0].spans_dropped == 8
    finally:
        w.set_health_listener(None)
        w.shutdown()


def test_shutdown_calls_the_listener(writer: OtelWriter):
    seen: list[MonitoringExportHealth] = []
    writer.set_health_listener(seen.append)
    writer.open_span(name="n", kind=SpanKind.CHAIN).end()
    writer.shutdown()
    assert seen == [MonitoringExportHealth()]


def test_removed_listener_is_not_called(writer: OtelWriter):
    seen: list[MonitoringExportHealth] = []
    writer.set_health_listener(seen.append)
    writer.set_health_listener(None)
    writer.flush()
    assert seen == []


def test_raising_listener_is_logged_and_the_flush_returns(writer: OtelWriter, caplog: pytest.LogCaptureFixture):
    def boom(_: MonitoringExportHealth) -> None:
        raise RuntimeError("listener broke")

    writer.set_health_listener(boom)
    with caplog.at_level(logging.ERROR):
        writer.flush()
    assert "monitoring health listener failed" in caplog.text


# --- fork --------------------------------------------------------------------------------


def _in_child(body: Any) -> dict[str, Any]:
    """Run ``body()`` in a forked child; return the JSON object it reports."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child, which exits through os._exit
        try:
            os.close(read_fd)
            os.write(write_fd, orjson.dumps(body()))
        finally:
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as pipe:
        data = pipe.read()
    os.waitpid(pid, 0)
    return orjson.loads(data)


@pytest.mark.filterwarnings(_FORK_WARNING)
def test_forked_child_counts_start_at_zero_and_never_see_the_parents_pending_drops(monkeypatch: pytest.MonkeyPatch):
    w, exporter = _queue_drop_writer(monkeypatch)
    w._health.add_record_failed("parent failure")
    child_seen: list[MonitoringExportHealth] = []

    def child() -> dict[str, Any]:
        exporter.release.set()
        before = w.export_health().model_dump()
        w.set_health_listener(child_seen.append)
        w.open_span(name="child", kind=SpanKind.CHAIN).end()
        w.flush()
        return {"before": before, "flushed": child_seen[-1].model_dump()}

    try:
        report = _in_child(child)
        assert report["before"] == MonitoringExportHealth().model_dump()
        assert report["flushed"]["spans_dropped"] == 0
        assert report["flushed"]["records_failed"] == 0
        exporter.release.set()
        w.flush()
        parent = w.export_health()
        assert parent.spans_dropped == 8
        assert parent.records_failed == 1
    finally:
        w.shutdown()


@pytest.mark.filterwarnings(_FORK_WARNING)
@pytest.mark.parametrize("hook_first", [True, False])
def test_an_other_after_fork_shutdown_hook_hands_the_listener_zeros(exporter: InMemorySpanExporter, hook_first: bool):
    holder: list[weakref.ref[OtelWriter]] = []
    seen: list[MonitoringExportHealth] = []

    def shutdown_hook() -> None:
        target = holder[0]() if holder else None
        if target is not None:
            target.shutdown()

    if hook_first:
        os.register_at_fork(after_in_child=shutdown_hook)
    w = OtelWriter(exporter=exporter)
    holder.append(weakref.ref(w))
    if not hook_first:
        os.register_at_fork(after_in_child=shutdown_hook)
    w.open_span(name="n", kind=SpanKind.CHAIN).end()
    w._health.add_record_failed("parent failure")
    w.set_health_listener(seen.append)
    try:
        report = _in_child(lambda: [h.model_dump() for h in seen])
        assert report == [MonitoringExportHealth().model_dump()]
        assert w.export_health().records_failed == 1
    finally:
        holder.clear()
        w.set_health_listener(None)
        w.shutdown()
