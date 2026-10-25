"""The Langfuse read implementation of ``MonitoringReader``.

``LangfuseReader`` composes one query object per read surface and delegates each contract method to it:
``query_metrics`` reads Metrics v2, every other read the observations list. The Langfuse client is synchronous,
so every call runs off the event loop. Every read but ``get_trace`` and ``get_observation`` is scoped to the
active project's ``source`` (the Langfuse ``environment``); a trace id is globally unique.
"""

from __future__ import annotations

from datetime import datetime

from tai42_contract.monitoring import (
    ListCapability,
    MetricsCapability,
    MetricsQuery,
    MetricsResult,
    MonitoringFilter,
    MonitoringObservation,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
    SpanKind,
    SpanWindowItem,
)

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager
from tai42_monitoring_langfuse.metrics_query import MetricsQueryRunner
from tai42_monitoring_langfuse.query_base import PAGE_SIZE
from tai42_monitoring_langfuse.sorting import TRACE_SORT_FIELDS
from tai42_monitoring_langfuse.span_window import SpanWindowQuery
from tai42_monitoring_langfuse.trace_query import TraceQuery


class LangfuseReader:
    """Serves the contract read surface from the Langfuse query APIs."""

    def __init__(self, manager: LangfuseClientManager) -> None:
        """Wire the reader's metrics, span-window and trace query helpers over ``manager``."""
        self._metrics = MetricsQueryRunner(manager)
        self._spans = SpanWindowQuery(manager)
        self._traces = TraceQuery(manager)

    def metrics_capability(self) -> MetricsCapability:
        """Declare the neutral measures and dimensions this backend's metrics query serves."""
        return self._metrics.metrics_capability()

    def list_capability(self) -> ListCapability:
        """Declare the trace sorts; every sort combines with every filter."""
        return ListCapability(sort_fields=TRACE_SORT_FIELDS)

    def max_page_size(self) -> int:
        """The largest ``limit`` one ``list_traces`` call accepts."""
        return PAGE_SIZE

    async def query_metrics(self, query: MetricsQuery) -> MetricsResult:
        """Return aggregated metrics matching ``query``."""
        return await self._metrics.query_metrics(query)

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
        """List spans in the ``[t0, t1)`` window, optionally filtered and ordered."""
        return await self._spans.list_spans_in_window(t0, t1, run=run, kind=kind, filter_=filter_, order_by=order_by)

    async def get_trace(self, trace_id: str) -> MonitoringTrace:
        """Return the full :class:`MonitoringTrace` for ``trace_id``."""
        return await self._traces.get_trace(trace_id)

    async def get_observation(self, trace_id: str, observation_id: str) -> MonitoringObservation:
        """Return one observation of ``trace_id`` with its full input/output."""
        return await self._traces.get_observation(trace_id, observation_id)

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
        """List trace summaries in a time window, one page at a time, optionally filtered and ordered."""
        return await self._traces.list_traces(
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            limit=limit,
            page=page,
            filter_=filter_,
            order_by=order_by,
        )

    async def export_traces(
        self,
        *,
        from_timestamp: datetime | None = None,
        to_timestamp: datetime | None = None,
        max_rows: int,
        filter_: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[MonitoringTraceSummary]:
        """Up to ``max_rows + 1`` trace summaries in the ``list_traces`` order, in one read."""
        return await self._traces.export_traces(
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            max_rows=max_rows,
            filter_=filter_,
            order_by=order_by,
        )
