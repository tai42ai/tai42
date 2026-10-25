"""Filter clauses for the observations list and the metrics queries.

Every clause rides in the ``filter`` JSON, which overrides the native query params, so no native filter param is
sent. A run is a trace; its run-level clauses are columns of its root observation, its run-wide clauses need every
observation of the trace.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from tai42_contract.monitoring import MonitoringFilter, MonitoringLevel

Clause = dict[str, Any]

ROOT_CLAUSE: Clause = {"column": "isRootObservation", "operator": "=", "value": True, "type": "boolean"}


def environment_clause(source: str) -> Clause:
    """Scope a read to the active source, the Langfuse ``environment``."""
    return {"column": "environment", "operator": "any of", "value": [source], "type": "stringOptions"}


def string_clause(column: str, value: str) -> Clause:
    """One string equality clause."""
    return {"column": column, "operator": "=", "value": value, "type": "string"}


def trace_ids_clause(trace_ids: Sequence[str]) -> Clause:
    """The observations of the listed traces; the server refuses an empty list."""
    if not trace_ids:
        raise ValueError("a traceId clause needs at least one trace id")
    return {"column": "traceId", "operator": "any of", "value": list(trace_ids), "type": "stringOptions"}


def window_clauses(t0: datetime | None, t1: datetime | None) -> list[Clause]:
    """The half-open start-time window ``[t0, t1)``."""
    clauses: list[Clause] = []
    if t0 is not None:
        clauses.append({"column": "startTime", "operator": ">=", "value": t0.isoformat(), "type": "datetime"})
    if t1 is not None:
        clauses.append({"column": "startTime", "operator": "<", "value": t1.isoformat(), "type": "datetime"})
    return clauses


def level_value(level: MonitoringLevel) -> str:
    """The Langfuse level string of a neutral level."""
    return level.value


def _number_clauses(column: str, low: float | None, high: float | None) -> list[Clause]:
    clauses: list[Clause] = []
    if low is not None:
        clauses.append({"column": column, "operator": ">=", "value": low, "type": "number"})
    if high is not None:
        clauses.append({"column": column, "operator": "<=", "value": high, "type": "number"})
    return clauses


def _metadata_clauses(metadata: dict[str, str]) -> list[Clause]:
    return [
        {"column": "metadata", "type": "stringObject", "key": k, "operator": "=", "value": v}
        for k, v in sorted(metadata.items())
    ]


def _tags_clause(column: str, operator: str, tags: list[str]) -> Clause:
    return {"column": column, "operator": operator, "value": tags, "type": "arrayOptions"}


# The run-wide filter fields, by the name a too-broad answer gives them; each needs every observation of a run.
RUN_WIDE_FILTERS: dict[str, tuple[str, ...]] = {
    "cost": ("min_cost", "max_cost"),
    "tokens": ("min_tokens", "max_tokens"),
    "status": ("level",),
    "model": ("model",),
}


def run_wide_filters(filter_: MonitoringFilter | None) -> list[str]:
    """The names of the run-wide clauses ``filter_`` sets, in a stable order."""
    if filter_ is None:
        return []
    return [name for name, fields in RUN_WIDE_FILTERS.items() if any(getattr(filter_, f) is not None for f in fields)]


def has_root_only_clauses(filter_: MonitoringFilter | None) -> bool:
    """Whether ``filter_`` sets a run-level clause beyond the name and tags every observation carries."""
    if filter_ is None:
        return False
    return bool(
        filter_.user_id is not None
        or filter_.session_id is not None
        or filter_.version is not None
        or filter_.metadata
        or filter_.min_latency is not None
        or filter_.max_latency is not None
    )


def run_level_clauses(filter_: MonitoringFilter | None) -> list[Clause]:
    """The run-level clauses of ``filter_``: columns of a run's root, for the root walk."""
    if filter_ is None:
        return []
    clauses: list[Clause] = []
    if filter_.name is not None:
        clauses.append(string_clause("traceName", filter_.name))
    if filter_.user_id is not None:
        clauses.append(string_clause("userId", filter_.user_id))
    if filter_.session_id is not None:
        clauses.append(string_clause("sessionId", filter_.session_id))
    if filter_.version is not None:
        clauses.append(string_clause("version", filter_.version))
    if filter_.tags:
        clauses.append(_tags_clause("traceTags", "all of", filter_.tags))
    clauses += _metadata_clauses(filter_.metadata)
    clauses += _number_clauses("latency", filter_.min_latency, filter_.max_latency)
    return clauses


def ranked_metric_clauses(filter_: MonitoringFilter | None) -> list[Clause]:
    """The name and tags clauses of a ranked top-N metrics query; the metrics tags column is ``tags``."""
    if filter_ is None:
        return []
    clauses: list[Clause] = []
    if filter_.name is not None:
        clauses.append(string_clause("traceName", filter_.name))
    if filter_.tags:
        clauses.append(_tags_clause("tags", "all of", filter_.tags))
    return clauses


def span_clauses(filter_: MonitoringFilter | None) -> list[Clause]:
    """The observation-level clauses of a span-window read."""
    if filter_ is None:
        return []
    clauses: list[Clause] = []
    if filter_.name is not None:
        clauses.append(string_clause("name", filter_.name))
    if filter_.user_id is not None:
        clauses.append(string_clause("userId", filter_.user_id))
    if filter_.session_id is not None:
        clauses.append(string_clause("sessionId", filter_.session_id))
    if filter_.version is not None:
        clauses.append(string_clause("version", filter_.version))
    if filter_.level is not None:
        clauses.append(string_clause("level", level_value(filter_.level)))
    if filter_.model is not None:
        clauses.append(string_clause("model", filter_.model))
    if filter_.tags:
        clauses.append(_tags_clause("traceTags", "any of", filter_.tags))
    clauses += _number_clauses("totalCost", filter_.min_cost, filter_.max_cost)
    clauses += _number_clauses("totalTokens", filter_.min_tokens, filter_.max_tokens)
    clauses += _number_clauses("latency", filter_.min_latency, filter_.max_latency)
    clauses += _metadata_clauses(filter_.metadata)
    return clauses
