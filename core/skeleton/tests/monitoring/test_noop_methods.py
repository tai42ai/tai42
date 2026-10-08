"""Every NoOp* method is exercised: writes accept their args and do nothing,
context managers yield, the reader returns empty results, and the span handle
records nothing. This is the explicit, named no-op backend used when monitoring
is disabled — not a real backend silently degrading."""

from __future__ import annotations

from datetime import datetime

import pytest
from tai42_contract.monitoring import (
    Dimension,
    Measure,
    MetricsCapability,
    MetricsQuery,
    MonitoringExportHealth,
    MonitoringWriter,
    SpanKind,
    TokenUsage,
    TraceContext,
    TraceNotFoundError,
)

from tai42_skeleton.monitoring import NoOpReader, NoOpSpan, NoOpWriter


def _ctx() -> TraceContext:
    return TraceContext(trace_id="t-1")


# --- span -------------------------------------------------------------------


def test_noop_span_id_and_updates_are_inert() -> None:
    span = NoOpSpan()
    assert span.id == ""
    assert span.update(output="x", model="m", usage=TokenUsage(), metadata={}, status_message="s") is None
    assert span.set_trace_metadata(name="n", tags=["a"]) is None
    assert span.end(end_time=datetime.now()) is None


# --- writer -----------------------------------------------------------------


def test_noop_writer_start_span_yields_a_span() -> None:
    writer = NoOpWriter()
    with writer.start_span(name="s", kind=SpanKind.TOOL, trace_context=_ctx(), input_={"a": 1}) as span:
        assert isinstance(span, NoOpSpan)


def test_noop_writer_open_span_returns_an_inert_span() -> None:
    span = NoOpWriter().open_span(name="s", kind=SpanKind.CHAIN, input_={"a": 1}, activate=True)
    assert isinstance(span, NoOpSpan)
    assert span.id == ""


def test_noop_writer_record_and_event_are_inert() -> None:
    writer = NoOpWriter()
    now = datetime.now()
    assert writer.record_span(name="s", kind=SpanKind.LLM, start=now, end=now, trace_context=_ctx()) is None
    assert writer.create_event(name="e", trace_context=_ctx(), input_="i", output="o") is None
    assert writer.update_current_span(status_message="m", metadata={}, input_="i", output="o") is None


def test_noop_writer_context_managers_yield() -> None:
    writer = NoOpWriter()
    with writer.trace_attributes(name="n", tags=["t"], metadata={}):
        pass
    with writer.disable():
        pass


def test_noop_writer_query_helpers_return_neutral_values() -> None:
    writer = NoOpWriter()
    assert writer.current_trace_id() is None
    assert writer.current_span_id() is None
    assert writer.is_recording() is False
    assert writer.export_health() == MonitoringExportHealth()


def test_noop_writer_stores_the_health_listener_and_never_calls_it() -> None:
    writer = NoOpWriter()
    seen: list[MonitoringExportHealth] = []
    writer.set_health_listener(seen.append)
    writer.flush()
    writer.shutdown()
    assert seen == []


def test_noop_writer_implements_the_whole_writer_protocol() -> None:
    writer = NoOpWriter()
    import typing

    assert all(hasattr(writer, m) for m in typing.get_protocol_members(MonitoringWriter))


def test_noop_writer_lifecycle_calls_are_inert() -> None:
    writer = NoOpWriter()
    assert writer.flush() is None
    assert writer.shutdown() is None


# --- reader -----------------------------------------------------------------


async def test_noop_reader_returns_empty_results() -> None:
    reader = NoOpReader()
    now = datetime.now()
    from tai42_contract.monitoring import MetricsResult

    result = await reader.query_metrics(MetricsQuery(measures=[Measure.COUNT], from_timestamp=now, to_timestamp=now))
    assert isinstance(result, MetricsResult)
    assert result.rows == []
    assert await reader.list_spans_in_window(now, now) == []
    assert await reader.list_traces() == []


async def test_noop_reader_holds_no_observation() -> None:
    with pytest.raises(TraceNotFoundError):
        await NoOpReader().get_observation("t", "o")


def test_noop_reader_declares_the_full_capability() -> None:
    # The disabled backend declares the whole neutral vocabulary and serves it as
    # empty data, so a dashboard renders zeros rather than a read-not-supported error.
    assert NoOpReader().metrics_capability() == MetricsCapability(
        measures=frozenset(Measure), dimensions=frozenset(Dimension)
    )


def test_noop_reader_declares_every_sort_and_a_page_ceiling() -> None:
    reader = NoOpReader()
    capability = reader.list_capability()
    assert capability.sort_fields == {"timestamp", "total_cost", "name", "id", "latency", "total_tokens"}
    assert capability.incompatible_filters == {}
    assert reader.max_page_size() == 1000


async def test_noop_reader_refuses_a_page_above_its_ceiling() -> None:
    reader = NoOpReader()
    assert await reader.list_traces(limit=1000) == []
    with pytest.raises(ValueError, match="limit 1001 exceeds the reader's maximum page size 1000"):
        await reader.list_traces(limit=1001)
