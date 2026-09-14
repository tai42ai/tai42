"""TraceQuery metric-sort path: global metric ranking, page slicing, rank-order
preservation, input/filter validation, and the bounded ranked-row walk."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tai42_contract.monitoring import MonitoringFilter, MonitoringLevel, MonitoringReadNotSupportedError, OrderBy

from tai42_monitoring_langfuse.reader import LangfuseReader

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def test_version_metric_sort_is_unsupported(manager, mock_client):
    # The metrics ``traces`` view has no version column, so a metric SORT combined
    # with a version filter raises rather than misranking on a dropped clause.
    with pytest.raises(MonitoringReadNotSupportedError, match="version"):
        await LangfuseReader(manager).list_traces(
            from_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            limit=5,
            filter=MonitoringFilter(version="5"),
            order_by=OrderBy(field="total_cost", direction="desc"),
        )


@pytest.mark.parametrize(
    ("field", "measure"),
    [("total_cost", "totalCost"), ("latency", "latency"), ("total_tokens", "totalTokens")],
)
async def test_metric_sort_builds_query(
    manager, mock_client, field, measure, list_returns, route_metrics, errors_for, metric_query, trace_row
):
    route_metrics(ranking=[{"id": "a", f"sum_{measure}": 5}], tokens={})
    list_returns([trace_row(id="a")])
    errors_for([])
    await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field=field, direction="desc"), from_timestamp=_NOW, limit=5
    )
    q = metric_query()
    assert q["view"] == "traces"
    assert q["dimensions"] == [{"field": "id"}]
    # sum (NOT avg, even for latency) so the orderBy sum_<measure> key matches.
    assert q["metrics"] == [{"measure": measure, "aggregation": "sum"}]
    assert q["orderBy"] == [{"field": f"sum_{measure}", "direction": "desc"}]
    assert q["fromTimestamp"] == _NOW.isoformat()
    assert q["toTimestamp"]
    assert q["config"]["row_limit"] == 5
    # metric sort resolves the ranked ids' rows via trace.list, never trace.get.
    mock_client.api.trace.list.assert_called()
    mock_client.api.trace.get.assert_not_called()


async def test_metric_sort_direction_asc(manager, mock_client, route_metrics, metric_query):
    route_metrics(ranking=[], tokens={})
    await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost", direction="asc"), from_timestamp=_NOW, limit=5
    )
    assert metric_query()["orderBy"][0]["direction"] == "asc"


async def test_metric_sort_filters_exact_clauses(manager, mock_client, route_metrics, metric_query):
    route_metrics(ranking=[], tokens={})
    await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost"),
        from_timestamp=_NOW,
        limit=5,
        filter=MonitoringFilter(name="flow-a", user_id="u1", session_id="s1", tags=["run:7"], metadata={"k": "v"}),
    )
    filters = metric_query()["filters"]
    # tags rides the metrics column `tags` (arrayOptions), never `traceTags`.
    assert {"column": "tags", "operator": "any of", "value": ["run:7"], "type": "arrayOptions"} in filters
    assert not any(c["column"] == "traceTags" for c in filters)
    cols = {c["column"]: c for c in filters}
    assert cols["name"]["value"] == "flow-a"
    assert cols["userId"]["value"] == "u1"
    assert cols["sessionId"]["value"] == "s1"
    assert cols["environment"]["value"] == "tai"
    assert cols["metadata"]["type"] == "stringObject"
    assert cols["metadata"]["key"] == "k"
    assert cols["metadata"]["value"] == "v"


async def test_metric_sort_preserves_ranked_order(
    manager, mock_client, list_returns, route_metrics, errors_for, trace_row
):
    # Rows in non-alphabetical rank order; the resolved rows' cost deliberately
    # disagrees with the rank, so any client re-sort would break the order.
    route_metrics(
        ranking=[
            {"id": "c", "sum_totalCost": 9},
            {"id": "a", "sum_totalCost": 5},
            {"id": "b", "sum_totalCost": 1},
        ],
        tokens={},
    )
    list_returns(
        [trace_row(id="c", total_cost=1.0), trace_row(id="a", total_cost=9.0), trace_row(id="b", total_cost=5.0)],
    )
    errors_for([])
    result = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost", direction="desc"), from_timestamp=_NOW, limit=5
    )
    assert [t.id for t in result] == ["c", "a", "b"]
    # the resolved row's cost survives, distinct from the rank
    assert result[0].total_cost == 1.0


async def test_metric_sort_page_slice(
    manager, mock_client, list_returns, route_metrics, errors_for, metric_query, trace_row
):
    ranking = [{"id": f"r{i}", "sum_totalCost": 10 - i} for i in range(10)]
    route_metrics(ranking=ranking, tokens={})
    list_returns([trace_row(id=f"r{i}") for i in range(10)])
    errors_for([])
    r = LangfuseReader(manager)
    res = await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=2)
    assert [t.id for t in res] == ["r3", "r4", "r5"]
    assert metric_query()["config"]["row_limit"] == 6
    res = await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=1)
    assert [t.id for t in res] == ["r0", "r1", "r2"]
    assert metric_query()["config"]["row_limit"] == 3
    res = await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=None)
    assert [t.id for t in res] == ["r0", "r1", "r2"]


async def test_metric_sort_page_past_end_is_empty(manager, mock_client, route_metrics):
    ranking = [
        {"id": "r0", "sum_totalCost": 3},
        {"id": "r1", "sum_totalCost": 2},
        {"id": "r2", "sum_totalCost": 1},
    ]
    route_metrics(ranking=ranking, tokens={})
    res = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=2
    )
    assert res == []
    # An empty ranked slice never walks trace.list.
    mock_client.api.trace.list.assert_not_called()


async def test_metric_sort_partial_last_page(manager, mock_client, list_returns, route_metrics, errors_for, trace_row):
    ranking = [{"id": f"r{i}", "sum_totalCost": 4 - i} for i in range(4)]
    route_metrics(ranking=ranking, tokens={})
    list_returns([trace_row(id=f"r{i}") for i in range(4)])
    errors_for([])
    res = await LangfuseReader(manager).list_traces(
        order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=3, page=2
    )
    assert [t.id for t in res] == ["r3"]


async def test_metric_sort_row_limit_cap_inclusive(manager, mock_client, route_metrics, metric_query):
    route_metrics(ranking=[], tokens={})
    r = LangfuseReader(manager)
    await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=1000, page=1)
    assert metric_query()["config"]["row_limit"] == 1000
    with pytest.raises(MonitoringReadNotSupportedError):
        await r.list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=600, page=2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": None},
        {"limit": 0},
        {"page": 0, "limit": 5},
        {"from_timestamp": None, "limit": 5},
    ],
)
async def test_metric_sort_input_validation_raises_before_fetch(manager, mock_client, kwargs):
    base = {"order_by": OrderBy(field="total_cost"), "from_timestamp": _NOW}
    base.update(kwargs)
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(**base)
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()
    mock_client.api.trace.list.assert_not_called()


@pytest.mark.parametrize(
    "flt",
    [
        MonitoringFilter(level=MonitoringLevel.ERROR),
        MonitoringFilter(model="gpt"),
        MonitoringFilter(min_cost=1.0),
        MonitoringFilter(max_cost=1.0),
        MonitoringFilter(min_tokens=1),
        MonitoringFilter(max_tokens=1),
        MonitoringFilter(min_latency=1.0),
        MonitoringFilter(max_latency=1.0),
    ],
)
async def test_metric_sort_unsupported_filter_raises_before_fetch(manager, mock_client, flt):
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(
            order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=5, filter=flt
        )
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()
    mock_client.api.trace.list.assert_not_called()


async def test_metric_sort_unsupported_filter_names_clauses(manager, mock_client):
    with pytest.raises(MonitoringReadNotSupportedError) as exc:
        await LangfuseReader(manager).list_traces(
            order_by=OrderBy(field="total_cost"),
            from_timestamp=_NOW,
            limit=5,
            filter=MonitoringFilter(min_cost=1.0, max_tokens=10),
        )
    assert "min_cost" in str(exc.value)
    assert "max_tokens" in str(exc.value)


async def test_metric_sort_filter_ok_on_default_sort(manager, mock_client):
    # The same level/range filter that a metric sort rejects works on the
    # default (native) sort via trace.list.
    mock_client.api.trace.list.return_value = SimpleNamespace(data=[])
    await LangfuseReader(manager).list_traces(filter=MonitoringFilter(level=MonitoringLevel.ERROR, min_cost=1.0))
    mock_client.api.legacy.metrics_v1.metrics.assert_not_called()


async def test_metric_sort_ranked_id_absent_from_window_raises(
    manager, mock_client, list_returns, route_metrics, errors_for
):
    # METRIC path coverage: a ranked id the list window never surfaces cannot be
    # resolved into a row — the page raises loudly rather than returning short.
    route_metrics(ranking=[{"id": "ghost", "sum_totalCost": 9}], tokens={})
    list_returns([])  # a single short page ends the walk
    errors_for([])
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=5)
    mock_client.api.trace.get.assert_not_called()


async def test_metric_sort_walk_page_budget_raises(manager, mock_client, route_metrics, errors_for, trace_row):
    # The walk is bounded: full pages that never carry the ranked id stop at the
    # page budget with a loud error, never an unbounded scan.
    from tai42_monitoring_langfuse.query_base import _PAGE_SIZE
    from tai42_monitoring_langfuse.trace_query import _METRIC_LIST_PAGE_BUDGET

    route_metrics(ranking=[{"id": "ghost", "sum_totalCost": 9}], tokens={})
    full = [trace_row(id=f"x{i}") for i in range(_PAGE_SIZE)]
    mock_client.api.trace.list.return_value = SimpleNamespace(
        data=full, meta=SimpleNamespace(total_pages=_METRIC_LIST_PAGE_BUDGET + 5)
    )
    errors_for([])
    with pytest.raises(MonitoringReadNotSupportedError):
        await LangfuseReader(manager).list_traces(order_by=OrderBy(field="total_cost"), from_timestamp=_NOW, limit=5)
    assert mock_client.api.trace.list.call_count <= _METRIC_LIST_PAGE_BUDGET + 1
