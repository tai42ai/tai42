"""No-op monitoring backend: writes do nothing, reads return empty results.

Used in two places: as a named test double, and as the registered backend when
monitoring is intentionally disabled (e.g. a process with no monitoring
backend configured). It is an explicit, named no-op — not a real backend
silently degrading.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from tai42_contract.monitoring import (
    DEFAULT_LEVEL,
    MetricsFilter,
    MetricsResult,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
    ProjectConfig,
    Span,
    SpanKind,
    SpanWindowItem,
    TraceContext,
    TraceNotFoundError,
)


class NoOpSpan:
    """A span handle that records nothing."""

    @property
    def id(self) -> str:
        """The span id; always empty for a no-op span."""
        return ""

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
        """Record nothing for a span update."""

    def set_trace_metadata(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Record nothing for trace metadata."""


class NoOpWriter:
    """A writer that emits nothing."""

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
    ) -> Iterator[Span]:
        """Yield a no-op span; records nothing."""
        yield NoOpSpan()

    def record_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        start: datetime,
        end: datetime,
        trace_context: TraceContext,
        input_: Any = None,
        output: Any = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        model: str | None = None,
        usage_details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Discard a completed span."""

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
        """Discard an event."""

    def update_current_span(
        self,
        *,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        output: Any = None,
    ) -> None:
        """Record nothing for the current span."""

    @contextmanager
    def trace_attributes(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[None]:
        """Enter a no-op trace-attributes scope."""
        yield

    def current_trace_id(self) -> str | None:
        """The current trace id; always ``None`` for a no-op writer."""
        return None

    def inject_context(self, ctx: TraceContext) -> dict:
        """The trace-context carrier headers; always empty for a no-op writer."""
        return {}

    def get_monitoring_callbacks(self, ctx: TraceContext) -> list:
        """The monitoring callbacks for ``ctx``; always empty for a no-op writer."""
        return []

    @contextmanager
    def scope(self, public_key: str) -> Iterator[None]:
        """Enter a no-op project scope."""
        yield

    @contextmanager
    def disable(self) -> Iterator[None]:
        """Enter a no-op disable scope."""
        yield

    def flush(self) -> None:
        """Flush nothing."""

    def shutdown(self) -> None:
        """Shut down nothing."""


class NoOpReader:
    """A reader that returns empty results."""

    async def query_metrics(self, filter_: MetricsFilter) -> MetricsResult:
        """Return an empty metrics result."""
        return MetricsResult()

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
        """Return no spans."""
        return []

    async def get_trace(self, trace_id: str) -> MonitoringTrace:
        """Raise :class:`TraceNotFoundError`; a no-op reader holds no traces."""
        # No data in the double, so every trace is absent — raise rather than
        # return None (the contract's ``get_trace`` is non-optional).
        raise TraceNotFoundError(f"trace {trace_id!r} not found (no-op reader)")

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
        """Return no traces."""
        return []


class NoOpMonitoring:
    """A backend whose writer and reader both do nothing."""

    def __init__(self) -> None:
        """Build the no-op writer and reader."""
        self._writer = NoOpWriter()
        self._reader = NoOpReader()

    @property
    def writer(self) -> NoOpWriter:
        """The no-op writer."""
        return self._writer

    @property
    def reader(self) -> NoOpReader:
        """The no-op reader."""
        return self._reader

    def add_project(self, project: ProjectConfig) -> None:
        """Register nothing for a project."""
