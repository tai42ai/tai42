"""``query_metrics`` on Metrics v2: run measures over the roots, usage measures over every observation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.monitoring import (
    Dimension,
    Measure,
    MetricsQuery,
    MonitoringError,
    MonitoringReadNotServedError,
)

T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)
_ENV = {"column": "environment", "operator": "any of", "value": ["tai"], "type": "stringOptions"}
_ROOT = {"column": "isRootObservation", "operator": "=", "value": True, "type": "boolean"}


def _query(**kw):
    return MetricsQuery(from_timestamp=T0, to_timestamp=T0 + timedelta(hours=2), **kw)


def _sent(fake):
    return [json.loads(kw["query"]) for kw in fake.calls_of("metrics")]


def _runs(fake):
    fake.run("a", T0 + timedelta(minutes=5), seconds=1, children=2, child_cost=0.5, child_tokens=10, child_model="m-1")
    fake.run("b", T0 + timedelta(minutes=10), seconds=3, children=1, child_cost=0.25, child_tokens=4, child_model="m-2")
    fake.run("c", T0 + timedelta(minutes=70), seconds=2)
    fake.run("foreign", T0 + timedelta(minutes=5), environment="other", children=1, child_cost=9.0)


async def test_ungrouped_measures_merge_two_queries_on_the_bucket(fake, reader):
    _runs(fake)
    result = await reader.query_metrics(
        _query(measures=[Measure.COUNT, Measure.LATENCY, Measure.COST, Measure.TOKENS], granularity="hour")
    )
    assert [(r.bucket, r.measures) for r in result.rows] == [
        (
            "2026-01-01T10:00:00Z",
            {Measure.COUNT: 2.0, Measure.LATENCY: 2000.0, Measure.COST: 1.25, Measure.TOKENS: 24.0},
        ),
        (
            "2026-01-01T11:00:00Z",
            {Measure.COUNT: 1.0, Measure.LATENCY: 2000.0, Measure.COST: None, Measure.TOKENS: 0.0},
        ),
    ]
    runs, usage = _sent(fake)
    assert runs["view"] == usage["view"] == "observations"
    assert runs["filters"] == [_ENV, _ROOT]
    assert runs["metrics"] == [
        {"measure": "count", "aggregation": "count"},
        {"measure": "latency", "aggregation": "avg"},
    ]
    assert usage["filters"] == [_ENV]
    assert usage["metrics"] == [
        {"measure": "totalCost", "aggregation": "sum"},
        {"measure": "totalTokens", "aggregation": "sum"},
    ]
    assert runs["timeDimension"] == {"granularity": "hour"}
    assert runs["fromTimestamp"] == T0.isoformat()


async def test_only_the_query_a_measure_needs_runs(fake, reader):
    _runs(fake)
    result = await reader.query_metrics(_query(measures=[Measure.COST]))
    assert [(r.bucket, r.measures) for r in result.rows] == [(None, {Measure.COST: 1.25})]
    assert len(_sent(fake)) == 1


async def test_a_bucket_one_query_lacks_reads_none_for_its_measures(fake, reader, monkeypatch):
    answers = iter(
        [
            [{"time_dimension": "2026-01-01T10:00:00Z", "count_count": 2}],
            [{"time_dimension": "2026-01-01T11:00:00Z", "sum_totalCost": 0.5}],
        ]
    )

    async def _metrics(client, query):
        return next(answers)

    monkeypatch.setattr(reader._metrics, "_metrics", _metrics)
    result = await reader.query_metrics(_query(measures=[Measure.COUNT, Measure.COST], granularity="hour"))
    assert [r.measures for r in result.rows] == [
        {Measure.COUNT: 2.0, Measure.COST: None},
        {Measure.COST: 0.5, Measure.COUNT: None},
    ]


async def test_model_grouping_reads_one_query_over_every_observation(fake, reader):
    _runs(fake)
    result = await reader.query_metrics(_query(measures=[Measure.COST], dimensions=[Dimension.MODEL]))
    by_model = {r.dimensions[Dimension.MODEL]: r.measures[Measure.COST] for r in result.rows}
    assert by_model == {"m-1": 1.0, "m-2": 0.25, None: None}
    [query] = _sent(fake)
    assert query["dimensions"] == [{"field": "providedModelName"}]
    assert query["filters"] == [_ENV]


def test_metrics_capability_declares_the_neutral_set(reader):
    capability = reader.metrics_capability()
    assert capability.measures == frozenset(Measure)
    assert capability.dimensions == frozenset({Dimension.MODEL})


async def test_an_unserved_measure_is_refused_before_any_request(fake, reader, monkeypatch):
    from tai42_monitoring_langfuse import metrics_query

    monkeypatch.delitem(metrics_query._MEASURE_TO_LANGFUSE, Measure.LATENCY)
    with pytest.raises(MonitoringReadNotServedError, match="latency"):
        await reader.query_metrics(_query(measures=[Measure.LATENCY]))
    assert fake.calls == []


@pytest.mark.parametrize(
    ("row", "missing"),
    [
        ({"time_dimension": "2026-01-01T10:00:00Z"}, "sum_totalCost"),
        ({"sum_totalCost": 1.0}, "time_dimension"),
    ],
)
async def test_a_row_without_a_requested_column_fails_loudly(fake, reader, monkeypatch, row, missing):
    async def _metrics(client, query):
        return [row]

    monkeypatch.setattr(reader._metrics, "_metrics", _metrics)
    with pytest.raises(MonitoringError, match=missing):
        await reader.query_metrics(_query(measures=[Measure.COST], granularity="hour"))


async def test_a_grouped_row_without_its_dimension_fails_loudly(fake, reader, monkeypatch):
    async def _metrics(client, query):
        return [{"sum_totalCost": 1.0}]

    monkeypatch.setattr(reader._metrics, "_metrics", _metrics)
    with pytest.raises(MonitoringError, match="providedModelName"):
        await reader.query_metrics(_query(measures=[Measure.COST], dimensions=[Dimension.MODEL]))
