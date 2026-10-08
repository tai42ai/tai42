"""No-op monitoring backend: writes do nothing, reads return empty results.

Used in two places: as a named test double, and as the registered backend when
monitoring is intentionally disabled (e.g. a process with no monitoring
backend configured). It is an explicit, named no-op — not a real backend
silently degrading.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from tai42_contract.monitoring import (
    DEFAULT_LEVEL,
    Dimension,
    ListCapability,
    Measure,
    MetricsCapability,
    MetricsQuery,
    MetricsResult,
    MonitoringExportHealth,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringObservation,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
    RecordId,
    Span,
    SpanKind,
    SpanWindowItem,
    TokenUsage,
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
        usage: TokenUsage | None = None,
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

    def end(self, *, end_time: datetime | None = None) -> None:
        """Record nothing for the span's end."""


class NoOpWriter:
    """A writer that emits nothing; it declares itself not recording so producers skip all recording work."""

    def __init__(self) -> None:
        """Hold no health listener."""
        self._listener: Callable[[MonitoringExportHealth], None] | None = None

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
        start_time: datetime | None = None,
        activate: bool = False,
    ) -> Span:
        """Return a no-op span; records nothing."""
        return NoOpSpan()

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
        usage: TokenUsage | None = None,
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
    ) -> RecordId | None:
        """Discard an event; no record exists, so no id."""
        return None

    def update_current_span(
        self,
        *,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        input_: Any = None,
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

    def current_span_id(self) -> str | None:
        """The current span id; always ``None`` for a no-op writer."""
        return None

    def is_recording(self) -> bool:
        """A no-op writer records nothing."""
        return False

    def export_health(self) -> MonitoringExportHealth:
        """Nothing is delivered, so nothing can fail."""
        return MonitoringExportHealth()

    def set_health_listener(self, listener: Callable[[MonitoringExportHealth], None] | None) -> None:
        """Store the listener; a writer that records nothing never calls it."""
        self._listener = listener

    @contextmanager
    def disable(self) -> Iterator[None]:
        """Enter a no-op disable scope."""
        yield

    def flush(self) -> None:
        """Flush nothing."""

    def shutdown(self) -> None:
        """Shut down nothing."""


# The contract's documented ``list_traces`` sort fields; the no-op reader serves every one.
_TRACE_SORT_FIELDS = frozenset({"timestamp", "total_cost", "name", "id", "latency", "total_tokens"})
# The declared page ceiling; the no-op reader serves an empty page of any size up to it.
_MAX_PAGE_SIZE = 1000


class NoOpReader:
    """A reader that returns empty results."""

    def list_capability(self) -> ListCapability:
        """Declare every sort with no incompatible filter; the disabled backend serves each as an empty page."""
        return ListCapability(sort_fields=_TRACE_SORT_FIELDS)

    def max_page_size(self) -> int:
        """The declared page ceiling."""
        return _MAX_PAGE_SIZE

    def metrics_capability(self) -> MetricsCapability:
        """Declare the full neutral vocabulary; the disabled backend serves it as empty data, not an error."""
        return MetricsCapability(measures=frozenset(Measure), dimensions=frozenset(Dimension))

    async def query_metrics(self, query: MetricsQuery) -> MetricsResult:
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

    async def get_observation(self, trace_id: str, observation_id: str) -> MonitoringObservation:
        """Raise :class:`TraceNotFoundError`; a no-op reader holds no traces."""
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
        """Return no traces; a ``limit`` above :meth:`max_page_size` raises ``ValueError``."""
        if limit is not None and limit > _MAX_PAGE_SIZE:
            raise ValueError(f"limit {limit} exceeds the reader's maximum page size {_MAX_PAGE_SIZE}")
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
