"""The Metrics API query surface: ``query_metrics`` -> aggregated metric rows."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any

from tai42_contract.monitoring import MetricsFilter, MetricsResult, MetricsRow

from tai42_monitoring_langfuse.filters import _environment_clause
from tai42_monitoring_langfuse.query_base import _LangfuseQuery

# Natural aggregation per measure when the caller names a measure without one.
_DEFAULT_AGGREGATION: dict[str, str] = {
    "count": "count",
    "totalCost": "sum",
    "totalTokens": "sum",
    "latency": "avg",
    "timeToFirstToken": "avg",
}


class MetricsQuery(_LangfuseQuery):
    """Serves ``query_metrics`` from the Langfuse Metrics API."""

    async def query_metrics(self, filter: MetricsFilter) -> MetricsResult:
        client = await self._active_client()
        source = self._m.active_source()

        dimension_fields = list(filter.dimensions)
        query: dict[str, Any] = {
            "view": filter.view.value if hasattr(filter.view, "value") else filter.view,
            "metrics": [self._metric_entry(m) for m in filter.metrics],
            "dimensions": [{"field": d} for d in dimension_fields],
            # Scope every metrics query to our data via the environment marker.
            "filters": [*filter.filters, _environment_clause(source)],
            "fromTimestamp": filter.from_timestamp.isoformat(),
            "toTimestamp": filter.to_timestamp.isoformat(),
        }
        if filter.granularity:
            query["timeDimension"] = {"granularity": filter.granularity}
        if filter.order_by:
            query["orderBy"] = filter.order_by

        response = await asyncio.to_thread(
            partial(
                client.api.legacy.metrics_v1.metrics,
                query=json.dumps(query),
                request_options=self._request_options(),
            )
        )
        raw_rows: list[dict[str, Any]] = list(getattr(response, "data", []) or [])

        rows: list[MetricsRow] = []
        for raw in raw_rows:
            dims = {field: raw.get(field) for field in dimension_fields}
            metrics = {k: v for k, v in raw.items() if k not in dims}
            rows.append(MetricsRow(dimensions=dims, metrics=metrics))

        derived: dict[str, Any] = {}
        # Distinct-tag count is a cardinality, not a native metric: it is the
        # number of returned tag-groups when grouping on the tags dimension.
        if "tags" in dimension_fields:
            derived["distinct_tag_count"] = len(rows)

        return MetricsResult(rows=rows, derived=derived)

    @staticmethod
    def _metric_entry(measure: str) -> dict[str, Any]:
        return {"measure": measure, "aggregation": _DEFAULT_AGGREGATION.get(measure, "sum")}
