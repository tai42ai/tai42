"""The two exporters that turn the SDK's delivery outcomes into the writer's counts.

The OTLP exporter returns ``FAILURE`` without raising and the batch processor ignores the
result, so the delegating span exporter is the only place a failed delivery is seen. A full
export queue evicts the oldest span and is visible only through the processor's own
``otel.sdk.processor.span.processed`` counter (``error.type="queue_full"``), which the
metric exporter reads.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.metrics import Counter
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
    MetricsData,
    NumberDataPoint,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from tai42_kit.monitoring.otel.health import ExportHealthCounters

logger = logging.getLogger(__name__)

PROCESSED_SPANS_METRIC = "otel.sdk.processor.span.processed"
_QUEUE_FULL = "queue_full"


class CountingSpanExporter(SpanExporter):
    """Delegates to ``exporter`` and counts every failed delivery and every evicted attribute."""

    def __init__(self, exporter: SpanExporter, health: ExportHealthCounters) -> None:
        """Wrap ``exporter``; counts go to ``health``."""
        self._exporter = exporter
        self._health = health

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Export ``spans`` through the wrapped exporter, counting a FAILURE result or a raise."""
        dropped = sum(span.dropped_attributes for span in spans)
        if dropped:
            self._health.add_attributes_dropped(dropped)
            logger.error("monitoring dropped %d span attributes over the span limit", dropped)
        try:
            result = self._exporter.export(spans)
        except Exception as exc:
            self._health.add_export_failure(len(spans), repr(exc))
            logger.error("monitoring export failed for %d spans", len(spans))  # noqa: TRY400 - the processor logs the traceback
            raise
        if result is SpanExportResult.FAILURE:
            self._health.add_export_failure(len(spans), f"export of {len(spans)} spans returned FAILURE")
            logger.error("monitoring export failed for %d spans", len(spans))
        return result

    def shutdown(self) -> None:
        """Shut the wrapped exporter down."""
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Flush the wrapped exporter."""
        return self._exporter.force_flush(timeout_millis)


class QueueDropMetricExporter(MetricExporter):
    """Counts the processor's queue-full drops (delta temporality) into the writer's health."""

    def __init__(self, health: ExportHealthCounters) -> None:
        """Counts go to ``health``."""
        super().__init__(preferred_temporality={Counter: AggregationTemporality.DELTA})
        self._health = health

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any) -> MetricExportResult:
        """Add every queue-full data point of the processed-spans counter to ``spans_dropped``."""
        dropped = 0
        for resource_metrics in metrics_data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    if metric.name != PROCESSED_SPANS_METRIC:
                        continue
                    for point in metric.data.data_points:
                        point_attributes = point.attributes or {}
                        if isinstance(point, NumberDataPoint) and point_attributes.get("error.type") == _QUEUE_FULL:
                            dropped += int(point.value)
        if dropped > 0:
            self._health.add_spans_dropped(dropped)
            logger.error("monitoring dropped %d spans: the export queue was full", dropped)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        """Nothing is buffered here."""
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        """Nothing to release."""
