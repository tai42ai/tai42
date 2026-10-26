"""Response models for the observability operations: metrics, runs, traces, spans and capabilities."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, JsonValue

from tai42_skeleton.routers.observability_support import MetricsGranularity, RunSortKey, RunStatus


class MetricsSummary(BaseModel):
    """The dashboard summary tile derived from the ungrouped metrics row.

    ``timeToFirstTokenMs`` is always ``null`` (no neutral measure for it — kept
    for a stable shape).
    """

    totalRuns: int
    totalCost: float
    totalTokens: int
    averageLatencyMs: int
    avgCostPerRun: float
    avgTokensPerRun: int
    timeToFirstTokenMs: int | None = None


class MetricsTimePoint(BaseModel):
    """One granularity bucket of the dashboard series.

    ``bucket`` is the backend's time-field value, ``null`` when the row exposes no
    ISO-like bucket.
    """

    bucket: str | None = None
    runs: int
    cost: float
    avgLatencyMs: int
    totalTokens: int


class ModelUsageRow(BaseModel):
    """One per-model usage row (top 8 by cost).

    ``model`` is ``unknown`` when the backend named none.
    """

    model: str
    calls: int
    cost: float
    totalTokens: int
    avgLatencyMs: int


class MetricsResult(BaseModel):
    """The metrics body.

    The summary tile, the granularity series, the (optional, possibly-empty)
    per-model breakdown, and the resolved ``granularity``. ``byModelAvailable``
    is ``False`` when the backend does not declare the model dimension, so no
    by-model query is issued; a backend that declares it but cannot serve the
    query fails loudly, never a false ``byModelAvailable``.
    """

    summary: MetricsSummary
    timeSeries: list[MetricsTimePoint]
    byModel: list[ModelUsageRow]
    byModelAvailable: bool = True
    granularity: MetricsGranularity


class ObservabilityRunView(BaseModel):
    """One run-list row projected from a trace summary.

    ``inputPreview``/``outputPreview`` are server-bounded (structurally-clipped)
    JSON previews of the run's input and output; the full bodies live on the trace.
    Nullable aggregates are ``null`` when the backend returned none.
    """

    id: str
    traceId: str
    createdAt: str | None = None
    tags: list[str]
    status: RunStatus
    cost: float | None = None
    latencyMs: int | None = None
    totalTokens: int | None = None
    inputPreview: JsonValue
    outputPreview: JsonValue


class ObservabilityRunsPage(BaseModel):
    """One page of observability runs; ``nextPage`` is ``null`` on the last page."""

    items: list[ObservabilityRunView]
    page: int
    nextPage: int | None = None


class SpanOutlineView(BaseModel):
    """One span within a run trace, without its input and output.

    ``kind`` is the backend's observation type mapped onto the neutral kinds; the token
    counts are ``null`` when the producer reported none. ``metadata`` is the producer's
    metadata (open JSON), passed through whole, the platform reading no key out of it.
    """

    id: str
    parentId: str | None = None
    traceId: str | None = None
    name: str | None = None
    kind: Literal["LLM", "TOOL", "CHAIN", "EVENT"] | None = None
    level: str | None = None
    statusMessage: str | None = None
    start: str | None = None
    end: str | None = None
    model: str | None = None
    inputTokens: int | None = None
    outputTokens: int | None = None
    totalTokens: int | None = None
    metadata: JsonValue = None


class SpanView(SpanOutlineView):
    """One span within a run trace, with its free-form input and output (open JSON)."""

    input: JsonValue = None
    output: JsonValue = None


class RunTraceOutlineView(BaseModel):
    """The span tree of one run: every span without its input and output."""

    traceId: str
    spans: list[SpanOutlineView]


class ObservabilityMetricsCapabilities(BaseModel):
    """The neutral measures and dimensions the backend's metrics query serves."""

    measures: list[str]
    dimensions: list[str]


class ObservabilityCapabilities(BaseModel):
    """What the monitoring backend serves, in the run list's wire names.

    ``pageSizeMax`` is the largest run-list page; ``sortKeys`` the run-list sort keys it
    serves; ``incompatibleFilters`` maps a sort key to the run-list filter params it cannot
    be combined with.
    """

    pageSizeMax: int
    sortKeys: list[RunSortKey]
    incompatibleFilters: dict[str, list[str]]
    metrics: ObservabilityMetricsCapabilities


class RunTraceView(BaseModel):
    """The single-run detail.

    The trace's attributes (``input``/``output``/``metadata`` are open JSON) plus
    every span.
    """

    traceId: str
    timestamp: str | None = None
    tags: list[str]
    totalCost: float | None = None
    input: JsonValue = None
    output: JsonValue = None
    metadata: JsonValue = None
    spans: list[SpanView]


class ResolvedSpanValue(BaseModel):
    """One span field's value (at an RFC 6901 pointer) with every record reference resolved."""

    traceId: str
    spanId: str
    field: Literal["input", "output"]
    pointer: str
    value: JsonValue = None
