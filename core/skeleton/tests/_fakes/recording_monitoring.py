"""A recording monitoring backend test double for the send-outcome monitoring layer.

Subclasses the shipped ``NoOp*`` backend so it conforms to the writer protocol, and
records every opened span (name / kind / trace context / input / metadata, plus each
``update`` on its handle) and every ``create_event`` for assertions. ``active_trace_id``
seeds ``current_trace_id`` so a seam's ambient-gate can be driven both ways; the span a
``start_span`` or ``use_span`` block holds is the current span.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

from tai42_contract.app import tai42_app
from tai42_contract.monitoring import (
    DEFAULT_LEVEL,
    MonitoringLevel,
    Span,
    SpanKind,
    TokenUsage,
    TraceContext,
)

from tai42_skeleton.app.facets import MonitoringFacet
from tai42_skeleton.monitoring import NoOpMonitoring, NoOpReader, NoOpSpan, NoOpWriter


class RecordingSpan(NoOpSpan):
    """Span handle recording its id, its trace, every ``update`` call and its ``end``."""

    def __init__(self, span_id: str, trace_id: str) -> None:
        self._id = span_id
        self.trace_id = trace_id
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    @property
    def id(self) -> str:
        return self._id

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
        self.updates.append(
            {
                "output": output,
                "metadata": metadata,
                "level": level,
                "status_message": status_message,
            }
        )

    def end(self, *, end_time: Any = None) -> None:
        self.ended = True


class RecordingWriter(NoOpWriter):
    """Writer recording opened spans and created events. ``active_trace_id`` seeds
    ``current_trace_id``; ``next_span_id`` is handed to each opened span."""

    def __init__(self) -> None:
        self.active_trace_id: str | None = None
        self.next_span_id = "span-1"
        # The trace id a ROOT span establishes: opening a span when no trace is active
        # makes that span a new trace root, so ``current_trace_id`` reports this id for
        # the span's duration (mirroring a real backend). When a trace is already active
        # the open nests and the active id is unchanged.
        self.root_trace_id = "trace-root"
        self.spans: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._current: list[RecordingSpan] = []

    def current_trace_id(self) -> str | None:
        return self.active_trace_id

    def current_span_id(self) -> str | None:
        return self._current[-1].id if self._current else None

    def is_recording(self) -> bool:
        return True

    def _record(
        self, name: str, kind: SpanKind, trace_context: TraceContext | None, input_: Any, metadata: Any
    ) -> RecordingSpan:
        explicit = trace_context.trace_id if trace_context is not None else None
        span = RecordingSpan(self.next_span_id, explicit or self.active_trace_id or self.root_trace_id)
        self.spans.append(
            {
                "name": name,
                "kind": kind,
                "trace_context": trace_context,
                "input": input_,
                "metadata": metadata,
                "span": span,
            }
        )
        return span

    @contextmanager
    def _held(self, span: RecordingSpan) -> Iterator[None]:
        previous = self.active_trace_id
        self.active_trace_id = span.trace_id
        self._current.append(span)
        try:
            yield
        finally:
            self._current.pop()
            self.active_trace_id = previous

    def open_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        trace_context: TraceContext | None = None,
        input_: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        start_time: Any = None,
        activate: bool = False,
    ) -> RecordingSpan:
        return self._record(name, kind, trace_context, input_, metadata)

    @contextmanager
    def use_span(self, span: Span) -> Iterator[None]:
        assert isinstance(span, RecordingSpan), span
        with self._held(span):
            yield

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
    ) -> Iterator[RecordingSpan]:
        span = self._record(name, kind, trace_context, input_, metadata)
        with self._held(span):
            yield span

    def create_event(
        self,
        *,
        name: str,
        level: MonitoringLevel = DEFAULT_LEVEL,
        trace_context: TraceContext | None = None,
        input_: Any = None,
        output: Any = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "name": name,
                "level": level,
                "trace_context": trace_context,
                "input": input_,
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


@contextmanager
def monitoring_app() -> Iterator[None]:
    """Bind an app whose ``monitoring`` facet serves the registered backend, as ``tai42_app.monitoring`` reads it."""
    with tai42_app.bound(SimpleNamespace(monitoring=MonitoringFacet(cast(Any, None)))):
        yield
