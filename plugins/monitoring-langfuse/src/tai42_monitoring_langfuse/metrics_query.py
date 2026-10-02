"""The Metrics API query surface: ``query_metrics`` -> aggregated metric rows.

Maps the neutral contract vocabulary (:class:`Measure` / :class:`Dimension`) onto
the Langfuse Metrics API: each neutral measure to a Langfuse measure + aggregation,
the model dimension to Langfuse's ``providedModelName`` (a per-generation attribute,
so a model grouping aggregates over the observations view), and the time bucket back
off the response row. The backend declares what it serves through
:meth:`metrics_capability`; a query for anything outside that raises
``MonitoringReadNotSupportedError`` rather than returning a silent zero.
"""

from __future__ import annotations

import asyncio
import json
import re
from functools import partial
from typing import Any

from tai42_contract.monitoring import (
    Dimension,
    Measure,
    MetricsCapability,
    MetricsQuery,
    MetricsResult,
    MetricsRow,
    MonitoringReadNotSupportedError,
)

from tai42_monitoring_langfuse.filters import _environment_clause
from tai42_monitoring_langfuse.measures import _measure_key, _measure_value
from tai42_monitoring_langfuse.query_base import _LangfuseQuery

# Each neutral measure -> (Langfuse measure name, natural aggregation).
_MEASURE_TO_LANGFUSE: dict[Measure, tuple[str, str]] = {
    Measure.COUNT: ("count", "count"),
    Measure.COST: ("totalCost", "sum"),
    Measure.TOKENS: ("totalTokens", "sum"),
    Measure.LATENCY: ("latency", "avg"),
}

# Each neutral dimension -> the Langfuse group-by field. The model attribute lives on
# generations, so grouping by it reads the observations view.
_DIMENSION_TO_LANGFUSE: dict[Dimension, str] = {
    Dimension.MODEL: "providedModelName",
}

# A row's time-bucket column is not a stable key on the Langfuse response, so the
# bucket is recovered as the row's first ISO-date-like value (the plugin owns this
# Langfuse quirk — the platform never hunts a date).
_ISO_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}")


class MetricsQueryRunner(_LangfuseQuery):
    """Serves ``query_metrics`` from the Langfuse Metrics API."""

    def metrics_capability(self) -> MetricsCapability:
        """The neutral measures and dimensions this backend can aggregate / group by."""
        return MetricsCapability(
            measures=frozenset(_MEASURE_TO_LANGFUSE),
            dimensions=frozenset(_DIMENSION_TO_LANGFUSE),
        )

    async def query_metrics(self, query: MetricsQuery) -> MetricsResult:
        """Run ``query`` against the Langfuse Metrics API, scoped to this source's environment marker.

        A measure or dimension the backend does not serve raises
        ``MonitoringReadNotSupportedError`` before any request — never a silent zero.
        """
        self._check_served(query)
        client = await self._active_client()
        source = self._m.active_source()

        # The model attribute lives on generations, so a model grouping aggregates
        # over the observations view; everything else over whole runs (traces).
        view = "observations" if Dimension.MODEL in query.dimensions else "traces"
        langfuse_query: dict[str, Any] = {
            "view": view,
            "metrics": [self._metric_entry(m) for m in query.measures],
            "dimensions": [{"field": _DIMENSION_TO_LANGFUSE[d]} for d in query.dimensions],
            # Scope every metrics query to our data via the environment marker.
            "filters": [_environment_clause(source)],
            "fromTimestamp": query.from_timestamp.isoformat(),
            "toTimestamp": query.to_timestamp.isoformat(),
        }
        if query.granularity:
            langfuse_query["timeDimension"] = {"granularity": query.granularity}

        response = await asyncio.to_thread(
            partial(
                client.api.legacy.metrics_v1.metrics,
                query=json.dumps(langfuse_query),
                request_options=self._request_options(),
            )
        )
        raw_rows: list[dict[str, Any]] = list(getattr(response, "data", []) or [])
        rows = [self._to_row(raw, query) for raw in raw_rows]
        return MetricsResult(rows=rows)

    @staticmethod
    def _check_served(query: MetricsQuery) -> None:
        unsupported_measures = [m for m in query.measures if m not in _MEASURE_TO_LANGFUSE]
        unsupported_dimensions = [d for d in query.dimensions if d not in _DIMENSION_TO_LANGFUSE]
        if unsupported_measures or unsupported_dimensions:
            raise MonitoringReadNotSupportedError(
                "Langfuse metrics query cannot serve "
                f"measures={[m.value for m in unsupported_measures]} "
                f"dimensions={[d.value for d in unsupported_dimensions]}"
            )

    @staticmethod
    def _metric_entry(measure: Measure) -> dict[str, Any]:
        langfuse_measure, aggregation = _MEASURE_TO_LANGFUSE[measure]
        return {"measure": langfuse_measure, "aggregation": aggregation}

    @classmethod
    def _to_row(cls, raw: dict[str, Any], query: MetricsQuery) -> MetricsRow:
        """Map one Langfuse result row onto the typed :class:`MetricsRow`.

        A requested measure whose output column is absent from the row is a response
        the backend cannot honour — it raises rather than reporting a silent zero.
        """
        measures: dict[Measure, float | None] = {}
        for measure in query.measures:
            langfuse_measure = _MEASURE_TO_LANGFUSE[measure][0]
            key = _measure_key(raw, langfuse_measure)
            if key is None:
                raise MonitoringReadNotSupportedError(
                    f"Langfuse metrics response carries no column for measure {measure.value!r}"
                )
            measures[measure] = _measure_value(raw[key])

        dimensions: dict[Dimension, str | None] = {}
        for dimension in query.dimensions:
            value = raw.get(_DIMENSION_TO_LANGFUSE[dimension])
            dimensions[dimension] = str(value) if value is not None else None

        bucket = cls._extract_bucket(raw) if query.granularity else None
        return MetricsRow(dimensions=dimensions, measures=measures, bucket=bucket)

    @staticmethod
    def _extract_bucket(raw: dict[str, Any]) -> str | None:
        """The row's time-bucket label: its first ISO-date-like value, or ``None``."""
        for value in raw.values():
            if isinstance(value, str) and _ISO_LIKE.match(value):
                return value
        return None
