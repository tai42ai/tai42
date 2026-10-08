"""The processor's queue-full evictions reach ``export_health().spans_dropped`` through its own self-metric."""

from __future__ import annotations

import pytest
from tai42_contract.monitoring import SpanKind

from tai42_kit.monitoring.otel import OtelWriter
from tai42_kit.monitoring.otel.exporter import QueueDropMetricExporter
from tai42_kit.monitoring.otel.health import ExportHealthCounters

from .conftest import BlockingExporter


def test_full_queue_drops_are_counted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "2")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
    exporter = BlockingExporter()
    writer = OtelWriter(exporter=exporter)
    try:
        writer.open_span(name="first", kind=SpanKind.CHAIN).end()
        assert exporter.entered.wait(5)
        for i in range(10):
            writer.open_span(name=f"s{i}", kind=SpanKind.CHAIN).end()
        pipeline = writer._pipeline
        assert pipeline is not None
        pipeline.meter_provider.force_flush()
        assert writer.export_health().spans_dropped == 8
        pipeline.meter_provider.force_flush()
        assert writer.export_health().spans_dropped == 8
    finally:
        exporter.release.set()
        writer.shutdown()


def test_metric_exporter_lifecycle_is_inert():
    exporter = QueueDropMetricExporter(ExportHealthCounters())
    assert exporter.force_flush() is True
    exporter.shutdown()
