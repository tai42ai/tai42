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


@runtime_checkable
class MonitoringReader(Protocol):
    """Query totals/analytics and the runtime span window."""

    def metrics_capability(self) -> MetricsCapability:
        """Declare which neutral measures and dimensions ``query_metrics`` serves.

        A pure capability declaration (no I/O), so a caller can tell a panel the
        backend cannot serve from one it served empty, WITHOUT issuing a query that
        swallows the difference. ``query_metrics`` raises
        ``MonitoringReadNotSupportedError`` for anything outside this set.
        """
        ...

    def list_capability(self) -> ListCapability:
        """Declare which sorts ``list_traces`` serves and which filters each sort cannot combine with.

        A pure capability declaration (no I/O), so a caller offers only the sorts and
        filter combinations the backend serves. ``list_traces`` raises
        ``MonitoringReadNotSupportedError`` for anything outside it.
        """
        ...

    def max_page_size(self) -> int:
        """Declare the largest ``limit`` one ``list_traces`` call accepts.

        A pure declaration (no I/O). A ``limit`` above it raises ``ValueError``.
        """
        ...

    async def query_metrics(self, query: MetricsQuery) -> MetricsResult:
        """Totals / analytics screen — server-side aggregation.

        Aggregates the requested :class:`Measure` set grouped by the requested
        :class:`Dimension` set over the query window. A measure or dimension outside
        :meth:`metrics_capability` raises ``MonitoringReadNotSupportedError`` — never
        a silent zero.
        """
        ...

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
        """The smallest spans that ran in the half-open window ``[t0, t1)``.

        CONTRACT GUARANTEE: exactly one item per tool execution in the
        window. Every reader performs this tool-granularity selection — it is
        not an optional filter. A reader selects steps by the producers' marker:
        an observation whose metadata ``tai42.step_role`` is ``grouping`` or
        ``sub_step`` is never an item; generations and events are never items.

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
        """Fetch one COMPLETE trace (top-level attrs + every observation, with full input/output).

        Used for replay / evaluation / normalization. Always returns a trace or raises — never ``None``. An absent trace
        raises ``TraceNotFoundError``; a transient/backend failure (e.g. a timeout)
        propagates its error so the caller sees the failure and may retry.
        Distinct from ``list_spans_in_window`` (trimmed dashboard units).
        """
        ...

    async def get_observation(self, trace_id: str, observation_id: str) -> MonitoringObservation:
        """One observation of a trace, with its full input/output.

        An absent observation raises ``ObservationNotFoundError``; an absent trace raises
        ``TraceNotFoundError``; any other backend failure propagates.
        """
        ...

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
        """List run SUMMARIES matching the filters: one row per trace.

        Built from the backend's list surface plus its batched aggregates — never a per-trace body.
        ``get_trace`` is the only body door. A malformed backend row fails the page loudly (never kept
        as a partial row nor silently skipped).

        ``from_timestamp`` / ``to_timestamp`` bound the trace timestamp to the
        half-open window ``[from, to)``; either may be omitted for an open end.
        ``filter`` applies the neutral ``MonitoringFilter`` clauses (tags live
        here, not as a discrete param). ``order_by`` sorts the result; omitted ⇒
        newest-first (``timestamp`` desc). The sortable fields and the filters
        each sort cannot combine with are declared by ``list_capability``; a sort
        or combination outside it, or another unsupported filter clause, raises
        ``MonitoringReadNotSupportedError``. A ``limit`` above ``max_page_size()``
        raises ``ValueError``.
        """
        ...
