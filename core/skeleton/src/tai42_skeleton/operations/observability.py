"""Observability read operations — ``/api/observability/*``.

Account-wide runs observability sourced exclusively from the vendor-neutral
monitoring READ contract (``tai42_contract.monitoring``): aggregate metrics, a
filterable run list, and single-run trace detail. A "run" is a monitoring
**trace** (keyed by ``trace_id``); there is no other source.

The reader is fetched per operation from the process monitoring registry
(``get_monitoring().reader``) so a reload swaps the backend without stale
capture; with no backend registered the no-op reader answers with EMPTY data
(not an error). Two typed monitoring errors are mapped: a read the backend
cannot serve becomes ``NotSupportedError`` (501, carrying the ``code`` the UI
keys its dedicated state on) and an absent trace becomes ``NotFoundError`` (404).
Every other reader error propagates loudly as a 500. The query string is decoded
into the neutral filter/paging types at the HTTP edge (the router's context
extractors, which raise ``BadRequestError`` → 400); these operations receive the
already-parsed flat params and stay request-free.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from tai42_contract.monitoring import (
    Dimension,
    Measure,
    MetricsQuery,
    MonitoringFilter,
    MonitoringReadNotSupportedError,
    ObservationNotFoundError,
    OrderBy,
    PayloadRefUnresolvedError,
    TraceNotFoundError,
)
from tai42_kit.monitoring import payload_ref, resolve_refs

from tai42_skeleton.monitoring.registry import get_monitoring
from tai42_skeleton.operations import BadRequestError, NotFoundError, NotSupportedError, UpstreamError, operation
from tai42_skeleton.operations.response_models_group_c import (
    MetricsResult,
    ObservabilityCapabilities,
    ObservabilityRunsPage,
    ResolvedSpanValue,
    RunTraceOutlineView,
    RunTraceView,
)
from tai42_skeleton.routers.observability_support import (
    ExportFormat,
    MetricsGranularity,
    RunSortKey,
    RunStatus,
    SortDirection,
    capabilities_view,
    derive_run,
    map_model_rows,
    map_trace,
    map_trace_outline,
    summary_from_rows,
    time_series_from_rows,
)

# The neutral measures the dashboard always requests: run count, cost, tokens and
# latency. A backend that does not serve all of them cannot drive the core tiles.
_MEASURES = [Measure.COUNT, Measure.COST, Measure.TOKENS, Measure.LATENCY]

# The ``code`` a monitoring read-not-supported answer carries so the UI can
# render its dedicated 'read not supported' state (not a generic error).
_READ_NOT_SUPPORTED_CODE = "monitoring-read-not-supported"


class MetricsQuerySpec(BaseModel):
    """The metrics door's optional time window and granularity.

    Spec metadata only — the door parses its query at the HTTP edge. Distinct from
    the contract's ``MetricsQuery`` (the neutral aggregation request the reader runs).
    """

    from_: str | None = Field(
        default=None,
        alias="from",
        description="Range start as an ISO instant or a relative token (e.g. ``30d``); defaults to 30 days ago.",
    )
    to: str | None = Field(
        default=None, description="Range end as an ISO instant or a relative token; defaults to now."
    )
    granularity: MetricsGranularity | None = Field(
        default=None, description="Bucket size; auto-selected from the span when omitted."
    )


class RunFilterQuery(BaseModel):
    """The run time window, advanced filters, and sort shared by the run-list and export doors.

    Exactly what ``parse_time_range`` + ``parse_run_filter`` read at the HTTP edge.

    Spec metadata only — the doors parse their query at the HTTP edge. Beyond the declared
    fields, any ``meta.<key>=<value>`` query param adds one metadata string-equality clause
    (dynamic keys, so not a fixed field); an empty key or value is a 400.
    """

    from_: str | None = Field(
        default=None,
        alias="from",
        description="Range start as an ISO instant or a relative token (e.g. ``30d``); defaults to 30 days ago.",
    )
    to: str | None = Field(
        default=None, description="Range end as an ISO instant or a relative token; defaults to now."
    )
    tags: str | None = Field(
        default=None, description='Tag filter as a JSON list (``["a","b"]``) or a comma-separated string.'
    )
    status: RunStatus | None = Field(default=None, description="Restrict to ``error`` runs; ``success`` is unfiltered.")
    user: str | None = Field(
        default=None, description="Filter to one run user (the backend's native user identity dimension)."
    )
    session: str | None = Field(
        default=None, description="Filter to one run session/thread (the backend's native session dimension)."
    )
    version: str | None = Field(
        default=None,
        description="Filter to one run version (the backend's native version dimension; e.g. a preset's version).",
    )
    min_cost: float | None = Field(default=None, alias="minCost", description="Inclusive lower bound on run cost.")
    max_cost: float | None = Field(default=None, alias="maxCost", description="Inclusive upper bound on run cost.")
    min_tokens: float | None = Field(
        default=None, alias="minTokens", description="Inclusive lower bound on token count."
    )
    max_tokens: float | None = Field(
        default=None, alias="maxTokens", description="Inclusive upper bound on token count."
    )
    min_latency_ms: float | None = Field(
        default=None, alias="minLatencyMs", description="Inclusive lower bound on latency in milliseconds."
    )
    max_latency_ms: float | None = Field(
        default=None, alias="maxLatencyMs", description="Inclusive upper bound on latency in milliseconds."
    )
    sort: RunSortKey | None = Field(default=None, description="Sort key.")
    dir: SortDirection | None = Field(default=None, description="Sort direction; defaults to ``desc``.")


class RunsListQuery(RunFilterQuery):
    """The run-list door's paging atop the shared run filter."""

    page: int = Field(default=1, ge=1, description="1-based page number.")
    page_size: int = Field(
        default=50,
        ge=1,
        alias="pageSize",
        description="Items per page; capped to the monitoring backend's declared maximum, never refused.",
    )


class ExportRunsQuery(RunFilterQuery):
    """The export door's output format atop the shared run filter."""

    format: ExportFormat = Field(default="csv", description="Download format; defaults to ``csv``.")


@operation(
    summary="Query observability metrics",
    tags=["observability"],
    errors=[BadRequestError, NotSupportedError],
    request_model=MetricsQuerySpec,
    response_model=MetricsResult,
)
async def get_metrics(t0: datetime, t1: datetime, granularity: str) -> dict:
    """Aggregate metrics over the time range via the contract's ``query_metrics``.

    The backend DECLARES what it serves (``metrics_capability``) before any query.
    A backend that does not serve the core measures fails loudly (501). The by-model
    panel's availability is the backend's DECLARATION that it groups by the model
    dimension — not a swallowed query error: when declared, the by-model query runs
    HARD and any failure propagates; when not declared, the panel is reported absent
    and no query is issued.
    """
    reader = get_monitoring().reader
    capability = reader.metrics_capability()

    missing = [m for m in _MEASURES if m not in capability.measures]
    if missing:
        raise NotSupportedError(
            f"monitoring backend does not serve the metrics measures {[m.value for m in missing]}",
            extra={"code": _READ_NOT_SUPPORTED_CODE},
        )
    by_model_available = Dimension.MODEL in capability.dimensions

    summary_query = MetricsQuery(measures=_MEASURES, from_timestamp=t0, to_timestamp=t1)
    series_query = MetricsQuery(measures=_MEASURES, from_timestamp=t0, to_timestamp=t1, granularity=granularity)
    pending = [reader.query_metrics(summary_query), reader.query_metrics(series_query)]
    if by_model_available:
        model_query = MetricsQuery(measures=_MEASURES, from_timestamp=t0, to_timestamp=t1, dimensions=[Dimension.MODEL])
        pending.append(reader.query_metrics(model_query))

    try:
        results = await asyncio.gather(*pending)
    except MonitoringReadNotSupportedError as exc:
        raise NotSupportedError(str(exc), extra={"code": _READ_NOT_SUPPORTED_CODE}) from exc

    model_res = results[2] if by_model_available else None
    return {
        "summary": summary_from_rows(results[0].rows),
        "timeSeries": time_series_from_rows(results[1].rows),
        "byModel": map_model_rows(model_res),
        "byModelAvailable": by_model_available,
        "granularity": granularity,
    }


@operation(
    summary="Get the monitoring backend's observability capabilities",
    tags=["observability"],
    errors=[],
    response_model=ObservabilityCapabilities,
)
async def get_observability_capabilities() -> dict:
    """The run-list page ceiling, sorts and sort/filter incompatibilities, and the metrics measures and dimensions.

    Read from the backend's pure declarations (``max_page_size``, ``list_capability``,
    ``metrics_capability``) in the run list's wire names; no backend query is issued.
    """
    reader = get_monitoring().reader
    return capabilities_view(reader.list_capability(), reader.metrics_capability(), reader.max_page_size())


@operation(
    summary="List observability runs",
    tags=["observability"],
    errors=[BadRequestError, NotSupportedError],
    request_model=RunsListQuery,
    response_model=ObservabilityRunsPage,
)
async def list_observability_runs(
    t0: datetime,
    t1: datetime,
    run_filter: MonitoringFilter | None,
    order_by: OrderBy | None,
    page: int,
    page_size: int,
) -> dict:
    """Filterable run list via the contract's ``list_traces``, paged with the reader's ``limit`` / ``page``.

    Time range plus the neutral advanced filters (tags / status / cost / token / latency
    ranges) and sort. ``page_size`` is capped to the backend's declared ``max_page_size``.

    List- or dict-typed query params are JSON-encoded in the query string
    (``tags`` may be a JSON list ``["a","b"]`` or a comma-separated string).
    """
    reader = get_monitoring().reader
    page_size = min(page_size, reader.max_page_size())
    try:
        summaries = await reader.list_traces(
            from_timestamp=t0,
            to_timestamp=t1,
            limit=page_size,
            page=page,
            filter_=run_filter,
            order_by=order_by,
        )
    except MonitoringReadNotSupportedError as exc:
        raise NotSupportedError(str(exc), extra={"code": _READ_NOT_SUPPORTED_CODE}) from exc

    items = [derive_run(s) for s in summaries]
    next_page = page + 1 if len(summaries) >= page_size else None
    return {"items": items, "page": page, "nextPage": next_page}


@operation(
    summary="Get a run's trace",
    tags=["observability"],
    errors=[NotFoundError, NotSupportedError],
    response_model=RunTraceView,
)
async def get_run_trace(trace_id: str) -> dict:
    """Full detailed trace for one run via the contract's ``get_trace``.

    The contract's ``get_trace`` always returns a trace or raises: an absent trace
    is ``TraceNotFoundError`` → 404, and every other backend error propagates as a
    loud 500.
    """
    reader = get_monitoring().reader
    try:
        trace = await reader.get_trace(trace_id)
    except MonitoringReadNotSupportedError as exc:
        raise NotSupportedError(str(exc), extra={"code": _READ_NOT_SUPPORTED_CODE}) from exc
    except TraceNotFoundError as exc:
        raise NotFoundError("Run trace not found") from exc
    return map_trace(trace)


@operation(
    summary="Get a run's trace outline",
    tags=["observability"],
    errors=[NotFoundError, NotSupportedError],
    response_model=RunTraceOutlineView,
)
async def get_run_trace_outline(trace_id: str) -> dict:
    """The span tree of one run via the contract's ``get_trace``: every span without its input and output.

    An absent trace is a 404 and a read the backend cannot serve a 501, exactly as ``get_run_trace``.
    """
    reader = get_monitoring().reader
    try:
        trace = await reader.get_trace(trace_id)
    except MonitoringReadNotSupportedError as exc:
        raise NotSupportedError(str(exc), extra={"code": _READ_NOT_SUPPORTED_CODE}) from exc
    except TraceNotFoundError as exc:
        raise NotFoundError("Run trace not found") from exc
    return map_trace_outline(trace)


class ResolvedSpanValueQuery(BaseModel):
    """Which field of the span to resolve, and where inside it."""

    field: Literal["input", "output"] = Field(description="The span field to resolve.")
    pointer: str = Field(
        default="",
        description="RFC 6901 JSON pointer into the field's value; empty for the whole value.",
    )

    @field_validator("pointer")
    @classmethod
    def _pointer_is_rfc6901(cls, value: str) -> str:
        if value and not value.startswith("/"):
            raise ValueError("pointer must be empty or start with '/' (RFC 6901)")
        return value


@operation(
    summary="Get a run span's resolved value",
    tags=["observability"],
    errors=[NotFoundError, NotSupportedError, UpstreamError],
    request_model=ResolvedSpanValueQuery,
    response_model=ResolvedSpanValue,
)
async def get_resolved_span_value(
    trace_id: str, span_id: str, field: Literal["input", "output"], pointer: str = ""
) -> dict:
    """One span field's value at ``pointer``, with every record reference on the way and inside it resolved.

    The pointer is applied exactly as a reference's pointer: a step that lands on a
    reference resolves it and continues inside its value. An absent trace or span is a 404;
    a reference the backend does not hold (not yet ingested, or lost), or a pointer the
    record does not hold, is a 502 carrying the reason.
    """
    reader = get_monitoring().reader
    try:
        await reader.get_observation(trace_id, span_id)
        value = await resolve_refs(payload_ref(span_id, field, pointer, trace_id=trace_id), reader, trace_id=trace_id)
    except MonitoringReadNotSupportedError as exc:
        raise NotSupportedError(str(exc), extra={"code": _READ_NOT_SUPPORTED_CODE}) from exc
    except (TraceNotFoundError, ObservationNotFoundError) as exc:
        raise NotFoundError("Run span not found") from exc
    except PayloadRefUnresolvedError as exc:
        raise UpstreamError(str(exc)) from exc
    return {"traceId": trace_id, "spanId": span_id, "field": field, "pointer": pointer, "value": value}
