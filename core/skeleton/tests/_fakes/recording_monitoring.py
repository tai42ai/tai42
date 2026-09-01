"""A recording monitoring backend test double for the send-outcome monitoring layer.

Subclasses the shipped ``NoOp*`` backend so it conforms to the writer protocol, and
records every opened span (name / kind / input / metadata, plus each ``update`` on its
handle) and every ``create_event`` for assertions. ``active_trace_id`` seeds
``current_trace_id`` so a seam's ambient-gate can be driven both ways.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tai42_contract.monitoring import (
    DEFAULT_LEVEL,
    MonitoringLevel,
    SpanKind,
    TraceContext,
)

from tai42_skeleton.monitoring import NoOpMonitoring, NoOpReader, NoOpSpan, NoOpWriter


class RecordingSpan(NoOpSpan):
    """Span handle recording its id and every ``update`` call."""

    def __init__(self, span_id: str) -> None:
        self._id = span_id
        self.updates: list[dict[str, Any]] = []

    @property
    def id(self) -> str:
        return self._id

    def update(
        self,
        *,
        output: Any = None,
        model: str | None = None,
        usage_details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
    ) -> None:
        self.updates.append(
            {
                "output": output,
                "metadata": metadata,
                "level": level,
                "status_message": status_message,
            }
        )


class RecordingWriter(NoOpWriter):
    """Writer recording opened spans and created events. ``active_trace_id`` seeds
    ``current_trace_id``; ``next_span_id`` is handed to each opened span."""

    def __init__(self) -> None:
        self.active_trace_id: str | None = None
        self.next_span_id = "span-1"
        self.spans: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def current_trace_id(self) -> str | None:
        return self.active_trace_id

    @contextmanager
    def start_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        trace_context: TraceContext | None = None,
        input: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[RecordingSpan]:
        span = RecordingSpan(self.next_span_id)
        self.spans.append({"name": name, "kind": kind, "input": input, "metadata": metadata, "span": span})
        yield span

    def create_event(
        self,
        *,
        name: str,
        level: MonitoringLevel = DEFAULT_LEVEL,
        trace_context: TraceContext | None = None,
        input: Any = None,
        output: Any = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "name": name,
                "level": level,
                "trace_context": trace_context,
                "input": input,
            }
        )


class RecordingMonitoring(NoOpMonitoring):
    """Backend whose writer records; reader stays the no-op."""

    def __init__(self) -> None:
        super().__init__()
        self._recording_writer = RecordingWriter()

    @property
    def writer(self) -> RecordingWriter:
        return self._recording_writer

    @property
    def reader(self) -> NoOpReader:
        return self._reader
