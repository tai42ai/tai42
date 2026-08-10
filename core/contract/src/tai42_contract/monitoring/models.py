"""Vendor-neutral data shapes for the monitoring contract.

These models are modeled on OpenTelemetry span semantics and carry no
backend-specific names. Cost / tokens / input / output are optional so a
pure-tracing backend (no LLM-observability extension) still fits.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SpanKind(StrEnum):
    """Neutral span category.

    Maps onto a backend's own type vocabulary by the implementation (the two are
    not 1:1 — a backend may render LLM as its generation type, TOOL/CHAIN as its
    span type, and EVENT as its event type).
    """

    LLM = "LLM"
    TOOL = "TOOL"
    CHAIN = "CHAIN"
    EVENT = "EVENT"


class MonitoringLevel(StrEnum):
    """Neutral severity level for spans and events."""

    DEBUG = "DEBUG"
    DEFAULT = "DEFAULT"
    WARNING = "WARNING"
    ERROR = "ERROR"


# The level applied to an event/span when the caller passes none. ``create_event``
# uses this as its default (the flows iterate events pass no level).
DEFAULT_LEVEL = MonitoringLevel.DEFAULT


class MetricsView(StrEnum):
    """Which records a metrics query aggregates over."""

    TRACES = "traces"
    OBSERVATIONS = "observations"


class ProjectConfig(BaseModel):
    """Credentials for one trace destination ("project"), selectable at write
    time via ``MonitoringWriter.scope(public_key)``.

    Registered on a multi-project backend through ``Monitoring.add_project``
    so an in-process component can emit to its own project while sharing the one
    backend. ``source`` stamps every write with an environment marker so several
    callers sharing a project can each read back only their own data. A backend
    with no multi-project notion ignores the extra fields.
    """

    model_config = ConfigDict(frozen=True)

    public_key: str
    secret_key: str
    host: str
    timeout_seconds: int = 30
    source: str = "tai"


@runtime_checkable
class Span(Protocol):
    """Handle to an open span, returned by ``MonitoringWriter.start_span``.

    Both methods are FAIL-SAFE: they catch their own errors and log — they
    never raise into application code (a monitoring outage must not break a
    flow).
    """

    @property
    def id(self) -> str:
        """This span's id, for explicitly threading a child's
        ``TraceContext.parent_span_id`` (OTel context propagation is
        unreliable across async boundaries)."""
        ...

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
        """Amend this span after it was opened.

        ``usage_details`` is the per-generation token/cost channel that feeds
        the totals/analytics metrics. ``model`` may be amended here when not
        known at open time.
        """
        ...

    def set_trace_metadata(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Set attributes on the enclosing trace from this span."""
        ...


class TraceContext(BaseModel):
    """Propagation context for a trace/span lineage.

    Built by the caller from the langgraph ``configurable`` (trace_id /
    parent_span_id) and carried downstream by ``inject_context`` /
    ``get_monitoring_callbacks``. ``tags`` drive the run/tag filtering on the
    totals screen; ``metadata`` carries attribution.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    trace_id: str | None = None
    parent_span_id: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class SpanWindowItem(BaseModel):
    """One tool/node execution returned by ``list_spans_in_window``.

    The "smallest span" unit (one tool/node run). ``tags`` come from the
    parent trace. ``input`` / ``output`` / ``metadata`` are nullable.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    parent_id: str | None = None
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None
    start: datetime
    end: datetime | None = None


class MetricsFilter(BaseModel):
    """A totals/analytics query, mapped to the backend metrics API.

    ``metrics`` is required; ``from_timestamp`` / ``to_timestamp`` are always
    required (the query is time-bound). The "run" axis has no native
    dimension — the implementation maps it to the trace identity (trace
    ``name`` or a run-id carried as a tag) and documents which it uses.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    view: MetricsView = MetricsView.TRACES
    metrics: list[str]
    from_timestamp: datetime
    to_timestamp: datetime
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    granularity: str | None = None
    order_by: list[dict[str, Any]] | None = None


class MetricsRow(BaseModel):
    """One grouped result row: its dimension values + the metric values."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    dimensions: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)


class MonitoringObservation(BaseModel):
    """One observation (span / generation / event) inside a full trace.

    Vendor-neutral: a backend maps its own observation shape onto this. Carries
    the full input/output and metadata so a reader can reconstruct/normalize an
    entire run (e.g. replay, evaluation), unlike ``SpanWindowItem`` which is the
    trimmed dashboard unit.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    trace_id: str | None = None
    parent_id: str | None = None
    # Free-form observation type from the backend (e.g. TOOL / GENERATION /
    # EVENT / SPAN / CHAIN). Kept as a string — backends differ on the set.
    type: str | None = None
    name: str | None = None
    level: str | None = None
    status_message: str | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None
    start: datetime | None = None
    end: datetime | None = None


class MonitoringTrace(BaseModel):
    """A complete trace: its top-level attributes plus every observation.

    The neutral return of ``MonitoringReader.get_trace`` / ``list_traces``.

    ``fetch_error`` is set when the trace body could not be loaded (e.g. the
    backend rejected the read). When set, the other fields are empty/partial and
    only ``id`` is reliable. None on a fully-loaded trace.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    timestamp: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None
    total_cost: float | None = None
    observations: list[MonitoringObservation] = Field(default_factory=list[MonitoringObservation])
    fetch_error: str | None = None


class MetricsResult(BaseModel):
    """The result of a metrics query.

    ``rows`` are the dimensioned groups. ``derived`` holds reader-computed
    scalars that are not native backend metrics — e.g. the distinct-tag count
    (the number of returned tag-groups).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    rows: list[MetricsRow] = Field(default_factory=list[MetricsRow])
    derived: dict[str, Any] = Field(default_factory=dict)


class OrderBy(BaseModel):
    """A single sort key for ``list_traces`` / ``list_spans_in_window``.

    ``field`` names a NEUTRAL, scalar/comparable model field (or a derived sort
    key the reader documents). Only one key — no multi-key / tie-break order.
    A field a backend cannot sort on raises ``MonitoringReadNotSupportedError``.

    Sortable keys:
    - ``list_traces`` (over ``MonitoringTrace``): ``timestamp`` (default),
      ``total_cost``, ``name``, ``id``, ``latency``, ``total_tokens``.
      ``total_cost`` / ``latency`` / ``total_tokens`` are aggregated-metric
      sorts: the backend ranks GLOBALLY and returns the requested ``limit`` /
      ``page`` slice of that ranking, not a re-rank of one fetched page. Like
      ``duration`` below, ``latency`` and ``total_tokens`` are sort-only derived
      keys — sorted by, but NOT fields on the returned ``MonitoringTrace`` (only
      ``total_cost`` is); not read back.
    - ``list_spans_in_window`` (over ``SpanWindowItem``): ``start`` (default),
      ``end``, ``duration`` (derived ``end - start``), ``name``, ``id``.

    ``tags`` / ``input`` / ``output`` / ``metadata`` are not sortable. An open
    span (missing ``start`` / ``end``) sorts LAST. When ``order_by`` is omitted
    the default is newest-first (``timestamp`` / ``start`` descending).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    field: str
    direction: Literal["asc", "desc"] = "desc"


class MonitoringFilter(BaseModel):
    """Vendor-neutral filter set for ``list_traces`` / ``list_spans_in_window``.

    Every field is optional; an unset field is not filtered on. Each ``metadata``
    entry is one equality match on the neutral ``metadata`` field. The min/max
    bounds are inclusive ranges over cost / token / latency totals; an inverted
    range (``min`` greater than ``max``) is rejected at construction. A backend
    that cannot honor a requested clause raises ``MonitoringReadNotSupportedError`` —
    it never silently drops the clause.

    ``latency`` is measured in seconds; ``cost`` in the backend's cost unit;
    ``tokens`` is the total token count.

    ``metadata`` values are strings: the clause is a string equality match, so
    a non-string attribute must be filtered by its string form. (Stored
    metadata elsewhere is ``Any``; the equality filter is deliberately narrower.)
    ``name`` / ``user_id`` / ``session_id`` / ``model`` filter on backend
    attributes that are not all present on the neutral result models.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    level: MonitoringLevel | None = None
    model: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    min_cost: float | None = None
    max_cost: float | None = None
    min_tokens: int | None = None
    max_tokens: int | None = None
    min_latency: float | None = None
    max_latency: float | None = None

    @model_validator(mode="after")
    def _check_ranges(self) -> MonitoringFilter:
        for lo, hi, name in (
            (self.min_cost, self.max_cost, "cost"),
            (self.min_tokens, self.max_tokens, "tokens"),
            (self.min_latency, self.max_latency, "latency"),
        ):
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"min_{name} ({lo}) must not exceed max_{name} ({hi})")
        return self
