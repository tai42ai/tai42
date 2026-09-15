"""Advanced-filter clause builders mapping a neutral ``MonitoringFilter`` onto Langfuse filter arrays."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tai42_contract.monitoring import MonitoringFilter, MonitoringReadNotSupportedError


def _environment_clause(source: str) -> dict[str, Any]:
    return {"column": "environment", "operator": "=", "value": source, "type": "string"}


def _level_value(level: Any) -> str | None:
    if level is None:
        return None
    return level.value if hasattr(level, "value") else str(level)


def _number_clauses(column: str, low: float | None, high: float | None) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    if low is not None:
        clauses.append({"column": column, "operator": ">=", "value": low, "type": "number"})
    if high is not None:
        clauses.append({"column": column, "operator": "<=", "value": high, "type": "number"})
    return clauses


def _metadata_clauses(metadata: dict[str, str]) -> list[dict[str, Any]]:
    # One stringObject clause per key; ``key`` is required for nested metadata.
    return [
        {"column": "metadata", "type": "stringObject", "key": k, "operator": "=", "value": v}
        for k, v in sorted(metadata.items())
    ]


def _shared_metric_clauses(filter_: MonitoringFilter) -> list[dict[str, Any]]:
    """Advanced-filter clauses common to traces and observations (cost/token/latency ranges and metadata)."""
    clauses: list[dict[str, Any]] = []
    clauses += _number_clauses("totalCost", filter_.min_cost, filter_.max_cost)
    clauses += _number_clauses("totalTokens", filter_.min_tokens, filter_.max_tokens)
    clauses += _number_clauses("latency", filter_.min_latency, filter_.max_latency)
    clauses += _metadata_clauses(filter_.metadata)
    return clauses


def _observation_advanced_filter(filter_: MonitoringFilter | None) -> list[dict[str, Any]]:
    """Build the get_many advanced filter.

    ``name`` / ``user_id`` / ``level`` / ``environment`` ride native params; tags / model /
    cost / tokens / latency / metadata go here.
    """
    if filter_ is None:
        return []
    clauses: list[dict[str, Any]] = []
    if filter_.tags:
        clauses.append({"column": "traceTags", "operator": "any of", "value": filter_.tags, "type": "arrayOptions"})
    if filter_.model is not None:
        clauses.append({"column": "model", "operator": "=", "value": filter_.model, "type": "string"})
    clauses += _shared_metric_clauses(filter_)
    return clauses


def _timestamp_clauses(t0: datetime | None, t1: datetime | None) -> list[dict[str, Any]]:
    """Build the trace ``timestamp`` range as advanced-filter clauses.

    So the time bound survives when other advanced clauses force the filter JSON.
    """
    clauses: list[dict[str, Any]] = []
    if t0 is not None:
        clauses.append({"column": "timestamp", "operator": ">=", "value": t0.isoformat(), "type": "datetime"})
    if t1 is not None:
        # Half-open [t0, t1): exclusive upper, matching trace.list's native toTimestamp.
        clauses.append({"column": "timestamp", "operator": "<", "value": t1.isoformat(), "type": "datetime"})
    return clauses


def _trace_advanced_filter(filter_: MonitoringFilter | None) -> list[dict[str, Any]]:
    """Build the trace.list advanced filter.

    ``name`` / ``user_id`` / ``session_id`` / ``version`` / ``tags`` / ``environment`` ride
    native params; ``level`` plus the cost / token / latency / metadata clauses go here. Traces
    have no ``model`` column — a model filter on traces is unsupported.
    """
    if filter_ is None:
        return []
    if filter_.model is not None:
        raise MonitoringReadNotSupportedError(
            "list_traces cannot filter on 'model' (no model column on traces); filter spans instead"
        )
    clauses: list[dict[str, Any]] = []
    if filter_.level is not None:
        clauses.append({"column": "level", "operator": "=", "value": _level_value(filter_.level), "type": "string"})
    clauses += _shared_metric_clauses(filter_)
    return clauses


def _metrics_supported_clauses(filter_: MonitoringFilter) -> list[dict[str, Any]]:
    """Build the metrics ``traces`` view's supported filter clauses.

    ``name`` / ``userId`` / ``sessionId`` / ``tags`` (column ``tags``, NOT ``traceTags``) /
    ``metadata``.
    """
    clauses: list[dict[str, Any]] = []
    if filter_.name is not None:
        clauses.append({"column": "name", "operator": "=", "value": filter_.name, "type": "string"})
    if filter_.user_id is not None:
        clauses.append({"column": "userId", "operator": "=", "value": filter_.user_id, "type": "string"})
    if filter_.session_id is not None:
        clauses.append({"column": "sessionId", "operator": "=", "value": filter_.session_id, "type": "string"})
    if filter_.tags:
        clauses.append({"column": "tags", "operator": "any of", "value": filter_.tags, "type": "arrayOptions"})
    clauses += _metadata_clauses(filter_.metadata)
    return clauses


def _metrics_unsupported_clauses(filter_: MonitoringFilter) -> list[str]:
    """List the ``filter`` clause names the metrics ``traces`` view has no column for.

    ``level`` / ``model`` / ``version`` and the cost / token / latency ranges.
    ``version`` is a native trace.list column (used on the timestamp-sort path) but
    the metrics ``traces`` view exposes no version dimension, so a metric SORT
    combined with a version filter raises here rather than misranking a page on a
    silently-dropped clause.
    """
    unsupported: list[str] = []
    if filter_.level is not None:
        unsupported.append("level")
    if filter_.model is not None:
        unsupported.append("model")
    if filter_.version is not None:
        unsupported.append("version")
    for name, value in (
        ("min_cost", filter_.min_cost),
        ("max_cost", filter_.max_cost),
        ("min_tokens", filter_.min_tokens),
        ("max_tokens", filter_.max_tokens),
        ("min_latency", filter_.min_latency),
        ("max_latency", filter_.max_latency),
    ):
        if value is not None:
            unsupported.append(name)
    return unsupported


def _trace_metric_filter(filter_: MonitoringFilter | None) -> list[dict[str, Any]]:
    """Build the metric-SORT rank filter array.

    The metrics ``traces`` view accepts only ``name`` / ``userId`` / ``sessionId`` / ``tags``
    (column ``tags``, NOT ``traceTags``) / ``metadata``; ``level`` / ``model`` / cost / token /
    latency are unsupported and RAISE here — on the sort path the clause decides the rank, so a
    dropped clause would misrank the page.
    """
    if filter_ is None:
        return []
    unsupported = _metrics_unsupported_clauses(filter_)
    if unsupported:
        raise MonitoringReadNotSupportedError(
            f"metric sort cannot filter on {unsupported}; the metrics traces view "
            "has no such column — sort by timestamp to use these filters"
        )
    return _metrics_supported_clauses(filter_)


def _metric_population_filter(filter_: MonitoringFilter | None) -> list[dict[str, Any]]:
    """Build the token-join filter array.

    It narrows the metrics POPULATION only — per-trace correctness comes from joining the result
    by trace id, not from this filter — so the clauses the metrics ``traces`` view has no column
    for (``level`` / ``model`` and the cost / token / latency ranges) are DROPPED here rather
    than raised, unlike the metric-SORT path. Keeps the view's supported clauses: ``name`` /
    ``userId`` / ``sessionId`` / ``tags`` / ``metadata``.
    """
    if filter_ is None:
        return []
    return _metrics_supported_clauses(filter_)
