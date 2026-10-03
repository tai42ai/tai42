"""MetricsQueryRunner: neutral-vocabulary mapping, typed-row parsing, and source scoping."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tai42_contract.monitoring import (
    Dimension,
    Measure,
    MetricsCapability,
    MetricsQuery,
    MonitoringReadNotSupportedError,
)

import tai42_monitoring_langfuse.metrics_query as metrics_query
from tai42_monitoring_langfuse.reader import LangfuseReader

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def test_query_metrics_maps_measures_and_parses_typed_rows(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(
        data=[{"time_dimension": "2026-01-01", "count": 3, "totalCost": 0.5}]
    )
    reader = LangfuseReader(manager)

    result = await reader.query_metrics(
        MetricsQuery(
            measures=[Measure.COUNT, Measure.COST],
            from_timestamp=_NOW,
            to_timestamp=_NOW,
            granularity="day",
        )
    )

    query = json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])
    assert query["view"] == "traces"
    assert {"measure": "count", "aggregation": "count"} in query["metrics"]
    assert {"measure": "totalCost", "aggregation": "sum"} in query["metrics"]
    assert query["dimensions"] == []
    assert query["timeDimension"] == {"granularity": "day"}

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.measures == {Measure.COUNT: 3.0, Measure.COST: 0.5}
    assert row.dimensions == {}
    assert row.bucket == "2026-01-01"


async def test_query_metrics_model_dimension_reads_observations_view(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(
        data=[{"providedModelName": "gpt-4o", "count": 7}]
    )
    result = await LangfuseReader(manager).query_metrics(
        MetricsQuery(measures=[Measure.COUNT], from_timestamp=_NOW, to_timestamp=_NOW, dimensions=[Dimension.MODEL])
    )
    query = json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])
    assert query["view"] == "observations"
    assert query["dimensions"] == [{"field": "providedModelName"}]

    row = result.rows[0]
    assert row.dimensions == {Dimension.MODEL: "gpt-4o"}
    assert row.measures == {Measure.COUNT: 7.0}
    assert row.bucket is None


async def test_query_metrics_adds_environment_clause(manager, mock_client):
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).query_metrics(
        MetricsQuery(measures=[Measure.COUNT], from_timestamp=_NOW, to_timestamp=_NOW)
    )
    query = json.loads(mock_client.api.legacy.metrics_v1.metrics.call_args.kwargs["query"])
    assert {"column": "environment", "operator": "=", "value": "tai", "type": "string"} in query["filters"]


def test_metrics_capability_declares_the_neutral_set(manager):
    capability = LangfuseReader(manager).metrics_capability()
    assert capability == MetricsCapability(
        measures=frozenset({Measure.COUNT, Measure.COST, Measure.TOKENS, Measure.LATENCY}),
        dimensions=frozenset({Dimension.MODEL}),
    )


async def test_query_metrics_raises_on_unserved_measure(manager, mock_client, monkeypatch):
    # A measure the backend has not mapped is refused loudly before any request —
    # never a silent zero. Simulated by dropping a measure from the mapping.
    patched = {m: v for m, v in metrics_query._MEASURE_TO_LANGFUSE.items() if m is not Measure.COST}
    monkeypatch.setattr(metrics_query, "_MEASURE_TO_LANGFUSE", patched)

    with pytest.raises(MonitoringReadNotSupportedError, match="cost"):
        await LangfuseReader(manager).query_metrics(
            MetricsQuery(measures=[Measure.COST], from_timestamp=_NOW, to_timestamp=_NOW)
        )
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()


async def test_query_metrics_missing_measure_column_raises(manager, mock_client):
    # A response row that carries no column for a requested measure cannot be served
    # as a silent zero — it raises.
    mock_client.api.legacy.metrics_v1.metrics.return_value = SimpleNamespace(data=[{"totalCost": 0.5}])
    with pytest.raises(MonitoringReadNotSupportedError, match="count"):
        await LangfuseReader(manager).query_metrics(
            MetricsQuery(measures=[Measure.COUNT], from_timestamp=_NOW, to_timestamp=_NOW)
        )
