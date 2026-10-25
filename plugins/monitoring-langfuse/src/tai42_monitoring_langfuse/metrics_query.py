"""The ``query_metrics`` read on Langfuse Metrics v2, the observations view.

An ungrouped query counts runs and averages their latency over the roots, and sums cost and tokens over every
observation, then merges the two on the time bucket; a run counts in the bucket its root started in. A model
grouping reads one query over every observation, since the model is a per-observation column.
"""

from __future__ import annotations

from typing import Any

import httpx
from tai42_contract.monitoring import (
    Dimension,
    Measure,
    MetricsCapability,
    MetricsQuery,
    MetricsResult,
    MetricsRow,
    MonitoringError,
    MonitoringReadNotServedError,
)

from tai42_monitoring_langfuse.filters import ROOT_CLAUSE, Clause, environment_clause
from tai42_monitoring_langfuse.measures import _measure_value
from tai42_monitoring_langfuse.observation_query import ObservationQuery

# Each neutral measure -> (Langfuse measure, aggregation). Langfuse metrics latency is in milliseconds.
_MEASURE_TO_LANGFUSE: dict[Measure, tuple[str, str]] = {
    Measure.COUNT: ("count", "count"),
    Measure.COST: ("totalCost", "sum"),
    Measure.TOKENS: ("totalTokens", "sum"),
    Measure.LATENCY: ("latency", "avg"),
}
# The measures an ungrouped query reads over the roots, one per run.
_RUN_MEASURES = frozenset({Measure.COUNT, Measure.LATENCY})
_DIMENSION_TO_LANGFUSE: dict[Dimension, str] = {Dimension.MODEL: "providedModelName"}
_BUCKET_COLUMN = "time_dimension"


def _column(measure: Measure) -> str:
    name, aggregation = _MEASURE_TO_LANGFUSE[measure]
    return f"{aggregation}_{name}"


class MetricsQueryRunner(ObservationQuery):
    """Serves ``query_metrics`` from Langfuse Metrics v2."""

    def metrics_capability(self) -> MetricsCapability:
        """The neutral measures and dimensions this backend aggregates and groups by."""
        return MetricsCapability(measures=frozenset(_MEASURE_TO_LANGFUSE), dimensions=frozenset(_DIMENSION_TO_LANGFUSE))

    async def query_metrics(self, query: MetricsQuery) -> MetricsResult:
        """Run ``query`` scoped to the active source; an unserved measure or dimension raises before any request."""
        self._check_served(query)
        client = await self._active_client()
        base = [environment_clause(self._m.active_source())]
        if query.dimensions:
            rows = await self._run(client, query, query.measures, base)
            return MetricsResult(rows=[self._to_row(raw, query, query.measures) for raw in rows])
        run_measures = [m for m in query.measures if m in _RUN_MEASURES]
        observation_measures = [m for m in query.measures if m not in _RUN_MEASURES]
        merged: dict[str | None, MetricsRow] = {}
        for measures, clauses in ((run_measures, [*base, ROOT_CLAUSE]), (observation_measures, base)):
            if not measures:
                continue
            for raw in await self._run(client, query, measures, clauses):
                row = self._to_row(raw, query, measures)
                kept = merged.setdefault(row.bucket, MetricsRow(bucket=row.bucket))
                kept.measures.update(row.measures)
        for row in merged.values():
            for measure in query.measures:
                row.measures.setdefault(measure, None)
        return MetricsResult(rows=sorted(merged.values(), key=lambda row: row.bucket or ""))

    async def _run(
        self, client: httpx.Client, query: MetricsQuery, measures: list[Measure], clauses: list[Clause]
    ) -> list[dict[str, Any]]:
        langfuse_query: dict[str, Any] = {
            "view": "observations",
            "metrics": [
                {"measure": _MEASURE_TO_LANGFUSE[m][0], "aggregation": _MEASURE_TO_LANGFUSE[m][1]} for m in measures
            ],
            "dimensions": [{"field": _DIMENSION_TO_LANGFUSE[d]} for d in query.dimensions],
            "filters": clauses,
            "fromTimestamp": query.from_timestamp.isoformat(),
            "toTimestamp": query.to_timestamp.isoformat(),
        }
        if query.granularity:
            langfuse_query["timeDimension"] = {"granularity": query.granularity}
        return await self._metrics(client, langfuse_query)

    @staticmethod
    def _check_served(query: MetricsQuery) -> None:
        unserved_measures = [m for m in query.measures if m not in _MEASURE_TO_LANGFUSE]
        unserved_dimensions = [d for d in query.dimensions if d not in _DIMENSION_TO_LANGFUSE]
        if unserved_measures or unserved_dimensions:
            raise MonitoringReadNotServedError(
                "Langfuse metrics query cannot serve "
                f"measures={[m.value for m in unserved_measures]} "
                f"dimensions={[d.value for d in unserved_dimensions]}"
            )

    @staticmethod
    def _to_row(raw: dict[str, Any], query: MetricsQuery, measures: list[Measure]) -> MetricsRow:
        """One Langfuse result row as a :class:`MetricsRow`; a requested column missing from it raises."""
        missing = [_column(m) for m in measures if _column(m) not in raw]
        missing += [_DIMENSION_TO_LANGFUSE[d] for d in query.dimensions if _DIMENSION_TO_LANGFUSE[d] not in raw]
        if query.granularity and _BUCKET_COLUMN not in raw:
            missing.append(_BUCKET_COLUMN)
        if missing:
            raise MonitoringError(f"Langfuse metrics response row carries no {missing} column: {raw!r}")
        dimensions: dict[Dimension, str | None] = {}
        for dimension in query.dimensions:
            value = raw[_DIMENSION_TO_LANGFUSE[dimension]]
            dimensions[dimension] = None if value is None else str(value)
        bucket = raw[_BUCKET_COLUMN] if query.granularity else None
        return MetricsRow(
            dimensions=dimensions,
            measures={m: _measure_value(raw[_column(m)]) for m in measures},
            bucket=None if bucket is None else str(bucket),
        )
