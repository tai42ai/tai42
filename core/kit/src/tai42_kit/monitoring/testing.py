"""``InMemoryOtelReader``: a test helper that reads ``OtelWriter`` records back without a backend.

It maps each finished span of an ``InMemorySpanExporter`` back to a ``MonitoringObservation``
through the writer's attribute schema (the reader is the schema's inverse), so a consumer's
proof test walks and resolves a recording with no running backend. Flush the writer before
reading. Every list and aggregate read raises ``MonitoringReadNotSupportedError``; the declarations
serve no sort and no measure.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import orjson
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from tai42_contract.monitoring import (
    PROMOTED_METADATA_KEYS,
    ListCapability,
    MetricsCapability,
    MetricsQuery,
    MetricsResult,
    MonitoringFilter,
    MonitoringObservation,
    MonitoringReadNotSupportedError,
    MonitoringTrace,
    MonitoringTraceSummary,
    ObservationNotFoundError,
    OrderBy,
    SpanKind,
    SpanWindowItem,
    TraceNotFoundError,
)

from tai42_kit.monitoring.otel import attributes as attr

__all__ = ["InMemoryOtelReader", "span_to_observation"]

_NOT_SUPPORTED = "in-memory test reader"
_MAX_PAGE_SIZE = 100


def _decode(value: Any) -> Any:
    return orjson.loads(value) if isinstance(value, str) else value


def _ts(ns: int | None) -> datetime | None:
    return None if ns is None else datetime.fromtimestamp(ns / 1e9, tz=UTC)


def _metadata(attributes: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata: dict[str, Any] = {}
    if attr.METADATA in attributes:
        metadata.update(_decode(attributes[attr.METADATA]))
    for key in PROMOTED_METADATA_KEYS:
        if key in attributes:
            metadata[key] = attributes[key]
    return metadata or None


def _int_attribute(attributes: Mapping[str, Any], key: str) -> int | None:
    value = attributes.get(key)
    return None if value is None else int(value)


def span_to_observation(span: ReadableSpan) -> MonitoringObservation:
    """One exported span as the observation the writer recorded."""
    attributes: Mapping[str, Any] = span.attributes or {}
    kind = SpanKind(attributes[attr.SPAN_KIND])
    context = span.get_span_context()
    status_message = attributes.get(attr.STATUS_MESSAGE)
    if status_message is None and span.status.status_code is StatusCode.ERROR:
        status_message = span.status.description
    input_key, output_key = attr.input_attribute(kind), attr.output_attribute(kind)
    return MonitoringObservation(
        id=format(context.span_id, "016x") if context else "",
        trace_id=format(context.trace_id, "032x") if context else None,
        parent_id=format(span.parent.span_id, "016x") if span.parent is not None else None,
        kind=kind,
        name=span.name,
        level=attributes.get(attr.LEVEL),
        status_message=status_message,
        input=_decode(attributes[input_key]) if input_key in attributes else None,
        output=_decode(attributes[output_key]) if output_key in attributes else None,
        metadata=_metadata(attributes),
        input_tokens=_int_attribute(attributes, attr.GEN_AI_USAGE_INPUT_TOKENS),
        output_tokens=_int_attribute(attributes, attr.GEN_AI_USAGE_OUTPUT_TOKENS),
        total_tokens=_int_attribute(attributes, attr.USAGE_TOTAL_TOKENS),
        model=attributes.get(attr.GEN_AI_RESPONSE_MODEL, attributes.get(attr.GEN_AI_REQUEST_MODEL)),
        start=_ts(span.start_time),
        end=_ts(span.end_time),
    )


class InMemoryOtelReader:
    """A ``MonitoringReader`` over an ``InMemorySpanExporter``'s finished spans."""

    def __init__(self, exporter: InMemorySpanExporter) -> None:
        """Read the spans ``exporter`` holds."""
        self._exporter = exporter

    def _trace_spans(self, trace_id: str) -> list[ReadableSpan]:
        spans = [
            s
            for s in self._exporter.get_finished_spans()
            if s.context is not None and format(s.context.trace_id, "032x") == trace_id
        ]
        if not spans:
            raise TraceNotFoundError(f"trace {trace_id} not found")
        return sorted(spans, key=lambda s: s.start_time or 0)

    async def get_trace(self, trace_id: str) -> MonitoringTrace:
        """Every observation of ``trace_id``, in start order; ``TraceNotFoundError`` when absent."""
        observations = [span_to_observation(s) for s in self._trace_spans(trace_id)]
        root = next((o for o in observations if o.parent_id is None), observations[0])
        return MonitoringTrace(
            id=trace_id,
            timestamp=root.start,
            input=root.input,
            output=root.output,
            metadata=root.metadata,
            observations=observations,
        )

    async def get_observation(self, trace_id: str, observation_id: str) -> MonitoringObservation:
        """One observation; ``TraceNotFoundError`` / ``ObservationNotFoundError`` when absent."""
        for span in self._trace_spans(trace_id):
            if span.context is not None and format(span.context.span_id, "016x") == observation_id:
                return span_to_observation(span)
        raise ObservationNotFoundError(f"observation {observation_id} not found in trace {trace_id}")

    def metrics_capability(self) -> MetricsCapability:
        """No measure and no dimension is served."""
        return MetricsCapability(measures=frozenset(), dimensions=frozenset())

    def list_capability(self) -> ListCapability:
        """No sort is served: ``list_traces`` is not supported."""
        return ListCapability(sort_fields=frozenset())

    def max_page_size(self) -> int:
        """The declared page ceiling; ``list_traces`` is not supported at any size."""
        return _MAX_PAGE_SIZE

    async def query_metrics(self, query: MetricsQuery) -> MetricsResult:
        """Not supported."""
        raise MonitoringReadNotSupportedError(_NOT_SUPPORTED)

    async def list_spans_in_window(
        self,
        t0: datetime,
        t1: datetime,
        *,
        run: str | None = None,
        kind: SpanKind | None = None,
        filter_: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[SpanWindowItem]:
        """Not supported."""
        raise MonitoringReadNotSupportedError(_NOT_SUPPORTED)

    async def list_traces(
        self,
        *,
        from_timestamp: datetime | None = None,
        to_timestamp: datetime | None = None,
        limit: int | None = None,
        page: int | None = None,
        filter_: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[MonitoringTraceSummary]:
        """Not supported."""
        raise MonitoringReadNotSupportedError(_NOT_SUPPORTED)
