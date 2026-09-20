"""MetricsQuery: query building, row parsing, and source scoping for query_metrics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from tai42_contract.monitoring import MetricsFilter, MetricsView

from tai42_monitoring_langfuse.reader import LangfuseReader


async def test_query_metrics_builds_query_and_parses_rows(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(
        data=[{"name": "flow-a", "count": 3, "totalCost": 0.5}]
    )
    reader = LangfuseReader(manager)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    result = await reader.query_metrics(
        MetricsFilter(
            view=MetricsView.TRACES,
            metrics=["count", "totalCost"],
            dimensions=["name"],
            from_timestamp=now,
            to_timestamp=now,
            granularity="day",
        )
    )

    query = json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])
    assert query["view"] == "traces"
    assert {"measure": "count", "aggregation": "count"} in query["metrics"]
    assert {"measure": "totalCost", "aggregation": "sum"} in query["metrics"]
    assert query["dimensions"] == [{"field": "name"}]
    assert query["timeDimension"] == {"granularity": "day"}

    assert len(result.rows) == 1
    assert result.rows[0].dimensions == {"name": "flow-a"}
    assert result.rows[0].metrics == {"count": 3, "totalCost": 0.5}


async def test_query_metrics_forwards_order_by_and_defaults_unknown_measure_to_sum(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await LangfuseReader(manager).query_metrics(
        MetricsFilter(
            metrics=["latency", "someNewMeasure"],
            from_timestamp=now,
            to_timestamp=now,
            order_by=[{"field": "latency", "direction": "asc"}],
        )
    )
    query = json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])
    assert {"measure": "latency", "aggregation": "avg"} in query["metrics"]
    # A measure with no natural aggregation falls back to sum.
    assert {"measure": "someNewMeasure", "aggregation": "sum"} in query["metrics"]
    assert query["orderBy"] == [{"field": "latency", "direction": "asc"}]


async def test_query_metrics_distinct_tag_count(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(
        data=[{"tags": "a"}, {"tags": "b"}, {"tags": "c"}]
    )
    reader = LangfuseReader(manager)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = await reader.query_metrics(
        MetricsFilter(metrics=["count"], dimensions=["tags"], from_timestamp=now, to_timestamp=now)
    )
    assert result.derived["distinct_tag_count"] == 3


async def test_query_metrics_adds_environment_clause(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    caller_clause = {"column": "name", "operator": "=", "value": "flow-a", "type": "string"}
    await LangfuseReader(manager).query_metrics(
        MetricsFilter(metrics=["count"], from_timestamp=now, to_timestamp=now, filters=[caller_clause])
    )
    query = json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])
    # The environment scope is ADDED to the caller's filters, not replacing them.
    assert caller_clause in query["filters"]
    assert {"column": "environment", "operator": "=", "value": "tai", "type": "string"} in query["filters"]
