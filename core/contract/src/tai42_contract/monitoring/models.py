"""Vendor-neutral data shapes for the monitoring contract.

These models are modeled on OpenTelemetry span semantics and carry no
backend-specific names. Cost / tokens / input / output are optional so a
pure-tracing backend (no LLM-observability extension) still fits.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


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

    The neutral return of ``MonitoringReader.get_trace`` (the only body door).
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


# Structural bounds for a run-list preview: keep the row payload small while
# staying VALID JSON (clip at the structure level, not mid-string), so the client
# always renders the preview as a parsed tree. The full value lives on
# ``get_trace``. ``TRACE_PREVIEW_MAX_CHARS`` caps a string leaf; the field name
# says "preview", so the cut is explicit, not a silent truncation of a complete
# value.
TRACE_PREVIEW_MAX_CHARS = 512  # max chars per string leaf
_PREVIEW_ITEMS = 20  # max entries per object / array
_PREVIEW_DEPTH = 6  # max nesting depth
_PREVIEW_PARSE_MAX = 20_000  # never structurally parse a string larger than this


def _maybe_json(text: str) -> Any:
    """Parse a string that is itself an object/array — JSON first, then a Python
    ``repr`` (single quotes, ``True``/``False``/``None``) via ``literal_eval`` —
    else ``None``. Lets a stringified structure be bounded structurally (and
    rendered as a tree) instead of char-clipped into garbage. A string over
    ``_PREVIEW_PARSE_MAX`` is not parsed — it would be fully materialized just to
    keep a few items — so the caller char-clips it instead."""
    s = text.strip()
    if len(s) < 2 or len(s) > _PREVIEW_PARSE_MAX or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        pass
    try:
        # literal_eval is safe — only Python literals, no code execution.
        return ast.literal_eval(s)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None


def preview(value: Any, depth: int = 0) -> JsonValue:
    """A structurally-bounded run-list preview of a trace's input/output: string
    leaves cut to ``TRACE_PREVIEW_MAX_CHARS``, objects/arrays capped at
    ``_PREVIEW_ITEMS`` entries, nesting elided past ``_PREVIEW_DEPTH`` — each with
    a terminal marker — so the result is small but still valid JSON the client
    renders as a tree. ``None`` stays ``None``; a stringified structure is parsed
    and bounded, a plain string or non-JSON leaf (dates, etc.) char-clipped. The
    cut is by design — the full value lives on ``get_trace``."""
    if isinstance(value, str):
        parsed = _maybe_json(value)
        if isinstance(parsed, (dict, list)):
            return preview(parsed, depth)
        return value if len(value) <= TRACE_PREVIEW_MAX_CHARS else value[:TRACE_PREVIEW_MAX_CHARS] + "…"
    if isinstance(value, dict):
        obj = cast("dict[Any, Any]", value)
        if depth >= _PREVIEW_DEPTH:
            return {"…": "…"}
        out: dict[str, JsonValue] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= _PREVIEW_ITEMS:
                out["…"] = f"+{len(obj) - _PREVIEW_ITEMS} more"
                break
            out[str(k)] = preview(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        seq = cast("list[Any] | tuple[Any, ...]", value)
        if depth >= _PREVIEW_DEPTH:
            return ["…"]
        items: list[JsonValue] = [preview(v, depth + 1) for v in seq[:_PREVIEW_ITEMS]]
        if len(seq) > _PREVIEW_ITEMS:
            items.append(f"+{len(seq) - _PREVIEW_ITEMS} more")
        return items
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)  # dates, etc. — JSON-safe fallback
    return text if len(text) <= TRACE_PREVIEW_MAX_CHARS else text[:TRACE_PREVIEW_MAX_CHARS] + "…"


class MonitoringTraceSummary(BaseModel):
    """One run-list row: the backend's list-surface attributes plus its batched
    aggregates, never a per-trace body.

    ``input_preview`` / ``output_preview`` are server-bounded previews (see
    ``preview``) — a structurally-clipped JSON value; the full input/output are
    read through ``get_trace``. ``total_tokens`` is ``None`` when the backend
    returned no usage for the trace (never coerced to ``0``). ``status`` is
    ``error`` when the run carries an error observation, ``ok`` otherwise — a
    malformed backend row fails the page loudly rather than being kept as a
    partial row."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    timestamp: datetime | None = None
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    input_preview: JsonValue = None
    output_preview: JsonValue = None
    latency_ms: float | None = None
    total_cost: float | None = None
    total_tokens: int | None = None
    status: Literal["ok", "error"]


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
    - ``list_traces`` (over ``MonitoringTraceSummary``): ``timestamp`` (default),
      ``total_cost``, ``name``, ``id``, ``latency``, ``total_tokens``.
      ``total_cost`` / ``latency`` / ``total_tokens`` are aggregated-metric
      sorts: the backend ranks GLOBALLY and returns the requested ``limit`` /
      ``page`` slice of that ranking, not a re-rank of one fetched page. All
      three are read back on the summary (``total_cost``, ``latency_ms``,
      ``total_tokens``).
    - ``list_spans_in_window`` (over ``SpanWindowItem``): ``start`` (default),
      ``end``, ``duration`` (derived ``end - start``), ``name``, ``id``.

    ``tags`` / ``input`` / ``output`` / ``metadata`` are not sortable. An open
    span (missing ``start`` / ``end``) sorts LAST. When ``order_by`` is omitted
    the default is newest-first (``timestamp`` / ``start`` descending).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    field: str
    direction: Literal["asc", "desc"] = "desc"


class RunAttribution(BaseModel):
    """The generic identity a run's trace is tagged with at the shared chokepoint.

    A pure key/value attribution envelope — every field is generic and optional
    where a door legitimately lacks it. ``tags`` are the flow kwargs the operator
    defines per delivery, carried verbatim with no platform interpretation;
    ``metadata`` are attribution key/values. There is NO tenant/client/domain
    field: attribution only, never a multi-tenant qualifier.
    """

    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
