"""The read/query half of the monitoring contract.

Implement only if the backend has a read API. A read-incapable backend still
exposes a ``reader`` object, but its methods raise ``MonitoringReadNotSupportedError``
on call (never a silent no-op).

OpenTelemetry has no read/query standard — each backend invents its own read
surface — so this face is the abstraction's real value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tai42_contract.monitoring.models import (
    MetricsFilter,
    MetricsResult,
    MonitoringFilter,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
    SpanKind,
    SpanWindowItem,
)


@runtime_checkable
class MonitoringReader(Protocol):
    """Query totals/analytics and the runtime span window."""

    async def query_metrics(self, filter: MetricsFilter) -> MetricsResult:
        """Totals / analytics screen — server-side aggregation.

        Counts (runs, tags), total cost, total tokens, latency, grouped by the
        requested dimensions.
        """
        ...

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
        """The smallest spans that ran in the half-open window ``[t0, t1)``.

        CONTRACT GUARANTEE: exactly one item per tool/node execution in the
        window. Every reader performs this tool-granularity selection — it is
        not an optional filter.

        ``run`` scopes to a single run (one trace); ``None`` = all runs in the
        window. ``kind`` narrows WITHIN the tool-granularity set (never widens
        or overrides the guarantee). ``filter`` applies the neutral
        ``MonitoringFilter`` clauses (tags live here, not as a discrete param).
        ``order_by`` sorts the result; omitted ⇒ newest-first (``start`` desc).
        An unsupported ``order_by.field`` or filter clause raises
        ``MonitoringReadNotSupportedError``.
        """
        ...

    async def get_trace(self, trace_id: str) -> MonitoringTrace:
        """Fetch one COMPLETE trace (top-level attrs + every observation, with
        full input/output) for replay / evaluation / normalization.

        Always returns a trace or raises — never ``None``. An absent trace
        raises ``TraceNotFoundError``; a transient/backend failure (e.g. a timeout)
        propagates its error so the caller sees the failure and may retry.
        Distinct from ``list_spans_in_window`` (trimmed dashboard units).
        """
        ...

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
        """List run SUMMARIES matching the filters: one row per trace, built from
        the backend's list surface plus its batched aggregates — never a
        per-trace body. ``get_trace`` is the only body door. A malformed backend
        row fails the page loudly (never kept as a partial row nor silently
        skipped).

        ``from_timestamp`` / ``to_timestamp`` bound the trace timestamp to the
        half-open window ``[from, to)``; either may be omitted for an open end.
        ``filter`` applies the neutral ``MonitoringFilter`` clauses (tags live
        here, not as a discrete param). ``order_by`` sorts the result; omitted ⇒
        newest-first (``timestamp`` desc). Sortable fields: ``timestamp``,
        ``total_cost``, ``name``, ``id``, ``latency``, ``total_tokens`` — of
        these ``total_cost`` / ``latency`` / ``total_tokens`` rank globally (see
        ``OrderBy``); the rest sort natively. An unsupported ``order_by.field``
        or filter clause raises ``MonitoringReadNotSupportedError``.
        """
        ...
