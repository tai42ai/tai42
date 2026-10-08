"""``CountingSpanExporter``: every failed delivery and every evicted attribute is counted."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import pytest
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from tai42_kit.monitoring.otel.exporter import CountingSpanExporter
from tai42_kit.monitoring.otel.health import ExportHealthCounters


class _Result(SpanExporter):
    def __init__(self, result: SpanExportResult) -> None:
        self.result = result
        self.calls: list[int] = []
        self.flushed = False
        self.stopped = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.calls.append(len(spans))
        return self.result

    def shutdown(self) -> None:
        self.stopped = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flushed = True
        return True


class _Raising(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise ConnectionError("collector down")


def _spans(n: int, dropped: int = 0) -> list[ReadableSpan]:
    attributes = BoundedAttributes(maxlen=1, attributes={f"a{i}": i for i in range(dropped + 1)})
    return [ReadableSpan(name="s", attributes=attributes)] * n


def test_failure_result_is_counted(caplog: pytest.LogCaptureFixture):
    health = ExportHealthCounters()
    wrapped = _Result(SpanExportResult.FAILURE)
    with caplog.at_level(logging.ERROR):
        assert CountingSpanExporter(wrapped, health).export(_spans(3)) is SpanExportResult.FAILURE
    snap = health.snapshot()
    assert (snap.spans_failed, snap.export_failures) == (3, 1)
    assert snap.last_error == "export of 3 spans returned FAILURE"
    assert "monitoring export failed for 3 spans" in caplog.text


def test_success_counts_nothing():
    health = ExportHealthCounters()
    assert CountingSpanExporter(_Result(SpanExportResult.SUCCESS), health).export(_spans(2)) is SpanExportResult.SUCCESS
    assert health.snapshot().export_failures == 0


def test_raise_is_counted_and_reraised():
    health = ExportHealthCounters()
    with pytest.raises(ConnectionError, match="collector down"):
        CountingSpanExporter(_Raising(), health).export(_spans(2))
    snap = health.snapshot()
    assert (snap.spans_failed, snap.export_failures) == (2, 1)
    assert snap.last_error == "ConnectionError('collector down')"


def test_dropped_attributes_are_counted(caplog: pytest.LogCaptureFixture):
    health = ExportHealthCounters()
    with caplog.at_level(logging.ERROR):
        CountingSpanExporter(_Result(SpanExportResult.SUCCESS), health).export(_spans(1, dropped=2))
    assert health.snapshot().attributes_dropped == 2
    assert "dropped 2 span attributes" in caplog.text


def test_shutdown_and_flush_delegate():
    wrapped = _Result(SpanExportResult.SUCCESS)
    exporter = CountingSpanExporter(wrapped, ExportHealthCounters())
    assert exporter.force_flush() is True
    exporter.shutdown()
    assert wrapped.flushed
    assert wrapped.stopped
