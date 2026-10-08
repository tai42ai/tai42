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


class Measure(StrEnum):
    """A neutral quantity a metrics query aggregates.

    ``COUNT`` is the number of records in the aggregation scope — runs for an
    ungrouped query, per-group records when grouped (e.g. per-model calls).
    ``COST`` / ``TOKENS`` / ``LATENCY`` are the cost, token and latency aggregates.
    A backend maps each onto its own metric names and declares the set it serves
    through :class:`MetricsCapability`; a query for a measure it does not serve
    raises ``MonitoringReadNotSupportedError``.
    """

    COUNT = "count"
    COST = "cost"
    TOKENS = "tokens"
    LATENCY = "latency"


class Dimension(StrEnum):
    """A neutral attribute a metrics query groups by.

    ``MODEL`` groups the aggregates per model. A backend maps it onto its own
    per-generation model attribute and declares whether it serves it through
    :class:`MetricsCapability`; a query that groups by a dimension the backend does
    not serve raises ``MonitoringReadNotSupportedError`` rather than silently
    returning one ungrouped total.
    """

    MODEL = "model"


class MetricsCapability(BaseModel):
    """What a monitoring backend's metrics query can serve.

    A backend declares the neutral :class:`Measure` set it can aggregate and the
    :class:`Dimension` set it can group by. A query that requests a measure or
    dimension outside this set raises ``MonitoringReadNotSupportedError`` — never a
    silent zero, never a silently dropped group. A reader consults this before a
    query so an unserved panel is reported as declared-absent, not as a swallowed
    failure.
    """

    model_config = ConfigDict(frozen=True)

    measures: frozenset[Measure]
    dimensions: frozenset[Dimension]


class ListCapability(BaseModel):
    """What a backend's ``list_traces`` can sort on, and which filters each sort cannot combine with.

    A pure declaration (no I/O). ``sort_fields`` are the ``OrderBy.field`` values
    ``list_traces`` serves. ``incompatible_filters`` maps a sort field to the
    :class:`MonitoringFilter` field names it cannot be combined with; a sort field
    absent from the map combines with every filter. ``list_traces`` raises
    ``MonitoringReadNotSupportedError`` for a sort or a combination outside this
    declaration.
    """

    model_config = ConfigDict(frozen=True)

    sort_fields: frozenset[str]
    incompatible_filters: dict[str, frozenset[str]] = Field(default_factory=dict[str, frozenset[str]])


class TokenUsage(BaseModel):
    """Token counts and cost of one model call; each field is ``None`` when the producer did not report it."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None


@runtime_checkable
class Span(Protocol):
    """Handle to an open span, returned by ``MonitoringWriter.open_span`` / ``start_span``.

    Every method is FAIL-SAFE: it catches its own errors and logs them at ERROR, and the
    writer counts them in ``export_health().records_failed`` — it never raises into
    application code (a monitoring outage must not break a flow).
    """

    @property
    def id(self) -> str:
        """This span's id, for explicitly threading a child's ``TraceContext.parent_span_id``.

        OTel context propagation is unreliable across async boundaries.
        """
        ...

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
        """Amend this span after it was opened.

        ``usage`` is the per-generation token/cost channel that feeds the totals/analytics
        metrics. ``model`` may be amended here when not known at open time. ``metadata`` is
        MERGED key-wise into the metadata given so far (a later value for a key wins), never
        a replacement of the whole object.
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

    def end(self, *, end_time: datetime | None = None) -> None:
        """End this span (at ``end_time``, else now) and record it.

        A span opened with ``activate=True`` stops being the current span here and the
        previous current span is restored. A span yielded by ``start_span`` is ended by its
        context manager.
        """
        ...


class TraceContext(BaseModel):
    """Propagation context for a trace/span lineage.

    Built by the caller from the downstream run config (trace_id /
    parent_span_id) and carried to ``open_span`` / ``start_span`` / ``record_span`` /
    ``create_event`` and to the kit's ``bind_run_trace``. ``tags`` drive the run/tag
    filtering on the totals screen; ``metadata`` carries attribution.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    trace_id: str | None = None
    parent_span_id: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class RecordId(BaseModel):
    """The identity of one written record: its trace (32 lower-hex) and its span (16 lower-hex)."""

    model_config = ConfigDict(frozen=True)

    trace_id: str
    span_id: str


# THE RESERVED MARKER NAMESPACE. A JSON object whose FIRST key starts with this prefix
# exists in a recorded value only when the writer itself rendered a marker (a reference,
# the unrecorded statement, an encoded byte string, an unencodable value). Producers pass
# the kit's marker objects, never hand-built marker dicts; a user value carrying such an
# object is refused by the writer's encoder, so a decoded one-key ``$tai42_*`` object was
# always written by the writer and a reference is never confused with data.
RESERVED_KEY_PREFIX = "$tai42_"
# The one key of a reference object: ``{"$tai42_ref": {<PayloadRef fields>}}``.
PAYLOAD_REF_KEY = "$tai42_ref"
# The one key of ``{"$tai42_unrecorded": true}``: a value existed but was produced while
# nothing recorded. It is not a reference; resolution leaves it in place.
UNRECORDED_KEY = "$tai42_unrecorded"


class PayloadRef(BaseModel):
    """A reference from one record's value to the record that holds it.

    Embedded in any input/output/metadata value as ``{"$tai42_ref": {<these fields>}}``.
    ``trace_id`` ``None`` names the trace of the record that carries the reference.
    ``field`` names the target record's input, output, or producer metadata object.
    ``pointer`` is an RFC 6901 JSON pointer into that field's value; ``""`` is the whole value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str | None = None
    span_id: str
    field: Literal["input", "output", "metadata"]
    pointer: str = ""


class StepRole(StrEnum):
    """What a record is in a run's step outline; a record without the marker is a step."""

    GROUPING = "grouping"  # a framework grouping node: holds steps, is not one
    SUB_STEP = "sub_step"  # a step's internal evaluation: part of a step, not one


# Metadata key of the record's :class:`StepRole`.
STEP_ROLE_METADATA_KEY = "tai42.step_role"
# Metadata key of a record's timing statement; ``TIMING_ABSENT`` = the record carries no live timing.
TIMING_METADATA_KEY = "tai42.timing"
TIMING_ABSENT = "absent"
# Metadata key of a generation's full generated-message record (kept in the metadata object, not promoted).
GENERATION_MESSAGE_METADATA_KEY = "tai42.message"
# Metadata keys a writer stores as their own record attributes rather than inside the metadata object.
PROMOTED_METADATA_KEYS: frozenset[str] = frozenset({STEP_ROLE_METADATA_KEY, TIMING_METADATA_KEY})


class MonitoringExportHealth(BaseModel):
    """What a writer could not deliver, cumulative since the writer was built in this process."""

    model_config = ConfigDict(frozen=True)

    spans_dropped: int = 0  # evicted by a full export queue
    spans_failed: int = 0  # in a batch the exporter could not deliver
    export_failures: int = 0  # export calls that failed (FAILURE result or exception)
    attributes_dropped: int = 0  # attributes evicted from exported spans
    records_failed: int = 0  # records (or one field of a record) the writer could not build
    last_error: str | None = None


class SpanWindowItem(BaseModel):
    """One tool execution returned by ``list_spans_in_window``.

    The "smallest span" unit (one tool run). ``tags`` come from the
    parent trace. ``input`` / ``output`` / ``metadata`` are nullable.
    ``tags_available`` is ``False`` when the parent trace's tags could not be
    fetched — distinct from an empty ``tags`` meaning the span is genuinely
    untagged; a reader shows 'unavailable', never 'untagged'.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    parent_id: str | None = None
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    tags_available: bool = True
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None
    start: datetime
    end: datetime | None = None


class MetricsQuery(BaseModel):
    """A totals/analytics aggregation request in neutral vocabulary.

    ``measures`` are the quantities to aggregate (at least one). ``from_timestamp``
    / ``to_timestamp`` bound the half-open window ``[from, to)`` and are always
    required (the query is time-bound). ``dimensions`` group the result — an empty
    list is one ungrouped total. ``granularity`` buckets the result by time
    (``hour`` / ``day`` / ``week``); ``None`` is no time bucketing. Every member is
    neutral: a backend maps each onto its own metrics API and declares the measures
    and dimensions it serves through :class:`MetricsCapability`. A measure or
    dimension the backend does not serve raises ``MonitoringReadNotSupportedError``
    rather than returning a silent zero.
    """

    measures: list[Measure]
    from_timestamp: datetime
    to_timestamp: datetime
    dimensions: list[Dimension] = Field(default_factory=list[Dimension])
    granularity: str | None = None


class MetricsRow(BaseModel):
    """One grouped result row: the group's dimension values and its aggregated measures.

    ``dimensions`` maps each requested :class:`Dimension` to that group's value (the
    model name; ``None`` when the group has no value for it). ``measures`` maps each
    requested :class:`Measure` to its aggregated value, ``None`` when the group has
    no data for that measure — never coerced to zero. ``bucket`` is the row's
    time-bucket label when the query set a granularity, ``None`` otherwise; the
    backend sets it explicitly so the reader never hunts a date out of the values.
    """

    dimensions: dict[Dimension, str | None] = Field(default_factory=dict[Dimension, "str | None"])
    measures: dict[Measure, float | None] = Field(default_factory=dict[Measure, "float | None"])
    bucket: str | None = None


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
    # The backend maps its own observation types onto the four neutral kinds.
    kind: SpanKind | None = None
    name: str | None = None
    level: str | None = None
    status_message: str | None = None
    input: Any = None
    output: Any = None
    # The producer's metadata, including the promoted keys (``tai42.step_role``, ``tai42.timing``).
    metadata: dict[str, Any] | None = None
    model: str | None = None
    # Token counts the producer reported for the observation; ``None`` when not reported.
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
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


class MonitoringTraceSummary(BaseModel):
    """One run-list row: the backend's list-surface attributes plus its batched aggregates, never a per-trace body.

    ``input`` / ``output`` are the trace's own input and output values as the
    backend's list surface returns them; a consumer that shows a bounded view
    bounds them itself. ``total_tokens`` is ``None`` when the backend
    returned no usage for the trace (never coerced to ``0``). ``status`` is
    ``error`` when the run carries an error observation, ``ok`` otherwise — a
    malformed backend row fails the page loudly rather than being kept as a
    partial row.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    timestamp: datetime | None = None
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    input: Any = None
    output: Any = None
    latency_ms: float | None = None
    total_cost: float | None = None
    total_tokens: int | None = None
    status: Literal["ok", "error"]


class MetricsResult(BaseModel):
    """The result of a metrics query: the dimensioned/bucketed groups as typed rows."""

    rows: list[MetricsRow] = Field(default_factory=list[MetricsRow])


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
    where a door legitimately lacks it. ``tags`` are the run kwargs the operator
    defines per run, carried verbatim with no platform interpretation;
    ``metadata`` are attribution key/values. There is NO tenant/client/domain
    field: attribution only, never a multi-tenant qualifier.

    ``user_id`` / ``session_id`` are the two backend-native identity DIMENSIONS a
    run is grouped and traced by (a conversation's person-or-address, and its
    resolved thread) — optional, since a door that lacks either legitimately omits
    it. They stay generic: the platform assigns no meaning beyond "the value a
    reader filters/groups on". A backend that has no native user/session notion
    maps them into ordinary attributes.
    """

    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    user_id: str | None = None
    session_id: str | None = None


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
    ``name`` / ``user_id`` / ``session_id`` / ``version`` / ``model`` filter on
    backend attributes that are not all present on the neutral result models.
    ``version`` matches the run's ROOT version DIMENSION — the value a run is
    stamped with (carried onto the backend's native version field), so a run
    stamped at "run R version V" is queryable by ``V`` even though the version does
    not live in ``metadata``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    version: str | None = None
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
