"""The Langfuse read/query implementation of ``MonitoringReader``.

- ``query_metrics`` -> the Metrics API.
- ``list_spans_in_window`` -> the Observations API (one item per tool/node run).
- ``get_trace`` / ``list_traces`` -> the Traces API.

``LangfuseReader`` is a thin façade: it composes one query object per API surface
and delegates each contract method to it. The reader methods are ``async`` but the
Langfuse client is synchronous, so every blocking call is dispatched via
``asyncio.to_thread``. Every read is scoped to the active project's ``source`` (the
Langfuse ``environment``) so a shared project returns only our rows; ``get_trace``
is exempt, since a trace id is globally unique.
"""

from __future__ import annotations

from datetime import datetime

from tai42_contract.monitoring import (
    MetricsFilter,
    MetricsResult,
    MonitoringFilter,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
    SpanKind,
    SpanWindowItem,
)

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager
from tai42_monitoring_langfuse.metrics_query import MetricsQuery
from tai42_monitoring_langfuse.span_window import SpanWindowQuery
from tai42_monitoring_langfuse.trace_query import TraceQuery


class LangfuseReader:
    """Serves the contract read surface from the Langfuse query APIs."""

    def __init__(self, manager: LangfuseClientManager) -> None:
        self._metrics = MetricsQuery(manager)
        self._spans = SpanWindowQuery(manager)
        self._traces = TraceQuery(manager)

    async def query_metrics(self, filter: MetricsFilter) -> MetricsResult:
        return await self._metrics.query_metrics(filter)

    async def list_spans_in_window(
        self,
        t0: datetime,
        t1: datetime,
        *,
        run: str | None = None,
        kind: SpanKind | None = None,
        filter: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[SpanWindowItem]:
        return await self._spans.list_spans_in_window(t0, t1, run=run, kind=kind, filter=filter, order_by=order_by)

    async def get_trace(self, trace_id: str) -> MonitoringTrace:
        return await self._traces.get_trace(trace_id)

    async def list_traces(
        self,
        *,
        from_timestamp: datetime | None = None,
        to_timestamp: datetime | None = None,
        limit: int | None = None,
        page: int | None = None,
        filter: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[MonitoringTraceSummary]:
        return await self._traces.list_traces(
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            limit=limit,
            page=page,
            filter=filter,
            order_by=order_by,
        )
