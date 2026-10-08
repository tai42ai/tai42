"""Pure helpers for the observability router.

Kept out of ``observability.py`` so the router holds only route registration, reader glue, and handlers.

Two groups, neither of which owns a reader instance:

* **Request → params** — decode the query string into the contract's neutral
  types (``parse_time_range``, ``parse_run_filter``, ``parse_paging``,
  ``select_granularity``). These take a Starlette ``Request`` but do no I/O and
  raise ``RequestParseError`` (→ 400) on malformed input.
* **Output transforms** — pure functions over plain values / contract models:
  the metrics summary + series + by-model row readers (which read neutral measures
  off the typed :class:`MetricsRow`, never hunting a column or a date), the trace →
  run / trace / outline mapping (the run row bounds its input/output with the kit's
  ``preview``), the backend declarations in wire names, and the CSV-injection guard.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args

from tai42_contract.monitoring import (
    Dimension,
    ListCapability,
    Measure,
    MetricsCapability,
    MetricsResult,
    MetricsRow,
    MonitoringFilter,
    MonitoringLevel,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
)
from tai42_kit.monitoring import preview

if TYPE_CHECKING:
    from starlette.requests import Request
    from tai42_contract.monitoring import MonitoringObservation


class RequestParseError(Exception):
    """A query parameter is missing or malformed — the handler maps it to a loud 400.

    Distinct from a reader/backend failure, which propagates as a 500 (or the two typed monitoring errors,
    which map to 501/404).
    """


# ---------------------------------------------------------------------------
# Request → params parsing (time range, advanced run filter, paging, granularity)
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)([hdw])$")
_RELATIVE_UNIT = {"h": "hours", "d": "days", "w": "weeks"}
_DEFAULT_FROM = "30d"

# The closed value sets the query parsers accept, each a ``Literal`` type so the doors' query
# models publish the same vocabulary the checks enforce: a value outside a set is a 400 and is
# absent from the emitted OpenAPI parameter's enum. Each binds its members as a tuple beside
# the type except ``RunSortKey``, whose members are the ``_SORT_FIELDS`` keys its check reads;
# and every check is made here except the export format's, which the export door in
# ``observability.py`` makes.

#: The bucket sizes the metrics door serves; anything else is a 400.
MetricsGranularity = Literal["hour", "day", "week"]
GRANULARITIES: tuple[MetricsGranularity, ...] = get_args(MetricsGranularity)

#: The run statuses the run filter serves: ``error`` selects error runs, ``success`` is the
#: unfiltered listing (the absence of errors is not a trace-level clause).
RunStatus = Literal["error", "success"]
RUN_STATUSES: tuple[RunStatus, ...] = get_args(RunStatus)

#: The run-list sort keys the door serves — the ``_SORT_FIELDS`` vocabulary as a type.
RunSortKey = Literal["createdAt", "cost", "latencyMs", "totalTokens"]

#: The sort directions the run filter serves; ``desc`` is the default.
SortDirection = Literal["asc", "desc"]
SORT_DIRECTIONS: tuple[SortDirection, ...] = get_args(SortDirection)

#: The download formats the runs export serves; ``csv`` is the default.
ExportFormat = Literal["csv", "json"]
EXPORT_FORMATS: tuple[ExportFormat, ...] = get_args(ExportFormat)

# Run-list sort key (frontend token) → neutral ``OrderBy.field`` on list_traces.
# ``timestamp`` sorts natively; ``total_cost`` / ``latency`` / ``total_tokens``
# are metric-ranked globally by the contract.
_SORT_FIELDS: dict[RunSortKey, str] = {
    "createdAt": "timestamp",
    "cost": "total_cost",
    "latencyMs": "latency",
    "totalTokens": "total_tokens",
}

# Run-list filter query param → the ``MonitoringFilter`` field it sets. ``status``
# sets ``level`` (``error`` only); the ``*LatencyMs`` params are milliseconds on the
# wire and seconds in the contract. ``meta.<key>`` params are dynamic and not listed.
_FILTER_PARAMS: dict[str, str] = {
    "status": "level",
    "user": "user_id",
    "session": "session_id",
    "version": "version",
    "tags": "tags",
    "minLatencyMs": "min_latency",
    "maxLatencyMs": "max_latency",
    "minCost": "min_cost",
    "maxCost": "max_cost",
    "minTokens": "min_tokens",
    "maxTokens": "max_tokens",
}


def one_of(values: tuple[str, ...]) -> str:
    """A closed value set as refusal prose — ``'a' or 'b'``, ``'a', 'b' or 'c'``.

    So the message names the same tuple the check reads.
    """
    quoted = [repr(value) for value in values]
    head, last = quoted[:-1], quoted[-1]
    return f"{', '.join(head)} or {last}" if head else last


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_instant(value: str, *, now: datetime, field: str) -> datetime:
    """Resolve an absolute ISO instant or a relative token (e.g. ``7d`` = 7 days ago), normalized to UTC."""
    match = _RELATIVE_RE.match(value)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        return now - timedelta(**{_RELATIVE_UNIT[unit]: amount})
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RequestParseError(f"Invalid {field}: {value}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_time_range(request: Request) -> tuple[datetime, datetime]:
    r"""The ``from`` / ``to`` window: each is an ISO instant or a relative token (``\\d+[hdw]``).

    ``from`` defaults to ``30d`` ago, ``to`` to now. ``from`` at or after ``to`` is a 400.
    """
    q = request.query_params
    now = _now_utc()
    t0 = _parse_instant(q.get("from") or _DEFAULT_FROM, now=now, field="from")
    t1 = _parse_instant(q["to"], now=now, field="to") if q.get("to") else now
    if t0 >= t1:
        raise RequestParseError("`from` must be before `to`")
    return t0, t1


def select_granularity(t0: datetime, t1: datetime, override: str | None) -> str:
    """≤2 days → hourly, ≤90 days → daily, else weekly — unless pinned to one of hour/day/week.

    An explicit granularity outside that set is malformed and raises ``RequestParseError`` (→ 400, same as
    a bad from/to); auto-selection applies only when it is absent.
    """
    if override:
        if override not in GRANULARITIES:
            raise RequestParseError(f"granularity must be one of {', '.join(GRANULARITIES)}: {override!r}")
        return override
    span = (t1 - t0).total_seconds()
    if span <= 2 * 86400:
        return "hour"
    if span <= 90 * 86400:
        return "day"
    return "week"


def _q_float(raw: str | None, key: str) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise RequestParseError(f"{key} must be a number") from exc


def _wire_param(field: str) -> str:
    """The run-list query param that sets ``MonitoringFilter`` field ``field``; ``KeyError`` when none does."""
    return next(param for param, target in _FILTER_PARAMS.items() if target == field)


def _parse_tags(raw: str | None) -> list[str]:
    """Tags as either a JSON list (``["a","b"]``) or a comma-separated string.

    A value opening with ``[`` is decoded as JSON (the list/dict query-param
    encoding); anything else is split on commas. Empty entries are dropped.
    """
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except ValueError as exc:
            raise RequestParseError("tags must be a JSON list or comma-separated") from exc
        if not isinstance(decoded, list) or not all(isinstance(t, str) for t in decoded):
            raise RequestParseError("tags JSON must be a list of strings")
        return [t.strip() for t in decoded if t.strip()]
    return [t.strip() for t in text.split(",") if t.strip()]


def _parse_meta(q: Any) -> dict[str, str]:
    """Collect the ``meta.<key>=<value>`` query params into the neutral ``MonitoringFilter.metadata`` map.

    Each ``meta.<key>`` param is one string-equality clause on the trace's
    ``metadata`` (the reader maps it to the backend's per-key metadata filter). A
    bare ``meta.`` prefix with no key, or an empty value, is malformed (→ 400) — a
    keyless or valueless equality would match nothing meaningfully.
    """
    meta: dict[str, str] = {}
    for raw_key in q:
        if not raw_key.startswith("meta."):
            continue
        key = raw_key[len("meta.") :]
        if not key:
            raise RequestParseError("meta.<key> requires a non-empty key")
        value = q.get(raw_key)
        if value is None or value == "":
            raise RequestParseError(f"meta.{key} requires a non-empty value")
        meta[key] = value
    return meta


def parse_run_filter(request: Request) -> tuple[MonitoringFilter | None, OrderBy | None]:
    """Build the neutral ``MonitoringFilter`` + ``OrderBy`` for the run list from the query string.

    Every clause is optional; an all-empty query yields ``(None, None)`` (a plain newest-first listing).
    List- or dict-typed params are JSON-encoded in the query string (``tags`` may
    be a JSON list or a comma-separated string). ``status=error`` maps to a
    level==ERROR clause; ``status=success`` is unfiltered (the absence of errors
    is not a trace-level clause). ``user`` / ``session`` / ``version`` filter on the
    backend's native user/session/version identity dimensions (``version`` matches a
    run's stamped ROOT version — e.g. a preset's ``preset-v`` version); each
    ``meta.<key>=<value>`` adds one metadata string-equality clause. The
    ``minCost`` / ``maxCost`` / ``minTokens`` /
    ``maxTokens`` / ``minLatencyMs`` / ``maxLatencyMs`` ranges are inclusive;
    latency is exposed in ms and converted to the contract's seconds. An inverted
    range is rejected by the contract (→ 400). Sort: ``sort`` ∈ {createdAt, cost,
    latencyMs, totalTokens} with ``dir`` ∈ {asc, desc} (default desc).
    """
    q = request.query_params
    raw = {field: q.get(param) for param, field in _FILTER_PARAMS.items()}

    status = raw["level"]
    if status not in (None, "", *RUN_STATUSES):
        raise RequestParseError(f"status must be {one_of(RUN_STATUSES)}")
    values: dict[str, Any] = {
        "tags": _parse_tags(raw["tags"]),
        "level": MonitoringLevel.ERROR if status == "error" else None,
        "user_id": raw["user_id"] or None,
        "session_id": raw["session_id"] or None,
        "version": raw["version"] or None,
        "metadata": _parse_meta(q),
    }
    for field in ("min_cost", "max_cost"):
        values[field] = _q_float(raw[field], _wire_param(field))
    for field in ("min_tokens", "max_tokens"):
        tokens = _q_float(raw[field], _wire_param(field))
        values[field] = int(tokens) if tokens is not None else None
    for field in ("min_latency", "max_latency"):
        latency_ms = _q_float(raw[field], _wire_param(field))
        values[field] = latency_ms / 1000 if latency_ms is not None else None

    filter_: MonitoringFilter | None = None
    if any(v not in (None, [], {}) for v in values.values()):
        try:
            filter_ = MonitoringFilter(**values)
        except ValueError as exc:  # inverted range → contract rejects at construction
            raise RequestParseError(str(exc)) from exc

    order_by: OrderBy | None = None
    sort = q.get("sort")
    if sort:
        if sort not in _SORT_FIELDS:
            raise RequestParseError(f"sort must be one of {sorted(_SORT_FIELDS)}")
        direction = q.get("dir", "desc")
        if direction not in SORT_DIRECTIONS:
            raise RequestParseError(f"dir must be {one_of(SORT_DIRECTIONS)}")
        order_by = OrderBy(field=_SORT_FIELDS[sort], direction=direction)

    return filter_, order_by


def parse_paging(request: Request) -> tuple[int, int]:
    """``page`` (default 1) and ``pageSize`` (default 50), validated.

    A ``page`` or ``pageSize`` below 1 is malformed and raises ``RequestParseError`` (→ 400), consistent
    with the from/to and granularity checks — never silently clamped. A non-integer value is a 400. The
    run-list operation caps ``pageSize`` to the monitoring backend's declared maximum.
    """
    q = request.query_params
    try:
        page = int(q.get("page", "1"))
        page_size = int(q.get("pageSize", "50"))
    except ValueError as exc:
        raise RequestParseError("page and pageSize must be integers") from exc
    if page < 1 or page_size < 1:
        raise RequestParseError("page and pageSize must be >= 1")
    return page, page_size


def capabilities_view(
    list_capability: ListCapability, metrics: MetricsCapability, page_size_max: int
) -> dict[str, Any]:
    """The backend's declarations in the run list's wire names.

    A contract sort field or filter field with no run-list wire form is not served.
    """
    served = [key for key, field in _SORT_FIELDS.items() if field in list_capability.sort_fields]
    sort_key = {field: key for key, field in _SORT_FIELDS.items()}
    filter_param = {field: param for param, field in _FILTER_PARAMS.items()}
    incompatible: dict[str, list[str]] = {}
    for field, filters in list_capability.incompatible_filters.items():
        key = sort_key.get(field)
        params = sorted(filter_param[f] for f in filters if f in filter_param)
        if key in served and params:
            incompatible[key] = params
    return {
        "pageSizeMax": page_size_max,
        "sortKeys": served,
        "incompatibleFilters": incompatible,
        "metrics": {
            "measures": sorted(m.value for m in metrics.measures),
            "dimensions": sorted(d.value for d in metrics.dimensions),
        },
    }


# ---------------------------------------------------------------------------
# CSV-injection guard
# ---------------------------------------------------------------------------

# Leading characters a spreadsheet treats as a formula. User/LLM-controlled cell
# text starting with one is prefixed with ``'`` so Excel/Sheets render it as
# text (CSV-injection guard).
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> Any:
    """Prefix a formula-leading string cell with ``'`` so a spreadsheet renders it as text."""
    if isinstance(value, str) and value[:1] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


# ---------------------------------------------------------------------------
# Metrics-row readers (Dashboard tab)
# ---------------------------------------------------------------------------


def _measure_float(row: MetricsRow, measure: Measure) -> float:
    """Read a measure's value off a typed row as a float.

    The backend maps every requested measure onto the row, so the key is present; a
    missing key is a backend-contract violation and raises loudly (never a silent
    zero). A ``None`` value is a genuine "no data" for a served measure and reads as
    ``0.0`` for the tile.
    """
    value = row.measures[measure]
    return float(value) if value is not None else 0.0


def _measure_int(row: MetricsRow, measure: Measure) -> int:
    """Read a measure's value off a typed row as an int (see :func:`_measure_float`)."""
    return int(_measure_float(row, measure))


def summary_from_rows(rows: list[MetricsRow]) -> dict[str, Any]:
    """The Dashboard summary tile derived from the first (ungrouped) metrics row.

    Empty rows → all zeros. ``avgCostPerRun`` / ``avgTokensPerRun`` are derived
    from the totals; ``timeToFirstTokenMs`` is always ``None`` (the dashboard
    requests no measure that feeds it — the field is kept for a stable response
    shape).
    """
    if not rows:
        return {
            "totalRuns": 0,
            "totalCost": 0.0,
            "totalTokens": 0,
            "averageLatencyMs": 0,
            "avgCostPerRun": 0.0,
            "avgTokensPerRun": 0,
            "timeToFirstTokenMs": None,
        }
    row = rows[0]
    total_runs = _measure_int(row, Measure.COUNT)
    total_cost = _measure_float(row, Measure.COST)
    total_tokens = _measure_int(row, Measure.TOKENS)
    return {
        "totalRuns": total_runs,
        "totalCost": total_cost,
        "totalTokens": total_tokens,
        "averageLatencyMs": _measure_int(row, Measure.LATENCY),
        "avgCostPerRun": (total_cost / total_runs) if total_runs else 0.0,
        "avgTokensPerRun": int(total_tokens / total_runs) if total_runs else 0,
        "timeToFirstTokenMs": None,
    }


def time_series_from_rows(rows: list[MetricsRow]) -> list[dict[str, Any]]:
    """Per-bucket series points for the Dashboard chart, one per granularity row.

    ``bucket`` is the backend's time-bucket label off the typed row (never hunted
    out of the values).
    """
    return [
        {
            "bucket": row.bucket,
            "runs": _measure_int(row, Measure.COUNT),
            "cost": _measure_float(row, Measure.COST),
            "avgLatencyMs": _measure_int(row, Measure.LATENCY),
            "totalTokens": _measure_int(row, Measure.TOKENS),
        }
        for row in rows
    ]


def map_model_rows(res: MetricsResult | None) -> list[dict[str, Any]]:
    """Per-model breakdown rows, top 8 by cost.

    Empty when the by-model panel is unavailable — the backend does not declare the
    model dimension (see ``get_metrics``), so no query was issued.
    """
    if res is None:
        return []
    rows = [
        {
            "model": row.dimensions.get(Dimension.MODEL) or "unknown",
            "calls": _measure_int(row, Measure.COUNT),
            "cost": _measure_float(row, Measure.COST),
            "totalTokens": _measure_int(row, Measure.TOKENS),
            "avgLatencyMs": _measure_int(row, Measure.LATENCY),
        }
        for row in res.rows
    ]
    rows.sort(key=lambda r: r["cost"], reverse=True)
    return rows[:8]


# ---------------------------------------------------------------------------
# Trace → run / trace mapping
# ---------------------------------------------------------------------------


def derive_run(row: MonitoringTraceSummary) -> dict[str, Any]:
    """A run-list row projected from a trace SUMMARY.

    Every list field is first-class on the summary: the input/output previews are
    the summary's raw values bounded by the kit's ``preview``, latency / tokens /
    status come from the backend's batched aggregates. ``status`` maps ``error`` -> ``error`` and ``ok`` ->
    ``success``; a malformed backend row never reaches here — it fails the page
    loudly at the reader.
    """
    return {
        "id": row.id,
        "traceId": row.id,
        "createdAt": row.timestamp.isoformat() if row.timestamp else None,
        "tags": list(row.tags or []),
        "status": "error" if row.status == "error" else "success",
        "cost": row.total_cost,
        "latencyMs": int(row.latency_ms) if row.latency_ms is not None else None,
        "totalTokens": row.total_tokens,
        "inputPreview": preview(row.input),
        "outputPreview": preview(row.output),
    }


def _map_span_outline(o: MonitoringObservation) -> dict[str, Any]:
    # ``metadata`` passes through WHOLE and opaque — the platform reads no consumer
    # key out of it.
    return {
        "id": o.id,
        "parentId": o.parent_id,
        "traceId": o.trace_id,
        "name": o.name,
        "kind": o.kind.value if o.kind is not None else None,
        "level": o.level,
        "statusMessage": o.status_message,
        "start": o.start.isoformat() if o.start else None,
        "end": o.end.isoformat() if o.end else None,
        "model": o.model,
        "inputTokens": o.input_tokens,
        "outputTokens": o.output_tokens,
        "totalTokens": o.total_tokens,
        "metadata": o.metadata,
    }


def _map_span(o: MonitoringObservation) -> dict[str, Any]:
    return {**_map_span_outline(o), "input": o.input, "output": o.output}


def map_trace(trace: MonitoringTrace) -> dict[str, Any]:
    """The single-run detail view: trace attributes plus every span."""
    return {
        "traceId": trace.id,
        "timestamp": trace.timestamp.isoformat() if trace.timestamp else None,
        "tags": list(trace.tags or []),
        "totalCost": trace.total_cost,
        "input": trace.input,
        "output": trace.output,
        "metadata": trace.metadata,
        "spans": [_map_span(o) for o in (trace.observations or [])],
    }


def map_trace_outline(trace: MonitoringTrace) -> dict[str, Any]:
    """The span tree of one run: every span without its input and output."""
    return {"traceId": trace.id, "spans": [_map_span_outline(o) for o in (trace.observations or [])]}
