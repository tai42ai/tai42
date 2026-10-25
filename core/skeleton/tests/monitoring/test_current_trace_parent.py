"""``current_trace_parent`` reads the live span as one parent context, or ``None`` when no span is live."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_contract.monitoring import SpanKind, TraceContext
from tai42_kit.monitoring.otel import OtelWriter

from tai42_skeleton.monitoring import (
    NoOpMonitoring,
    NoOpReader,
    current_trace_parent,
    init_monitoring,
    reset_monitoring,
)


class _OtelMonitoring(NoOpMonitoring):
    def __init__(self, writer: OtelWriter) -> None:
        super().__init__()
        self._otel_writer = writer

    @property
    def writer(self) -> OtelWriter:
        return self._otel_writer

    @property
    def reader(self) -> NoOpReader:
        return self._reader


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_monitoring()
    yield
    reset_monitoring()


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> Iterator[OtelWriter]:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    otel = OtelWriter(exporter=InMemorySpanExporter())
    init_monitoring(_OtelMonitoring(otel))
    yield otel
    otel.shutdown()


def test_under_a_live_span_it_is_that_spans_trace_and_id(writer: OtelWriter) -> None:
    with writer.start_span(name="caller", kind=SpanKind.CHAIN) as span:
        parent = current_trace_parent()
        trace_id = writer.current_trace_id()

    assert trace_id
    assert parent == TraceContext(trace_id=trace_id, parent_span_id=span.id)


def test_with_no_live_span_it_is_none(writer: OtelWriter) -> None:
    assert current_trace_parent() is None


def test_under_the_no_op_writer_it_is_none() -> None:
    assert current_trace_parent() is None
